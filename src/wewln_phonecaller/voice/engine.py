"""One spoken conversation on an answered call: listen, understand, answer, end politely.

    greeting -> [caller speaks -> pause -> STT -> LLM (streamed) -> TTS per sentence -> play]*
             -> goodbye / [END] -> hang up

- Turn-taking: the caller's turn ends with a pause (VAD); the answer starts with the first
  finished sentence, while the rest is still being generated.
- Barge-in: when the caller speaks over wewln for `barge_in_ms`, playback stops at once, the
  reply being generated is dropped, and the caller is listened to. Only what was really
  played is remembered as said.
- Silence: after `no_input_s` without speech wewln asks again (`reprompt`), and after
  `max_reprompts` says goodbye and ends the call.
- Errors: a recognition failure asks the caller to repeat; a failure of the dialogue model or
  of speech synthesis ends the call with a fixed apology (synthesized in advance).
- The caller's hangup ends the session at once.

Transcripts reach the call service through `on_turn`; logs carry only sizes and timings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .audio import FRAME_BYTES, SILENCE_DBFS, dbfs, duration_ms, frames, silence
from .llm import ChatModel, LlmError
from .stt import SpeechToText, SttError
from .text import Marks, SentenceSplitter, asks_not_to_call, speakable
from .transport import MediaStream
from .tts import TextToSpeech, TtsError
from .vad import EnergyVad, VadConfig, VadEventKind

log = logging.getLogger("wewln.voice")

# Why a conversation ended.
CONVERSATION_END = "conversation_end"
OPT_OUT = "opt_out"
NO_INPUT = "no_input"
MAX_TURNS = "max_turns"
VOICE_ERROR = "voice_error"
CALLER_HANGUP = "caller_hangup"

READ_IDLE_S = 0.5


@dataclass(frozen=True)
class ConversationConfig:
    system_prompt: str
    greeting: str
    goodbye: str = "Спасибо за разговор! До свидания."
    reprompt: str = "Алло? Вы меня слышите?"
    sorry: str = "Извините, я не расслышал. Повторите, пожалуйста."
    failure: str = "Извините, у нас технические неполадки. Мы перезвоним позже. До свидания!"
    max_turns: int = 12
    no_input_s: float = 8.0
    max_reprompts: int = 2
    max_history: int = 16
    barge_in: bool = True
    barge_in_ms: int = 320
    # While wewln speaks, the caller's line must be this much louder to count as speech, and
    # no quieter than wewln's own voice minus `echo_return_db` (an echo of our own voice that
    # comes back over the line must not interrupt us).
    echo_guard_db: float = 6.0
    echo_return_db: float = 20.0
    # The echo comes back with a delay: the guard stays on this long after wewln stops.
    echo_tail_ms: int = 400
    vad: VadConfig = field(default_factory=VadConfig)
    # Playback pacing: one 20 ms frame per 20 ms (0 = as fast as possible, for tests), up to
    # `lead_frames` ahead to absorb timer jitter.
    frame_s: float = 0.02
    lead_frames: int = 3
    # After the last word, before the call is ended (lets the audio drain through Asterisk).
    tail_ms: int = 400
    stt_timeout_s: float = 15.0
    tts_timeout_s: float = 15.0
    max_stt_failures: int = 2


@dataclass
class Turn:
    seq: int
    role: str  # "assistant" | "caller"
    kind: str  # greeting, reply, reprompt, sorry, goodbye, failure | speech
    text: str
    at_ms: int
    interrupted: bool = False
    speech_ms: int | None = None
    stt_ms: int | None = None
    llm_ms: int | None = None
    tts_ms: int | None = None
    response_ms: int | None = None

    def metrics(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in {
                "role": self.role,
                "kind": self.kind,
                "chars": len(self.text),
                "interrupted": self.interrupted or None,
                "speech_ms": self.speech_ms,
                "stt_ms": self.stt_ms,
                "llm_ms": self.llm_ms,
                "tts_ms": self.tts_ms,
                "response_ms": self.response_ms,
            }.items()
            if v is not None
        }


@dataclass
class SessionResult:
    reason: str
    hangup: bool  # wewln must end the call (otherwise it has ended already)
    opt_out: bool
    turns: list[Turn]
    error: str | None = None

    @property
    def caller_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "caller")


class _Kind(StrEnum):
    SPEECH_START = "speech_start"
    NOISE = "noise"
    UTTERANCE = "utterance"
    BARGE_IN = "barge_in"
    CLOSED = "closed"


@dataclass(frozen=True)
class _Event:
    kind: _Kind
    audio: bytes = b""
    speech_ms: int = 0
    at: float = 0.0
    overlapped: bool = False


class _Inbox:
    def __init__(self) -> None:
        self._items: deque[_Event] = deque()
        self._ready = asyncio.Event()

    def put(self, event: _Event) -> None:
        self._items.append(event)
        self._ready.set()

    def push_front(self, events: list[_Event]) -> None:
        for event in reversed(events):
            self._items.appendleft(event)
        if self._items:
            self._ready.set()

    def pop(self) -> _Event | None:
        if not self._items:
            return None
        event = self._items.popleft()
        if not self._items:
            self._ready.clear()
        return event

    async def wait(self) -> None:
        await self._ready.wait()


class PhraseCache:
    """Synthesized fixed phrases (greeting, goodbye, apologies), reused between calls."""

    def __init__(self, tts: TextToSpeech, limit: int = 32) -> None:
        self.tts = tts
        self.limit = limit
        self._audio: dict[str, bytes] = {}

    async def get(self, text: str) -> bytes:
        cached = self._audio.get(text)
        if cached is not None:
            return cached
        audio = await self.tts.synthesize(speakable(text))
        if len(self._audio) >= self.limit:
            self._audio.pop(next(iter(self._audio)))
        self._audio[text] = audio
        return audio


class Player:
    """Sends queued audio to the call in real time; can be stopped at once (barge-in)."""

    def __init__(self, media: MediaStream, *, frame_s: float = 0.02, lead_frames: int = 3):
        self.media = media
        self.frame_s = frame_s
        self.lead_s = max(0, lead_frames) * frame_s
        self._queue: deque[tuple[bytes, int | None]] = deque()
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._play_until = 0.0
        self._task: asyncio.Task[None] | None = None
        self.completed_mark = -1
        self.frames_sent = 0
        self.first_sound_at: float | None = None
        # Levels of the last frames sent (0.8 s: an echo comes back with some delay).
        self._levels: deque[float] = deque(maxlen=40)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def playing(self) -> bool:
        return bool(self._queue) or self._loop.time() < self._play_until

    @property
    def quiet_s(self) -> float:
        """Seconds since the last audio was played (0 while playing)."""
        if self._queue:
            return 0.0
        return max(0.0, self._loop.time() - self._play_until)

    @property
    def recent_dbfs(self) -> float:
        """The loudest of the frames sent lately (silence when nothing was sent)."""
        return max(self._levels, default=SILENCE_DBFS)

    def enqueue(self, pcm: bytes, mark: int | None = None) -> None:
        chunks = list(frames(pcm))
        if not chunks:
            if mark is not None and not self._queue:
                self.completed_mark = mark
            return
        for index, chunk in enumerate(chunks):
            self._queue.append((chunk, mark if index == len(chunks) - 1 else None))
        self._wake.set()

    def flush(self) -> None:
        self._queue.clear()
        self._play_until = min(self._play_until, self._loop.time())

    def begin_batch(self) -> None:
        self.first_sound_at = None
        self.completed_mark = -1

    async def drained(self) -> None:
        while self._queue:
            if self._task is None or self._task.done():
                self._queue.clear()
                break
            self._wake.set()
            await asyncio.sleep(max(self.frame_s, 0.005))
        remaining = self._play_until - self._loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _run(self) -> None:
        next_at: float | None = None
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            now = self._loop.time()
            if next_at is None or now - next_at > 0.2:
                next_at = now  # (re)start the clock after a pause or a stall
            ahead = next_at - now
            if ahead > self.lead_s:
                await asyncio.sleep(ahead - self.lead_s)
            if not self._queue:  # flushed while waiting
                continue
            chunk, mark = self._queue.popleft()
            if self.first_sound_at is None:
                self.first_sound_at = max(self._loop.time(), next_at)
            await self.media.write(chunk)
            self._levels.append(dbfs(chunk))
            self.frames_sent += 1
            if mark is not None:
                self.completed_mark = mark
            next_at += self.frame_s
            self._play_until = next_at
            if self.frame_s == 0:
                await asyncio.sleep(0)


@dataclass
class _Reply:
    sentences: list[str] = field(default_factory=list)
    marks: Marks = field(default_factory=Marks)
    llm_at: float | None = None
    tts_at: float | None = None
    outcome: str = "done"  # done | barge_in | closed | error
    error: str | None = None


TurnSink = Callable[[Turn], Awaitable[None]]


class ConversationSession:
    def __init__(
        self,
        media: MediaStream,
        *,
        stt: SpeechToText,
        llm: ChatModel,
        tts: TextToSpeech,
        config: ConversationConfig,
        phrases: PhraseCache | None = None,
        on_turn: TurnSink | None = None,
        label: str = "",
    ) -> None:
        self.media = media
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.cfg = config
        self.phrases = phrases or PhraseCache(tts)
        self.on_turn = on_turn
        self.label = label
        self._vad = EnergyVad(config.vad)
        self._inbox = _Inbox()
        self._history: list[dict[str, str]] = []
        self._turns: list[Turn] = []
        self._opt_out = False
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._started = self._loop.time()
        self._player: Player | None = None

    # ------------------------------------------------------------------ helpers

    def _ms(self, start: float | None, end: float | None) -> int | None:
        if start is None or end is None:
            return None
        return max(0, int((end - start) * 1000))

    def _now_ms(self) -> int:
        return int((self._loop.time() - self._started) * 1000)

    async def _record(self, turn: Turn) -> None:
        self._turns.append(turn)
        log.info("voice_turn", extra={"call": self.label, **turn.metrics()})
        if self.on_turn is not None:
            try:
                await self.on_turn(turn)
            except Exception:
                log.exception("voice_turn_not_saved", extra={"call": self.label})

    def _turn(
        self, role: str, kind: str, text: str, *, at_ms: int | None = None, **values: Any
    ) -> Turn:
        return Turn(
            seq=len(self._turns) + 1,
            role=role,
            kind=kind,
            text=text,
            at_ms=self._now_ms() if at_ms is None else at_ms,
            **values,
        )

    def _result(self, reason: str, *, hangup: bool, error: str | None = None) -> SessionResult:
        log.info(
            "voice_session_end",
            extra={"call": self.label, "reason": reason, "turns": len(self._turns)},
        )
        return SessionResult(reason, hangup, self._opt_out, list(self._turns), error)

    @property
    def player(self) -> Player:
        if self._player is None:
            raise RuntimeError("the session is not running")
        return self._player

    # ------------------------------------------------------------------ listening

    async def _read_loop(self) -> None:
        buffer = b""
        barge_sent = False
        while True:
            try:
                chunk = await asyncio.wait_for(self.media.read(), READ_IDLE_S)
            except TimeoutError:
                # No audio arrives while the line is silent on some bridges: count it as
                # silence, so that the end of a turn is still found.
                chunk = silence(int(READ_IDLE_S * 1000))
            if chunk is None:
                self._closed = True
                self._inbox.put(_Event(_Kind.CLOSED))
                return
            buffer += chunk
            while len(buffer) >= FRAME_BYTES:
                frame, buffer = buffer[:FRAME_BYTES], buffer[FRAME_BYTES:]
                playing = self._player is not None and self._player.playing
                echo = playing or (
                    self._player is not None and self._player.quiet_s * 1000 < self.cfg.echo_tail_ms
                )
                self._vad.extra_db = self.cfg.echo_guard_db if echo else 0.0
                self._vad.floor_dbfs = (
                    self.player.recent_dbfs - self.cfg.echo_return_db if echo else None
                )
                event = self._vad.process(frame)
                if event is not None:
                    if event.kind == VadEventKind.SPEECH_START:
                        barge_sent = False
                        self._inbox.put(_Event(_Kind.SPEECH_START, at=self._loop.time()))
                    elif event.kind == VadEventKind.NOISE:
                        self._inbox.put(_Event(_Kind.NOISE))
                    else:
                        self._inbox.put(
                            _Event(
                                _Kind.UTTERANCE,
                                audio=event.audio,
                                speech_ms=event.speech_ms,
                                at=self._loop.time(),
                                overlapped=playing,
                            )
                        )
                if (
                    playing
                    and not barge_sent
                    and self._vad.in_speech
                    and self._vad.speech_ms >= self.cfg.barge_in_ms
                ):
                    barge_sent = True
                    self._inbox.put(_Event(_Kind.BARGE_IN, at=self._loop.time()))

    async def _next_utterance(self) -> _Event | None:
        """The caller's next utterance; a CLOSED event at hangup; None after the silence
        limit (time while the caller is speaking does not count)."""
        deadline = self._loop.time() + self.cfg.no_input_s
        while True:
            event = self._inbox.pop()
            if event is not None:
                if event.kind in (_Kind.CLOSED, _Kind.UTTERANCE):
                    return event
                if event.kind == _Kind.NOISE:
                    deadline = self._loop.time() + self.cfg.no_input_s
                continue
            if self._vad.in_speech:
                await self._inbox.wait()  # until the utterance ends
                deadline = self._loop.time() + self.cfg.no_input_s
                continue
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                return None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._inbox.wait(), remaining)

    # ------------------------------------------------------------------ speaking

    async def _watch(self, task: asyncio.Task[Any], *, interruptible: bool) -> str:
        """Wait for speaking to finish; "done", "barge_in" or "closed". Events for later (the
        hangup, an utterance that ended after our voice) stay in the inbox."""
        held: list[_Event] = []
        try:
            while True:
                waiter = asyncio.ensure_future(self._inbox.wait())
                done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter not in done:
                    waiter.cancel()
                event = self._inbox.pop()
                while event is not None:
                    if event.kind == _Kind.CLOSED:
                        held.append(event)
                        await self._cancel(task)
                        return "closed"
                    if event.kind == _Kind.BARGE_IN and interruptible and not task.done():
                        await self._cancel(task)
                        self.player.flush()
                        return "barge_in"
                    if event.kind == _Kind.UTTERANCE and not event.overlapped:
                        held.append(event)  # spoken after our voice ended: answered next
                    # Speech over our voice that did not interrupt (a short "ага") is dropped.
                    event = self._inbox.pop()
                if task in done:
                    return "done"
        finally:
            self._inbox.push_front(held)

    async def _cancel(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _play_task(self, audio: bytes) -> None:
        self.player.enqueue(audio, mark=0)
        await self.player.drained()

    async def _say(self, text: str, kind: str, *, interruptible: bool = True) -> str:
        """A fixed phrase. Returns "done", "barge_in", "closed" or "error"."""
        try:
            audio = await asyncio.wait_for(self.phrases.get(text), self.cfg.tts_timeout_s)
        except (TtsError, TimeoutError):
            log.error("voice_tts_failed", extra={"call": self.label, "kind": kind})
            return "error"
        self.player.begin_batch()
        at_ms = self._now_ms()
        task = asyncio.create_task(self._play_task(audio))
        outcome = await self._watch(task, interruptible=interruptible and self.cfg.barge_in)
        interrupted = outcome == "barge_in"
        await self._record(
            self._turn("assistant", kind, text, at_ms=at_ms, interrupted=interrupted)
        )
        self._history.append({"role": "assistant", "content": text + ("…" if interrupted else "")})
        return outcome

    async def _generate(self, messages: list[dict[str, str]], reply: _Reply) -> None:
        splitter = SentenceSplitter()

        async def speak(sentence: str) -> None:
            text = speakable(sentence)
            if not text:
                return
            audio = await asyncio.wait_for(self.tts.synthesize(text), self.cfg.tts_timeout_s)
            if reply.tts_at is None:
                reply.tts_at = self._loop.time()
            self.player.enqueue(audio, mark=len(reply.sentences))
            reply.sentences.append(text)

        async for piece in self.llm.stream(messages):
            if reply.llm_at is None:
                reply.llm_at = self._loop.time()
            for sentence in splitter.feed(piece):
                await speak(sentence)
        for sentence in splitter.flush():
            await speak(sentence)
        reply.marks = splitter.marks
        if not reply.sentences and not reply.marks.end:
            raise LlmError("the dialogue model gave an empty answer", code="llm_empty")
        await self.player.drained()

    async def _reply(self, heard_at: float) -> _Reply:
        messages = [{"role": "system", "content": self.cfg.system_prompt}]
        messages += self._history[-self.cfg.max_history :]
        reply = _Reply()
        self.player.begin_batch()
        at_ms = self._now_ms()
        task = asyncio.create_task(self._generate(messages, reply))
        reply.outcome = await self._watch(task, interruptible=self.cfg.barge_in)
        if reply.outcome == "done" and task.exception() is not None:
            exc = task.exception()
            reply.outcome = "error"
            reply.error = getattr(exc, "code", None) or type(exc).__name__
            if isinstance(exc, TimeoutError):
                reply.error = "tts_timeout"
            elif isinstance(exc, TtsError):
                reply.error = "tts_error"
        spoken = reply.sentences
        if reply.outcome != "done":
            count = self.player.completed_mark + 1
            if reply.outcome == "barge_in" and self.player.first_sound_at is not None:
                count += 1  # the sentence that was being said
            spoken = reply.sentences[:count]
        text = " ".join(spoken)
        interrupted = reply.outcome == "barge_in"
        if text or interrupted:
            await self._record(
                self._turn(
                    "assistant",
                    "reply",
                    text,
                    at_ms=at_ms,
                    interrupted=interrupted,
                    llm_ms=self._ms(heard_at, reply.llm_at),
                    tts_ms=self._ms(reply.llm_at, reply.tts_at),
                    response_ms=self._ms(heard_at, self.player.first_sound_at),
                )
            )
        if text:
            self._history.append(
                {"role": "assistant", "content": text + ("…" if interrupted else "")}
            )
        return reply

    async def _fail(self, error: str) -> SessionResult:
        log.error("voice_failed", extra={"call": self.label, "error": error})
        if not self._closed:
            await self._say(self.cfg.failure, "failure", interruptible=False)
            await asyncio.sleep(self.cfg.tail_ms / 1000)
        return self._result(VOICE_ERROR, hangup=not self._closed, error=error)

    async def _goodbye(self, reason: str) -> SessionResult:
        outcome = await self._say(self.cfg.goodbye, "goodbye", interruptible=False)
        if outcome == "closed":
            return self._result(CALLER_HANGUP, hangup=False)
        await asyncio.sleep(self.cfg.tail_ms / 1000)
        return self._result(reason, hangup=True)

    # ------------------------------------------------------------------ the conversation

    async def run(self) -> SessionResult:
        self._player = Player(
            self.media, frame_s=self.cfg.frame_s, lead_frames=self.cfg.lead_frames
        )
        self._player.start()
        reader = asyncio.create_task(self._read_loop())
        try:
            return await self._converse()
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            await self._player.stop()
            with contextlib.suppress(Exception):
                await self.media.close()

    async def _converse(self) -> SessionResult:
        cfg = self.cfg
        outcome = await self._say(cfg.greeting, "greeting")
        if outcome == "closed":
            return self._result(CALLER_HANGUP, hangup=False)
        if outcome == "error":
            return self._result(VOICE_ERROR, hangup=True, error="tts_error")
        reprompts = 0
        stt_failures = 0
        while True:
            event = await self._next_utterance()
            if event is not None and event.kind == _Kind.CLOSED:
                return self._result(CALLER_HANGUP, hangup=False)
            if event is None:
                if reprompts >= cfg.max_reprompts:
                    return await self._goodbye(NO_INPUT)
                reprompts += 1
                outcome = await self._say(cfg.reprompt, "reprompt")
                if outcome == "closed":
                    return self._result(CALLER_HANGUP, hangup=False)
                continue
            heard_at = event.at
            try:
                text = await asyncio.wait_for(self.stt.transcribe(event.audio), cfg.stt_timeout_s)
            except (SttError, TimeoutError) as exc:
                stt_failures += 1
                log.warning(
                    "voice_stt_failed", extra={"call": self.label, "error": type(exc).__name__}
                )
                if stt_failures > cfg.max_stt_failures:
                    return await self._fail("stt_error")
                outcome = await self._say(cfg.sorry, "sorry")
                if outcome == "closed":
                    return self._result(CALLER_HANGUP, hangup=False)
                continue
            if not text:
                continue  # noise, or nothing intelligible
            stt_failures = 0
            reprompts = 0
            await self._record(
                self._turn(
                    "caller",
                    "speech",
                    text,
                    at_ms=max(0, self._ms(self._started, heard_at) - duration_ms(event.audio)),
                    speech_ms=event.speech_ms,
                    stt_ms=self._ms(heard_at, self._loop.time()),
                )
            )
            if asks_not_to_call(text):
                self._opt_out = True
            self._history.append({"role": "user", "content": text})
            if sum(1 for t in self._turns if t.role == "caller") > cfg.max_turns:
                return await self._goodbye(MAX_TURNS)
            reply = await self._reply(heard_at)
            if reply.outcome == "closed":
                return self._result(CALLER_HANGUP, hangup=False)
            if reply.outcome == "error":
                return await self._fail(reply.error or "llm_error")
            if reply.marks.opt_out:
                self._opt_out = True
            if reply.outcome == "done" and reply.marks.end:
                if not reply.sentences:
                    return await self._goodbye(OPT_OUT if self._opt_out else CONVERSATION_END)
                await asyncio.sleep(cfg.tail_ms / 1000)
                reason = OPT_OUT if self._opt_out else CONVERSATION_END
                return self._result(reason, hangup=True)
