"""`wewln voice ...` and `wewln calls transcript|preflight` with fake models (nothing dialed)."""

import asyncio
import hashlib
import io
import wave
import zipfile
from pathlib import Path

import pytest

from voice_fakes import FakeLlm, FakeStt, FakeTts, fake_runtime
from wewln_phonecaller import cli_voice
from wewln_phonecaller.cli import main
from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE, Settings
from wewln_phonecaller.voice import runtime as voice_runtime

PHONE = "+994501234567"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WEWLN_DEFAULT_PHONE_REGION", "AZ")
    return tmp_path


@pytest.fixture
def models_ready(monkeypatch):
    """The voice packages and models count as installed (the fakes stand in for them)."""
    monkeypatch.setattr(Settings, "voice_problems", property(lambda self: []))


@pytest.fixture
def fakes(monkeypatch, models_ready):
    made = {"llm": FakeLlm(["Это тестовый звонок, проверка связи."])}
    monkeypatch.setattr(cli_voice, "build_tts", lambda settings: FakeTts())
    monkeypatch.setattr(
        cli_voice, "build_stt", lambda settings: FakeStt([cli_voice.CHECK_PHRASE.lower()])
    )
    monkeypatch.setattr(cli_voice, "build_llm", lambda settings: made["llm"])
    return made


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


async def run_async(capsys, *argv):
    code = await asyncio.to_thread(main, list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_voice_check_tries_each_model(env, fakes, capsys):
    wav = env / "check.wav"
    code, out, _ = run(capsys, "voice", "check", "--save-wav", str(wav))
    assert code == 0, out
    assert "WEWLN_VOICE_MODE=off" in out  # works before the engine is switched on
    assert out.count("[ok]") == 3
    assert "(100% of the words)" in out
    assert "Это тестовый звонок, проверка связи." in out
    assert "Voice check passed." in out
    with wave.open(str(wav)) as saved:
        assert (saved.getframerate(), saved.getnchannels()) == (8000, 1)
    # The dialogue model got the test call's instructions and a question.
    (request,) = fakes["llm"].requests
    assert request[0]["role"] == "system" and "тестовый звонок" in request[0]["content"]
    assert request[-1] == {"role": "user", "content": cli_voice.CHECK_QUESTION}


def test_voice_check_reports_a_dialogue_model_that_is_down(env, fakes, capsys):
    fakes["llm"] = FakeLlm(status="Ollama is not reachable at http://127.0.0.1:11434")
    code, out, _ = run(capsys, "voice", "check")
    assert code == 1
    assert "[FAIL] Ollama is not reachable" in out
    code, out, _ = run(capsys, "voice", "check", "--no-llm")
    assert code == 0
    assert "Dialogue model" not in out


def test_voice_check_names_what_is_missing(env, monkeypatch, capsys):
    # Without the fakes: no Vosk model and no Piper voice in the data folder.
    monkeypatch.setattr("wewln_phonecaller.config.importlib.util.find_spec", lambda name: object())
    code, _, err = run(capsys, "voice", "check")
    assert code == 2
    assert "wewln voice download vosk" in err
    assert "wewln voice download piper" in err


def test_voice_chat_is_a_text_conversation(env, fakes, monkeypatch, capsys):
    fakes["llm"] = FakeLlm(["Да, слышу вас хорошо.", "Хорошо, больше не позвоним. [OPT_OUT] [END]"])
    lines = iter(["Алло, кто это?", "Не звоните мне больше"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    code, out, _ = run(capsys, "voice", "chat", "--script", "Пригласи на вебинар в пятницу.")
    assert code == 0
    assert "wewln: Да, слышу вас хорошо." in out
    assert "wewln: Хорошо, больше не позвоним." in out
    assert "[OPT_OUT]" not in out
    assert "DNC list" in out
    assert "ended the conversation" in out
    first = fakes["llm"].requests[0]
    assert "Пригласи на вебинар в пятницу." in first[0]["content"]
    assert first[1]["role"] == "assistant"  # the greeting comes first, as on a call


def test_voice_chat_uses_a_campaign_script(env, fakes, monkeypatch, capsys):
    assert run(capsys, "campaign", "create", "Вебинар", "--script", "Скажи про вебинар.")[0] == 0
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert run(capsys, "voice", "chat", "--campaign", "1")[0] == 0
    code, _, err = run(capsys, "voice", "chat", "--campaign", "7")
    assert code == 1 and "Error" in err


def test_voice_simulate_runs_a_spoken_conversation(env, models_ready, monkeypatch, capsys):
    runtime = fake_runtime(heard=["алло", "да удобно"], replies=["Это проверка.", "Спасибо! [END]"])
    monkeypatch.setattr(cli_voice, "create_voice_runtime", lambda settings: runtime)
    wav = env / "talk.wav"
    code, out, _ = run(
        capsys,
        "voice",
        "simulate",
        "--say",
        "Алло.",
        "--say",
        "Да, удобно.",
        "--save-wav",
        str(wav),
    )
    assert code == 0, out
    assert "Conversation: conversation_end, 2 caller phrase(s)" in out
    assert "caller: алло" in out
    assert "wewln: Это проверка." in out and "wewln: Спасибо!" in out
    assert "Answer delay:" in out
    with wave.open(str(wav)) as saved:
        assert saved.getnchannels() == 2


def test_voice_simulate_can_say_wav_files(env, models_ready, monkeypatch, capsys):
    from wewln_phonecaller.voice.audio import tone, wav_bytes

    phrase = env / "phrase.wav"
    phrase.write_bytes(wav_bytes(tone(300.0, 600, rate=16000), rate=16000))
    runtime = fake_runtime(heard=["алло"], replies=["Слышу вас. [END]"])
    monkeypatch.setattr(cli_voice, "create_voice_runtime", lambda settings: runtime)
    code, out, _ = run(capsys, "voice", "simulate", "--wav", str(phrase))
    assert code == 0, out
    assert "caller: алло" in out
    # The recognizer got the file's audio at the line's rate (8 kHz): about 0.6 s.
    assert 500 <= runtime.stt.heard_ms[0] <= 1500


def test_dialing_with_the_engine_on_needs_the_models(env, monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_VOICE_MODE", "ai")
    code, _, err = run(capsys, "calls", "test", "--softphone")
    assert code == 2
    assert "Problem:" in err and "wewln voice download" in err


def test_a_mock_test_call_talks_and_keeps_a_transcript(env, models_ready, monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_VOICE_MODE", "ai")
    runtime = fake_runtime(
        heard=["алло слушаю", "да удобно"], replies=["Спасибо, до свидания! [END]"]
    )
    monkeypatch.setattr(voice_runtime, "create_voice_runtime", lambda settings: runtime)
    code, out, _ = run(capsys, "calls", "test", "--softphone")
    assert code == 0, out
    assert "call audio: in memory, with a simulated caller" in out
    assert "Conversation: conversation_end" in out
    assert "wewln calls transcript 1" in out
    code, out, _ = run(capsys, "calls", "transcript", "1")
    assert code == 0
    assert "Call 1 (test, completed): conversation_end" in out
    assert "caller: алло слушаю" in out
    assert "wewln: Спасибо, до свидания!" in out
    code, out, _ = run(capsys, "calls", "transcript", "1", "--delete")
    assert code == 0 and "Deleted" in out
    code, out, _ = run(capsys, "calls", "transcript", "1")
    assert "no transcript" in out
    code, _, err = run(capsys, "calls", "transcript", "9")
    assert code == 1


def test_preflight_with_the_mock_provider(env, models_ready, monkeypatch, capsys):
    code, out, _ = run(capsys, "calls", "preflight", "--to", PHONE)
    assert code == 0, out
    assert "nothing is dialed" in out
    assert "not needed: the mock provider simulates calls" in out
    assert "off (WEWLN_VOICE_MODE=off)" in out
    monkeypatch.setenv("WEWLN_VOICE_MODE", "ai")
    monkeypatch.setattr(cli_voice, "create_voice_runtime", lambda settings: fake_runtime())
    code, out, _ = run(capsys, "calls", "preflight", "--softphone")
    assert code == 0, out
    assert "voice engine" in out and "loaded: recognition" in out
    assert "Ready: nothing blocks the call." in out
    # Nothing was dialed.
    assert run(capsys, "calls", "list")[1].strip().endswith("(nothing)")


@pytest.fixture
def asterisk_env(env, ari_sim, monkeypatch):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    for name, value in {
        "WEWLN_MODE": "production",
        "WEWLN_PRODUCTION_CONFIRM": PRODUCTION_CONFIRM_PHRASE,
        "WEWLN_TELEPHONY_PROVIDER": "asterisk",
        "WEWLN_ARI_URL": ari_sim.base_url,
        "WEWLN_ARI_PASSWORD": ari_sim.password,
        "WEWLN_CALL_REGIONS": "AZ",
        "WEWLN_VOICE_MODE": "ai",
        "WEWLN_VOICE_AUDIOSOCKET_PORT": str(port),
    }.items():
        monkeypatch.setenv(name, value)
    return env


async def test_preflight_checks_the_call_audio_through_asterisk(
    asterisk_env, ari_sim, models_ready, monkeypatch, capsys
):
    monkeypatch.setattr(cli_voice, "create_voice_runtime", lambda settings: fake_runtime())
    code, out, err = await run_async(capsys, "calls", "preflight", "--to", PHONE)
    # No permit yet: the gate blocks a real call; everything else is checked and passes.
    assert code == 1, out + err
    assert "blocked by: operator_not_armed" in out
    assert "audiosocket" in out and "Running" in out
    assert "Asterisk reached 127.0.0.1:" in out
    # A test audio channel came and went; no call was placed.
    assert ari_sim.media_connects == 1
    assert not [r for r in ari_sim.requests if r[0] == "POST" and "/ari/channels/wewln-" in r[1]]
    assert run(capsys, "calls", "permit", "--to", PHONE, "--confirm", PHONE[-4:])[0] == 0
    code, out, err = await run_async(capsys, "calls", "preflight", "--to", PHONE)
    assert code == 0, out + err
    assert "open: a permit is in force" in out
    ari_sim.media_behavior = "refuse"
    code, out, _ = await run_async(capsys, "calls", "preflight", "--softphone")
    assert code == 1
    assert "[FAIL] call audio" in out


# ---------------------------------------------------------------------- model downloads


def vosk_zip(folder="vosk-model-small-ru-0.22", extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{folder}/am/final.mdl", b"model")
        zf.writestr(f"{folder}/conf/mfcc.conf", b"--sample-frequency=16000\n")
        if extra:
            zf.writestr(extra, b"x")
    return buffer.getvalue()


def package_for(data, name="vosk-model-small-ru-0.22.zip"):
    item = cli_voice.ModelFile(
        f"https://example.invalid/{name}", name, len(data), hashlib.sha256(data).hexdigest()
    )
    return cli_voice.ModelPackage("test model", (item,), "vosk-model-small-ru-0.22", unpack=True)


@pytest.fixture
def served(monkeypatch):
    """urlopen serves these bytes (no network); records the URLs asked for."""
    asked = []

    def install(data):
        def fake_urlopen(url, timeout=None):
            asked.append(url)
            return io.BytesIO(data)

        monkeypatch.setattr(cli_voice, "urlopen", fake_urlopen)
        return asked

    return install


def test_download_checks_and_unpacks_the_model(env, served, monkeypatch, capsys):
    data = vosk_zip()
    monkeypatch.setitem(cli_voice.MODEL_PACKAGES, "vosk", package_for(data))
    asked = served(data)
    code, out, _ = run(capsys, "voice", "download", "vosk")
    assert code == 0, out
    models = env / "data" / "models"
    assert (models / "vosk-model-small-ru-0.22" / "am" / "final.mdl").read_bytes() == b"model"
    assert not list(models.glob("*.zip")) and not list(models.glob("*.part"))
    assert asked == ["https://example.invalid/vosk-model-small-ru-0.22.zip"]
    code, out, _ = run(capsys, "voice", "download", "vosk")
    assert code == 0 and "Already there" in out and len(asked) == 1
    assert run(capsys, "voice", "download", "vosk", "--force")[0] == 0
    assert len(asked) == 2


def test_a_damaged_download_installs_nothing(env, served, monkeypatch, capsys):
    data = vosk_zip()
    monkeypatch.setitem(cli_voice.MODEL_PACKAGES, "vosk", package_for(data))
    served(data[:-1] + b"!")
    code, _, err = run(capsys, "voice", "download", "vosk")
    assert code == 1
    assert "SHA-256 mismatch" in err
    models = env / "data" / "models"
    assert list(models.iterdir()) == []


def test_an_archive_may_not_write_outside_its_folder(env, served, monkeypatch, capsys):
    data = vosk_zip(extra="../evil.txt")
    monkeypatch.setitem(cli_voice.MODEL_PACKAGES, "vosk", package_for(data))
    served(data)
    code, _, err = run(capsys, "voice", "download", "vosk")
    assert code == 1
    assert "unexpected path" in err
    assert not (env / "data" / "evil.txt").exists()
    assert not (env / "data" / "models" / "vosk-model-small-ru-0.22").exists()


def test_download_from_a_folder_without_internet(env, monkeypatch, capsys):
    data = vosk_zip()
    monkeypatch.setitem(cli_voice.MODEL_PACKAGES, "vosk", package_for(data))

    def no_network(url, timeout=None):
        raise AssertionError("no download expected")

    monkeypatch.setattr(cli_voice, "urlopen", no_network)
    source = env / "downloads"
    source.mkdir()
    (source / "vosk-model-small-ru-0.22.zip").write_bytes(data)
    code, out, _ = run(capsys, "voice", "download", "vosk", "--from", str(source))
    assert code == 0, out
    assert (env / "data" / "models" / "vosk-model-small-ru-0.22" / "conf").is_dir()


def test_only_https_downloads(tmp_path):
    item = cli_voice.ModelFile("http://example.invalid/x", "x", 1, "0" * 64)
    with pytest.raises(cli_voice.DownloadError, match="only HTTPS"):
        cli_voice.fetch(item, tmp_path / "x")


def test_the_default_models_are_pinned():
    for package in cli_voice.MODEL_PACKAGES.values():
        for item in package.files:
            assert item.url.startswith("https://")
            assert len(item.sha256) == 64 and item.size > 0
    piper = cli_voice.MODEL_PACKAGES["piper"]
    # A fixed revision of the voice repository, not a moving branch.
    assert all("/resolve/main/" not in f.url for f in piper.files)
    assert Path(piper.files[1].name).suffix == ".json"


def test_word_match():
    assert (
        cli_voice.word_match(
            "Здравствуйте! Как вы меня слышите?", "здравствуйте как вы меня слышите"
        )
        == 1.0
    )
    assert cli_voice.word_match("Ёлка зелёная", "елка") == 0.5
    assert cli_voice.word_match("", "что-то") == 1.0


def test_download_help_lists_the_files_for_downloading_by_hand(capsys):
    with pytest.raises(SystemExit):
        main(["voice", "download", "--help"])
    out = capsys.readouterr().out
    for package in cli_voice.MODEL_PACKAGES.values():
        for item in package.files:
            assert item.url in out and item.sha256 in out
