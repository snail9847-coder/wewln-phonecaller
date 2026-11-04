"""The conversation engine on an in-memory line: interrupting the assistant, and its echo."""

import asyncio
import contextlib
from collections import deque

import pytest

from voice_fakes import FAST_FRAME_S, FakeLlm, FakeStt, FakeTts, fast_config
from wewln_phonecaller.voice.audio import dbfs, frames, gain, silence, tone
from wewln_phonecaller.voice.engine import (
    CONVERSATION_END,
    NO_INPUT,
    ConversationSession,
    SessionResult,
)
from wewln_phonecaller.voice.transport import MediaStream, memory_pair

# About 6 s of speech (1.5 s at the tests' fast pace).
LONG = "Здравствуйте! Это тестовый звонок: мы хотим задать вам пару коротких вопросов о сервисе."


def audio_s(ms: int) -> float:
    """Real seconds for `ms` of audio at the tests' fast pace."""
    return ms / 20 * FAST_FRAME_S


class Phone:
    """The phone's end of the line, a 20 ms frame per tick: hears the assistant, says what the
    test gives it and can send back an echo of the assistant's voice."""

    def __init__(self, media: MediaStream, *, echo_gain: float | None = None, echo_ms: int = 0):
        self.media = media
        self.echo_gain = echo_gain
        self.echo_s = audio_s(echo_ms)
        self.voice_ms = 0  # the assistant's voice heard
        self.last_voice_at = 0.0
        self._outgoing: deque[bytes] = deque()
        self._echo: deque[tuple[float, bytes]] = deque()
        self._heard = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def say(self, pcm: bytes) -> None:
        self._outgoing.extend(frames(pcm))

    async def wait_for_voice(self, ms: int = 20) -> None:
        """Until the assistant has said `ms` more."""
        target = self.voice_ms + ms
        while self.voice_ms < target:
            self._heard.clear()
            await self._heard.wait()

    async def wait_for_quiet(self, ms: int) -> None:
        """Until the assistant, having spoken, has been quiet for `ms`."""
        if self.voice_ms == 0:
            await self.wait_for_voice()
        while True:
            remaining = self.last_voice_at + audio_s(ms) - self._loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _listen(self) -> None:
        while (chunk := await self.media.read()) is not None:
            now = self._loop.time()
            for frame in frames(chunk):
                if dbfs(frame) > -40:
                    self.voice_ms += 20
                    self.last_voice_at = now
                if self.echo_gain is not None:
                    self._echo.append((now + self.echo_s, gain(frame, self.echo_gain)))
            self._heard.set()

    def _next_frame(self, now: float) -> bytes:
        if self._outgoing:
            return self._outgoing.popleft()
        if self._echo and self._echo[0][0] <= now:
            return self._echo.popleft()[1]
        return silence(20)

    async def run(self) -> None:
        listener = asyncio.create_task(self._listen())
        next_at = self._loop.time()
        try:
            # Paced by the clock (like the assistant's player), whatever the timer resolution.
            while not listener.done():
                now = self._loop.time()
                while next_at <= now and not listener.done():
                    await self.media.write(self._next_frame(now))
                    next_at += FAST_FRAME_S
                await asyncio.sleep(max(0.0, next_at - self._loop.time()))
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener


async def talk(session: ConversationSession, phone: Phone, script=None) -> SessionResult:
    """Run the conversation; `script(phone)` acts on the phone meanwhile."""
    phone_task = asyncio.create_task(phone.run())
    session_task = asyncio.create_task(session.run())
    try:
        if script is not None:
            await asyncio.wait_for(script(phone), 20)
        return await asyncio.wait_for(session_task, 20)
    finally:
        for task in (session_task, phone_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def kinds(result: SessionResult) -> list[str]:
    return [t.kind for t in result.turns]


async def test_speaking_over_the_greeting_interrupts_it():
    call, line = memory_pair()
    stt = FakeStt(["подождите, а кто это?"])
    llm = FakeLlm(["Это тестовый звонок. До свидания! [END]"])
    tts = FakeTts(ms_per_char=70, max_ms=8000)
    session = ConversationSession(
        call, stt=stt, llm=llm, tts=tts, config=fast_config(greeting=LONG)
    )
    phone = Phone(line)

    async def script(phone: Phone) -> None:
        await phone.wait_for_voice(200)
        phone.say(tone(200, 800))

    result = await talk(session, phone, script)
    assert kinds(result) == ["greeting", "speech", "reply"]
    assert result.turns[0].interrupted and not result.turns[2].interrupted
    assert result.reason == CONVERSATION_END
    # The greeting stopped at once: much less than its 6 s was heard (the reply is 2 s).
    assert phone.voice_ms < 5000
    assert llm.requests[0][1] == {"role": "assistant", "content": LONG + "…"}


async def test_speaking_over_a_reply_interrupts_it():
    call, line = memory_pair()
    first = "Это тестовый звонок от компании, мы проверяем, как работает голосовой помощник."
    stt = FakeStt(["кто это?", "понятно, хватит"])
    llm = FakeLlm([first + " Он отвечает на вопросы.", "Хорошо, до свидания! [END]"])
    tts = FakeTts(ms_per_char=70, max_ms=8000)
    session = ConversationSession(call, stt=stt, llm=llm, tts=tts, config=fast_config())
    phone = Phone(line)

    async def script(phone: Phone) -> None:
        await phone.wait_for_quiet(300)  # after the greeting
        phone.say(tone(200, 600))
        await phone.wait_for_voice(600 + 400)  # into the reply
        phone.say(tone(200, 800))

    result = await talk(session, phone, script)
    assert kinds(result) == ["greeting", "speech", "reply", "speech", "reply"]
    interrupted = result.turns[2]
    # What was being said when the caller spoke is kept; the rest was never said.
    assert interrupted.interrupted and interrupted.text == first
    assert {"role": "assistant", "content": first + "…"} in llm.requests[1]
    assert result.reason == CONVERSATION_END


@pytest.mark.parametrize(
    ("barge_in", "speech_ms"),
    [(True, 260), (False, 800)],
    ids=["a short yes", "barge-in off"],
)
async def test_speech_over_the_greeting_that_does_not_interrupt_is_dropped(barge_in, speech_ms):
    call, line = memory_pair()
    stt = FakeStt(["ага"])
    tts = FakeTts(ms_per_char=70, max_ms=8000)
    config = fast_config(greeting=LONG, barge_in=barge_in, no_input_s=0.5, max_reprompts=0)
    session = ConversationSession(call, stt=stt, llm=FakeLlm(), tts=tts, config=config)
    phone = Phone(line)

    async def script(phone: Phone) -> None:
        await phone.wait_for_voice(200)
        phone.say(tone(200, speech_ms))

    result = await talk(session, phone, script)
    assert kinds(result) == ["greeting", "goodbye"]
    assert not result.turns[0].interrupted
    assert stt.heard_ms == [] and result.reason == NO_INPUT


@pytest.mark.parametrize("echo_ms", [0, 320], ids=["at once", "320 ms later"])
async def test_an_echo_of_the_assistants_voice_is_not_speech(echo_ms):
    call, line = memory_pair()
    stt = FakeStt(["эхо"])
    # The guard after speaking is in real time: 200 ms at the fast pace is 800 ms of audio.
    config = fast_config(no_input_s=0.6, max_reprompts=0, echo_tail_ms=200)
    session = ConversationSession(call, stt=stt, llm=FakeLlm(), tts=FakeTts(), config=config)
    # 26 dB quieter than the assistant: loud enough to count as speech on a quiet line.
    phone = Phone(line, echo_gain=0.05, echo_ms=echo_ms)

    result = await talk(session, phone)
    assert kinds(result) == ["greeting", "goodbye"]
    assert not result.turns[0].interrupted
    assert stt.heard_ms == [] and result.reason == NO_INPUT
