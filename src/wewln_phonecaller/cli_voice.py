"""Voice commands: try the voice engine and the whole call path without placing a call.

    wewln voice check       load the models; synthesize, recognize, ask the dialogue model
    wewln voice chat        a text conversation with the dialogue model (to tune a script)
    wewln voice simulate    a spoken conversation with a simulated caller (no call at all)
    wewln voice download    the default Vosk model and Piper voice into the models folder
    wewln calls transcript  what was said on a call
    wewln calls preflight   every check before a call; nothing is dialed

These commands work also with WEWLN_VOICE_MODE=off: calls use the engine only when it is on.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import re
import shutil
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import urlopen

from .cli import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RUNTIME,
    _load,
    _number,
    _quiet_logging,
    _repository,
    _store,
)
from .config import DEFAULT_PIPER_VOICE, DEFAULT_VOSK_MODEL, Settings
from .errors import WewlnError
from .phones import describe_number, dial_policy_problem
from .redact import mask_phone_numbers
from .safety import evaluate_real_calls
from .storage.calls import CallStore
from .telephony import PROVIDERS
from .telephony.base import ProviderError
from .telephony.factory import create_provider
from .telephony.service import CallService, DialerBusyError
from .voice.audio import SAMPLE_RATE, duration_ms, read_wav, resample, wav_bytes
from .voice.engine import SessionResult, Turn
from .voice.llm import ChatModel, LlmError
from .voice.runtime import (
    VoiceUnavailableError,
    build_llm,
    build_stt,
    build_tts,
    create_voice_runtime,
    system_prompt,
)
from .voice.simulate import DEFAULT_PHRASES, SimulatedCaller
from .voice.stt import SttError
from .voice.text import SentenceSplitter, asks_not_to_call, speakable
from .voice.timing import fine_timer
from .voice.transport import memory_pair
from .voice.tts import TextToSpeech, TtsError

CHECK_PHRASE = "Здравствуйте! Это проверка связи. Как вы меня слышите?"
CHECK_QUESTION = "Алло, здравствуйте. А кто это звонит?"
_WORD = re.compile(r"[a-zа-я0-9]+")


def _voice_settings(settings: Settings) -> Settings:
    """The settings with the engine on (these commands try it also while calls do not use it)."""
    if settings.voice_enabled:
        return settings
    return settings.model_copy(update={"voice_mode": "ai"})


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("ё", "е"))


def word_match(expected: str, heard: str) -> float:
    """Share of the expected words that were recognized (a rough recognition check)."""
    want = _words(expected)
    got = set(_words(heard))
    return sum(1 for w in want if w in got) / len(want) if want else 1.0


_STARTUP = {
    "stt_load_s": "recognition",
    "tts_load_s": "synthesis",
    "llm_load_s": "dialogue model",
    "phrases_s": "fixed phrases",
}


def startup_text(timings: dict[str, Any]) -> str:
    """How long the engine took to load, per part."""
    return ", ".join(f"{_STARTUP.get(k, k)} {v} s" for k, v in timings.items())


def _seconds(value: float) -> str:
    return f"{value:.2f} s" if value < 10 else f"{value:.0f} s"


async def _timed(awaitable: Any) -> float:
    started = time.perf_counter()
    await awaitable
    return time.perf_counter() - started


async def _close(*components: Any) -> None:
    for component in components:
        if component is not None:
            with contextlib.suppress(Exception):
                await component.close()


def _script(args: argparse.Namespace, settings: Settings) -> str | None:
    """--script TEXT, or the «Сценарий» of --campaign ID; None: the test call's script."""
    if getattr(args, "script", None):
        return str(args.script)
    campaign = getattr(args, "campaign", None)
    if campaign is None:
        return None
    row = _repository(settings).get_campaign(campaign)
    script = (row["script"] or "").strip()
    if not script:
        raise WewlnError(f"campaign {campaign} has no script («Сценарий»)", code="invalid_input")
    return script


def describe_call_path(settings: Settings, *, softphone: bool = False) -> list[str]:
    """How a call goes, in plain words (printed before calls and by preflight)."""
    lines: list[str] = []
    asterisk = settings.telephony_provider == "asterisk"
    if not asterisk:
        lines.append("provider: mock - simulated calls; nothing reaches a phone or a network")
    else:
        lines.append(f"provider: Asterisk over ARI at {settings.ari_url} (app {settings.ari_app})")
        if softphone:
            lines.append(
                f"dial: {settings.asterisk_softphone_endpoint} - the local softphone, "
                "no phone network"
            )
        else:
            lines.append(
                f"dial: {settings.asterisk_endpoint} - the SIP trunk to the phone network "
                "(real calls only behind the real-call gate)"
            )
    if settings.voice_enabled:
        lines.append(
            f"answered call: a spoken conversation - recognition {settings.voice_stt}, "
            f"dialogue {settings.voice_llm} {settings.voice_llm_model}, "
            f"synthesis {settings.voice_tts}"
        )
        if asterisk:
            lines.append(
                f"call audio: Asterisk connects to {settings.audiosocket_advertise} (AudioSocket; "
                f"wewln listens on {settings.voice_audiosocket_host}:"
                f"{settings.voice_audiosocket_port})"
            )
        else:
            lines.append("call audio: in memory, with a simulated caller")
    elif asterisk:
        media = settings.asterisk_answer_media or "nothing"
        lines.append(f"answered call: plays {media} and ends (WEWLN_VOICE_MODE=off)")
    else:
        lines.append("answered call: ends as the mock scenario says (WEWLN_VOICE_MODE=off)")
    return lines


def turn_line(turn: Turn | dict[str, Any]) -> str:
    """One phrase of a transcript with its timings."""
    get = turn.get if isinstance(turn, dict) else (lambda key: getattr(turn, key))
    who = "wewln" if get("role") == "assistant" else "caller"
    notes: list[str] = []
    if get("kind") not in ("reply", "speech"):
        notes.append(str(get("kind")))
    if get("interrupted"):
        notes.append("interrupted")
    if get("speech_ms") is not None:
        notes.append(f"spoke {get('speech_ms') / 1000:.1f} s")
    if get("stt_ms") is not None:
        notes.append(f"recognized in {get('stt_ms') / 1000:.2f} s")
    if get("response_ms") is not None:
        notes.append(f"answered after {get('response_ms') / 1000:.2f} s")
    at = (get("at_ms") or 0) / 1000
    suffix = f"  ({', '.join(notes)})" if notes else ""
    return f"  {at:6.1f} s  {who}: {get('text') or '...'}{suffix}"


def print_conversation(result: SessionResult) -> None:
    opt_out = ", asked not to be called again" if result.opt_out else ""
    error = f", error {result.error}" if result.error else ""
    print(f"Conversation: {result.reason}, {result.caller_turns} caller phrase(s){opt_out}{error}")
    for turn in result.turns:
        print(turn_line(turn))
    replies = [t.response_ms for t in result.turns if t.response_ms is not None]
    if replies:
        print(
            f"Answer delay: {min(replies) / 1000:.2f}-{max(replies) / 1000:.2f} s "
            f"(average {sum(replies) / len(replies) / 1000:.2f} s, from the end of the "
            "caller's phrase to wewln's first sound)"
        )


# ---------------------------------------------------------------------- voice check


async def _ask(llm: ChatModel, messages: list[dict[str, str]]) -> tuple[str, Any, float, float]:
    """(spoken answer, marks, seconds to the first words, seconds in all)."""
    splitter = SentenceSplitter()
    sentences: list[str] = []
    started = time.perf_counter()
    first: float | None = None
    async for piece in llm.stream(messages):
        if first is None:
            first = time.perf_counter()
        sentences += [s for s in (speakable(x) for x in splitter.feed(piece)) if s]
    sentences += [s for s in (speakable(x) for x in splitter.flush()) if s]
    total = time.perf_counter() - started
    return " ".join(sentences), splitter.marks, (first or time.perf_counter()) - started, total


async def check_voice(settings: Settings, *, llm_check: bool = True) -> tuple[bool, bytes]:
    """Every model of the engine, one by one; returns (all passed, the synthesized phrase)."""
    ok = True
    audio = b""
    tts = build_tts(settings)
    stt = build_stt(settings)
    llm = build_llm(settings) if llm_check else None
    try:
        print(f"Speech synthesis: {settings.voice_tts} ({tts.describe().get('model', '')})")
        try:
            load_s = await _timed(tts.load())
            started = time.perf_counter()
            audio = await tts.synthesize(CHECK_PHRASE)
            synth_s = time.perf_counter() - started
            print(
                f"  [ok] loaded in {_seconds(load_s)}; a {duration_ms(audio) / 1000:.1f} s "
                f"phrase took {_seconds(synth_s)}"
            )
        except TtsError as exc:
            ok = False
            print(f"  [FAIL] {exc}")
        print(f"Speech recognition: {settings.voice_stt} ({stt.describe().get('model', '')})")
        try:
            load_s = await _timed(stt.load())
            if not audio:
                print(f"  [ok] loaded in {_seconds(load_s)} (no phrase to recognize)")
            else:
                started = time.perf_counter()
                heard = await stt.transcribe(audio)
                stt_s = time.perf_counter() - started
                share = word_match(CHECK_PHRASE, heard)
                mark = "ok" if share >= 0.5 else "WARN"
                print(
                    f"  [{mark}] loaded in {_seconds(load_s)}; the phrase (8 kHz, as on a "
                    f"phone line) was recognized in {_seconds(stt_s)}: «{heard}» "
                    f"({share:.0%} of the words)"
                )
        except SttError as exc:
            ok = False
            print(f"  [FAIL] {exc}")
        if llm is not None:
            print(
                f"Dialogue model: {settings.voice_llm} {settings.voice_llm_model} "
                f"at {settings.voice_llm_url}"
            )
            try:
                check = await llm.check()
                if check.get("status") != "ok":
                    ok = False
                    print(f"  [FAIL] {check.get('error')}")
                else:
                    warm_s = await _timed(llm.warm(system_prompt(None)))
                    messages = [
                        {"role": "system", "content": system_prompt(None)},
                        {"role": "assistant", "content": settings.voice_greeting},
                        {"role": "user", "content": CHECK_QUESTION},
                    ]
                    answer, _marks, first_s, total_s = await _ask(llm, messages)
                    version = f"version {check['version']}; " if check.get("version") else ""
                    print(
                        f"  [ok] {version}loaded in {_seconds(warm_s)}; first words after "
                        f"{_seconds(first_s)}, the whole answer in {_seconds(total_s)}"
                    )
                    print(f"  «{CHECK_QUESTION}» -> «{answer or '(empty answer)'}»")
                    if not answer:
                        ok = False
            except LlmError as exc:
                ok = False
                print(f"  [FAIL] {exc}")
    finally:
        await _close(tts, stt, llm)
    return ok, audio


def cmd_voice_check(args: argparse.Namespace) -> int:
    loaded = _load(args.env_file)
    if loaded is None:
        return EXIT_CONFIG
    _quiet_logging(loaded)
    settings = _voice_settings(loaded)
    print("Voice engine check (nothing is dialed; everything runs on this computer).")
    if not loaded.voice_enabled:
        print("Note: WEWLN_VOICE_MODE=off, so calls do not use the engine yet.")
    problems = settings.voice_problems
    if problems:
        print("Not ready:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_CONFIG
    ok, audio = asyncio.run(check_voice(settings, llm_check=not args.no_llm))
    if args.save_wav and audio:
        Path(args.save_wav).write_bytes(wav_bytes(audio))
        print(f"The synthesized phrase is saved to {args.save_wav} (8 kHz, as on a phone line).")
    print("Voice check passed." if ok else "Voice check failed; see docs/VOICE.md.")
    return EXIT_OK if ok else EXIT_RUNTIME


# ---------------------------------------------------------------------- voice chat


async def chat(settings: Settings, script: str | None) -> int:
    llm = build_llm(settings)
    try:
        check = await llm.check()
        if check.get("status") != "ok":
            print(f"The dialogue model is not ready: {check.get('error')}", file=sys.stderr)
            return EXIT_RUNTIME
        system = system_prompt(script)
        history = [{"role": "assistant", "content": settings.voice_greeting}]
        print("A text conversation with the dialogue model, as a caller would hear it.")
        print("An empty line ends it.")
        print(f"wewln: {settings.voice_greeting}")
        phrases = 0
        while True:
            try:
                line = (await asyncio.to_thread(input, "you: ")).strip()
            except EOFError:
                break
            if not line:
                break
            phrases += 1
            history.append({"role": "user", "content": line})
            if phrases > settings.voice_max_turns:
                print(f"wewln: {settings.voice_goodbye}")
                print("(the conversation reached WEWLN_VOICE_MAX_TURNS)")
                break
            messages = [{"role": "system", "content": system}, *history[-16:]]
            answer, marks, first_s, total_s = await _ask(llm, messages)
            print(
                f"wewln: {answer or '(nothing)'}  [first words after {_seconds(first_s)}, "
                f"{_seconds(total_s)} in all]"
            )
            if answer:
                history.append({"role": "assistant", "content": answer})
            if marks.opt_out or asks_not_to_call(line):
                print("(a request not to be called: a real call puts the number on the DNC list)")
            if marks.end:
                print("(the dialogue model ended the conversation)")
                break
    except LlmError as exc:
        print(f"Dialogue model error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        await llm.close()
    return EXIT_OK


def cmd_voice_chat(args: argparse.Namespace) -> int:
    loaded = _load(args.env_file)
    if loaded is None:
        return EXIT_CONFIG
    _quiet_logging(loaded)
    settings = _voice_settings(loaded)
    try:
        return asyncio.run(chat(settings, _script(args, settings)))
    except KeyboardInterrupt:
        print()
        return EXIT_OK


# ---------------------------------------------------------------------- voice simulate


class WavVoice(TextToSpeech):
    """A simulated caller who «says» WAV files: the phrase is the file's path."""

    name = "wav-files"

    async def synthesize(self, text: str) -> bytes:
        try:
            pcm, rate = read_wav(await asyncio.to_thread(Path(text).read_bytes))
        except (OSError, ValueError) as exc:
            raise TtsError(f"{text}: {exc}") from None
        return resample(pcm, rate, SAMPLE_RATE)


async def simulate(
    settings: Settings,
    *,
    phrases: list[str],
    script: str | None = None,
    wavs: list[str] | None = None,
    save_wav: str | None = None,
) -> SessionResult:
    runtime = create_voice_runtime(settings)
    try:
        print("Loading the voice engine...")
        await runtime.start()
        print(f"  loaded: {startup_text(runtime.startup)}")
        line, phone = memory_pair()
        voice: TextToSpeech = WavVoice() if wavs else runtime.tts
        caller = SimulatedCaller(
            phone, list(wavs or phrases), voice, frame_s=runtime.config.frame_s
        )
        session = runtime.session(line, script=script, label="simulation")
        print("Conversation with a simulated caller (no call; real time)...")
        with fine_timer():
            person = asyncio.create_task(caller.run())
            try:
                result = await session.run()
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(person, 5)
        if save_wav:
            await asyncio.to_thread(Path(save_wav).write_bytes, caller.recording())
        return result
    finally:
        await runtime.close()


def cmd_voice_simulate(args: argparse.Namespace) -> int:
    loaded = _load(args.env_file)
    if loaded is None:
        return EXIT_CONFIG
    _quiet_logging(loaded)
    settings = _voice_settings(loaded)
    problems = settings.voice_problems
    if problems:
        print("Not ready:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        result = asyncio.run(
            simulate(
                settings,
                phrases=args.say or list(DEFAULT_PHRASES),
                script=_script(args, settings),
                wavs=args.wav,
                save_wav=args.save_wav,
            )
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    print_conversation(result)
    if args.save_wav:
        print(f"The recording is saved to {args.save_wav} (stereo: left the caller, right wewln).")
    return EXIT_RUNTIME if result.reason == "voice_error" else EXIT_OK


# ---------------------------------------------------------------------- voice download


class DownloadError(WewlnError):
    code = "download_failed"


@dataclass(frozen=True)
class ModelFile:
    url: str
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelPackage:
    title: str
    files: tuple[ModelFile, ...]
    # What lands in the models folder: the unpacked folder (zip) or the first file.
    installed: str
    unpack: bool = False


_PIPER = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/"
    "e7229f7d33a10e145e109b886d661984201ac3c4/ru/ru_RU/denis/medium/"
)
MODEL_PACKAGES = {
    "vosk": ModelPackage(
        "Vosk vosk-model-small-ru-0.22: Russian speech recognition (45 MB, Apache-2.0)",
        (
            ModelFile(
                f"https://alphacephei.com/vosk/models/{DEFAULT_VOSK_MODEL}.zip",
                f"{DEFAULT_VOSK_MODEL}.zip",
                46_236_750,
                "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4",
            ),
        ),
        installed=DEFAULT_VOSK_MODEL,
        unpack=True,
    ),
    "piper": ModelPackage(
        "Piper voice ru_RU-denis-medium: Russian speech synthesis (63 MB; licenses: docs/VOICE.md)",
        (
            ModelFile(
                _PIPER + DEFAULT_PIPER_VOICE,
                DEFAULT_PIPER_VOICE,
                63_201_294,
                "15fab56e11a097858ee115545d0f697fc2a316c41a291a5362349fb870411b0a",
            ),
            ModelFile(
                _PIPER + f"{DEFAULT_PIPER_VOICE}.json",
                f"{DEFAULT_PIPER_VOICE}.json",
                4_823,
                "831c860dac0b5073eaa81610a0a638ec23d90a6cf8e5f871b4485c2cec3767c8",
            ),
        ),
        installed=DEFAULT_PIPER_VOICE,
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_checked(source: Any, dest: Path, item: ModelFile, progress: Callable[[int], None]):
    """Copy a stream to `dest` (via a .part file) if its size and SHA-256 are the expected."""
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("wb") as out:
            while chunk := source.read(1 << 16):
                size += len(chunk)
                if size > item.size:
                    raise DownloadError(f"{item.name}: bigger than expected; nothing installed")
                digest.update(chunk)
                out.write(chunk)
                progress(size)
        if size != item.size or digest.hexdigest() != item.sha256:
            raise DownloadError(
                f"{item.name}: the file is damaged or not the expected one (size {size} bytes, "
                "SHA-256 mismatch); nothing installed"
            )
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def fetch(item: ModelFile, dest: Path, *, source_dir: Path | None = None) -> None:
    """One model file into `dest`: downloaded over HTTPS, or copied from `source_dir`."""
    shown = [-1]

    def progress(size: int) -> None:
        if not sys.stdout.isatty():
            return
        percent = size * 100 // max(1, item.size)
        if percent != shown[0]:
            shown[0] = percent
            print(f"\r  {item.name}: {size / 1e6:.1f} of {item.size / 1e6:.1f} MB", end="")

    if source_dir is not None:
        local = source_dir / item.name
        if not local.is_file():
            raise DownloadError(f"{local} does not exist")
        with local.open("rb") as stream:
            _copy_checked(stream, dest, item, progress)
        return
    if not item.url.startswith("https://"):
        raise DownloadError(f"{item.url}: only HTTPS downloads are allowed")
    try:
        with urlopen(item.url, timeout=60) as response:  # noqa: S310 - HTTPS checked above
            _copy_checked(response, dest, item, progress)
    except OSError as exc:
        raise DownloadError(f"{item.name}: the download failed ({exc})") from None
    finally:
        if shown[0] >= 0:
            print()


def unpack(archive: Path, models: Path, folder: str) -> Path:
    """A zip with one top folder `folder` into models/folder (no path leaves it)."""
    target = models / folder
    staging = models / f".{folder}.unpacking"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = PurePosixPath(info.filename.replace("\\", "/"))
                if name.is_absolute() or ".." in name.parts or name.parts[:1] != (folder,):
                    raise DownloadError(f"unexpected path in the archive: {info.filename}")
            zf.extractall(staging)
        if target.exists():
            shutil.rmtree(target)
        (staging / folder).replace(target)
    except zipfile.BadZipFile as exc:
        raise DownloadError(f"{archive.name}: not a valid zip archive ({exc})") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def install_package(
    key: str, models: Path, *, force: bool = False, source_dir: Path | None = None
) -> tuple[Path, bool]:
    """(where the model is, whether anything was installed now)."""
    package = MODEL_PACKAGES[key]
    models.mkdir(parents=True, exist_ok=True)
    installed = models / package.installed
    if package.unpack:
        present = (installed / "am").is_dir() or (installed / "conf").is_dir()
    else:
        present = all(
            (models / f.name).is_file() and (models / f.name).stat().st_size == f.size
            for f in package.files
        )
    if present and not force:
        return installed, False
    for item in package.files:
        dest = models / item.name
        size_ok = dest.is_file() and dest.stat().st_size == item.size
        if not force and size_ok and _sha256(dest) == item.sha256:
            continue
        fetch(item, dest, source_dir=source_dir)
    if package.unpack:
        archive = models / package.files[0].name
        unpack(archive, models, package.installed)
        archive.unlink(missing_ok=True)
    return installed, True


def download_epilog() -> str:
    """The files `voice download` fetches: for downloading them by hand (--from)."""
    lines = ["files (checked by size and SHA-256; download them by hand for --from):"]
    for key, package in MODEL_PACKAGES.items():
        for item in package.files:
            lines.append(f"  {key}: {item.url}")
            lines.append(f"    {item.name}, {item.size} bytes, SHA-256 {item.sha256}")
    return "\n".join(lines)


def cmd_voice_download(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    _quiet_logging(settings)
    models = settings.models_dir
    source = Path(args.source) if args.source else None
    keys = list(MODEL_PACKAGES) if args.model == "all" else [args.model]
    for key in keys:
        package = MODEL_PACKAGES[key]
        size = sum(f.size for f in package.files) / 1e6
        verb = "Copying" if source else "Downloading"
        print(f"{package.title}")
        print(f"  {verb} {size:.0f} MB into {models} ...")
        started = time.perf_counter()
        try:
            path, changed = install_package(key, models, force=args.force, source_dir=source)
        except DownloadError as exc:
            print(f"  Failed: {exc.message}", file=sys.stderr)
            return EXIT_RUNTIME
        if changed:
            print(f"  Ready in {_seconds(time.perf_counter() - started)}: {path}")
        else:
            print(f"  Already there: {path} (--force downloads it again)")
    print("Next: wewln voice check")
    return EXIT_OK


# ---------------------------------------------------------------------- calls transcript


def cmd_calls_transcript(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    _quiet_logging(settings)
    store = _store(settings)
    attempt = store.get_attempt(args.attempt_id)
    if args.delete:
        removed = store.delete_transcript(args.attempt_id)
        print(f"Deleted {removed} phrase(s) of call {args.attempt_id}.")
        return EXIT_OK
    turns = store.transcript(args.attempt_id)
    end = attempt.get("voice_end") or "no conversation"
    opt_out = ", asked not to be called again" if attempt.get("opt_out") else ""
    print(f"Call {args.attempt_id} ({attempt['kind']}, {attempt['state']}): {end}{opt_out}")
    if not turns:
        print(
            "  (no transcript: the call had no conversation, transcripts are off "
            "(WEWLN_VOICE_STORE_TRANSCRIPTS), or it was deleted)"
        )
    for turn in turns:
        print(turn_line(turn))
    return EXIT_OK


# ---------------------------------------------------------------------- calls preflight


@dataclass
class Checks:
    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        self.rows.append((name, {True: "ok", False: "FAIL", None: "--"}[ok], detail))

    @property
    def ok(self) -> bool:
        return all(status != "FAIL" for _name, status, _detail in self.rows)

    def print(self) -> None:
        width = max((len(name) for name, _s, _d in self.rows), default=0)
        for name, status, detail in self.rows:
            print(f"  [{status:>4}] {name.ljust(width)}  {detail}")


def _check_summary(result: dict[str, Any]) -> str:
    if result.get("status") != "ok":
        return str(result.get("error") or result.get("status"))
    skip = {"status", "places_real_calls"}
    return ", ".join(f"{k} {v}" for k, v in result.items() if k not in skip and v is not None)


async def _preflight(settings: Settings, store: CallStore, checks: Checks) -> None:
    provider = create_provider(settings)
    if settings.voice_enabled and provider.supports_media:
        provider.enable_media()  # the check then covers the AudioSocket module too
    try:
        result = await provider.check()
    except ProviderError as exc:
        result = {"status": "error", "error": exc.message}
    checks.add("telephony", result.get("status") == "ok", _check_summary(result))
    if not settings.voice_enabled:
        checks.add("voice engine", None, "off (WEWLN_VOICE_MODE=off)")
        return
    problems = settings.voice_problems
    if problems:
        checks.add("voice engine", False, "; ".join(problems))
        return
    runtime = create_voice_runtime(settings)
    try:
        try:
            await runtime.start()
        except VoiceUnavailableError as exc:
            checks.add("voice engine", False, exc.message)
            return
        checks.add("voice engine", True, f"loaded: {startup_text(runtime.startup)}")
        if settings.telephony_provider == "mock":
            checks.add("call audio", True, "in memory (the mock provider; a simulated caller)")
            return
        if result.get("status") != "ok":
            checks.add("call audio", None, "not checked: the telephony check failed")
            return
        # The audio check needs the dialer (one at a time): Asterisk connects a test audio
        # channel to wewln's AudioSocket server; nothing is dialed.
        media_provider = create_provider(settings)
        try:
            async with CallService(settings, store, media_provider, voice=runtime):
                media = await media_provider.check_media()  # type: ignore[attr-defined]
        except DialerBusyError as exc:
            media = {"status": "error", "error": exc.message}
        except ProviderError as exc:
            media = {"status": "error", "error": exc.message}
        ok = media.get("status") == "ok"
        detail = (
            f"Asterisk reached {media.get('target')} in {media.get('connect_ms')} ms"
            if ok
            else str(media.get("error"))
        )
        checks.add("call audio", ok, detail)
    finally:
        await runtime.close()


def cmd_calls_preflight(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    _quiet_logging(settings)
    print("Preflight: the checks before a call; nothing is dialed.")
    print("Call path:")
    for line in describe_call_path(settings, softphone=args.softphone):
        print(f"  {line}")
    checks = Checks()
    problems = settings.provider_problems
    checks.add("configuration", not problems, "; ".join(problems) or "valid")
    store = _store(settings)
    kill = store.kill_switch()
    checks.add(
        "emergency stop",
        kill is None and not settings.kill_switch,
        "off" if kill is None and not settings.kill_switch else "ON: wewln calls resume",
    )
    real = PROVIDERS[settings.telephony_provider].places_real_calls
    if args.to:
        to = _number(settings, args.to)
        if to is None:
            return EXIT_CONFIG
        info = describe_number(to)
        kind = f"{info.region} {info.kind}" if info else "unknown"
        policy = dial_policy_problem(to, settings.allowed_call_regions) if real else None
        checks.add(
            "number",
            policy is None,
            f"{mask_phone_numbers(to)} ({kind})" + (f": {policy}" if policy else ""),
        )
        if real:
            decision = evaluate_real_calls(
                settings,
                operator_armed=store.has_valid_permit(to),
                kill_switch_engaged=kill is not None,
            )
            reasons = ", ".join(r.value for r in decision.reasons)
            checks.add(
                "real-call gate",
                decision.allowed,
                "open: a permit is in force" if decision.allowed else f"blocked by: {reasons}",
            )
        else:
            checks.add("real-call gate", None, "not needed: the mock provider simulates calls")
    if not problems:
        asyncio.run(_preflight(settings, store, checks))
    print("Checks:")
    checks.print()
    if checks.ok:
        print("Ready: nothing blocks the call." if args.to or args.softphone else "Ready.")
        return EXIT_OK
    print("Not ready: fix the FAIL lines (docs/TESTING.md, docs/ASTERISK.md).", file=sys.stderr)
    return EXIT_RUNTIME


# ---------------------------------------------------------------------- parser


def register(sub: Any, calls_sub: Any) -> None:
    """The voice command group and the voice-related `calls` commands."""
    voice = sub.add_parser("voice", help="the voice engine: check, chat, simulate, download")
    voice_sub = voice.add_subparsers(dest="voice_command", required=True)
    v_check = voice_sub.add_parser("check", help="load the models and try each (no call)")
    v_check.add_argument("--no-llm", action="store_true", help="skip the dialogue model")
    v_check.add_argument("--save-wav", help="save the synthesized test phrase to this WAV file")
    v_check.set_defaults(func=cmd_voice_check)
    v_chat = voice_sub.add_parser("chat", help="type to the dialogue model (tune a script)")
    group = v_chat.add_mutually_exclusive_group()
    group.add_argument("--script", help="the call's task (default: the test call's script)")
    group.add_argument("--campaign", type=int, help="use this campaign's script")
    v_chat.set_defaults(func=cmd_voice_chat)
    v_sim = voice_sub.add_parser("simulate", help="a spoken conversation with a simulated caller")
    group = v_sim.add_mutually_exclusive_group()
    group.add_argument("--script", help="the call's task (default: the test call's script)")
    group.add_argument("--campaign", type=int, help="use this campaign's script")
    said = v_sim.add_mutually_exclusive_group()
    said.add_argument("--say", action="append", help="a caller's phrase (repeat for more)")
    said.add_argument("--wav", action="append", help="a caller's phrase as a 16-bit WAV file")
    v_sim.add_argument("--save-wav", help="save the conversation as a stereo WAV file")
    v_sim.set_defaults(func=cmd_voice_simulate)
    v_dl = voice_sub.add_parser(
        "download",
        help="the default Russian models (about 110 MB)",
        epilog=download_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    v_dl.add_argument("model", choices=[*MODEL_PACKAGES, "all"])
    v_dl.add_argument("--force", action="store_true", help="download again")
    v_dl.add_argument(
        "--from", dest="source", help="a folder with the already downloaded files (no internet)"
    )
    v_dl.set_defaults(func=cmd_voice_download)

    l_transcript = calls_sub.add_parser("transcript", help="what was said on a call")
    l_transcript.add_argument("attempt_id", type=int)
    l_transcript.add_argument("--delete", action="store_true", help="delete the transcript")
    l_transcript.set_defaults(func=cmd_calls_transcript)
    l_pre = calls_sub.add_parser("preflight", help="every check before a call (nothing dialed)")
    target = l_pre.add_mutually_exclusive_group()
    target.add_argument("--to", help="the number of the planned test call")
    target.add_argument("--softphone", action="store_true", help="a call to the local softphone")
    l_pre.set_defaults(func=cmd_calls_preflight)
