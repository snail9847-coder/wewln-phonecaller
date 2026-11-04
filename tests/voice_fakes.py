"""Stand-ins for the speech and dialogue models: deterministic and instant, no packages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any

from wewln_phonecaller.voice.audio import tone
from wewln_phonecaller.voice.engine import ConversationConfig
from wewln_phonecaller.voice.llm import ChatModel, LlmError
from wewln_phonecaller.voice.runtime import VoiceRuntime, system_prompt
from wewln_phonecaller.voice.stt import SpeechToText, SttError
from wewln_phonecaller.voice.tts import TextToSpeech, TtsError
from wewln_phonecaller.voice.vad import VadConfig


class FakeTts(TextToSpeech):
    """A tone whose length follows the text (about 40 ms per character, 0.3-2.5 s)."""

    name = "fake-tts"

    def __init__(
        self,
        freq: float = 440.0,
        *,
        fail: bool = False,
        ms_per_char: int = 40,
        max_ms: int = 2500,
    ):
        self.freq = freq
        self.fail = fail
        self.ms_per_char = ms_per_char
        self.max_ms = max_ms
        self.spoken: list[str] = []
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def synthesize(self, text: str) -> bytes:
        if self.fail:
            raise TtsError("synthesis failed")
        self.spoken.append(text)
        return tone(self.freq, max(300, min(self.max_ms, self.ms_per_char * len(text))))


class FakeStt(SpeechToText):
    """Hears the given texts in order, one per utterance."""

    name = "fake-stt"

    def __init__(self, texts: Iterable[str] = (), *, fail: int = 0, delay: float = 0.0):
        self.texts = list(texts)
        self.fail = fail
        self.delay = delay
        self.heard_ms: list[int] = []
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def transcribe(self, pcm: bytes) -> str:
        self.heard_ms.append(len(pcm) // 16)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            self.fail -= 1
            raise SttError("recognition failed")
        return self.texts.pop(0) if self.texts else ""


class FakeLlm(ChatModel):
    """Replies from a list, word by word; a reply may be an exception to raise."""

    name = "fake-llm"
    model = "fake"

    def __init__(self, replies: Iterable[Any] = (), *, word_delay: float = 0.0, status="ok"):
        self.replies = list(replies)
        self.word_delay = word_delay
        self.status = status
        self.requests: list[list[dict[str, str]]] = []
        self.warmed = False

    async def warm(self, prompt: str | None = None) -> None:
        self.warmed = True

    async def check(self) -> dict[str, Any]:
        if self.status != "ok":
            return {"llm": self.name, "status": "error", "error": self.status}
        return {"llm": self.name, "status": "ok"}

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        self.requests.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else "Хорошо. [END]"
        if isinstance(reply, BaseException):
            raise reply
        for word in reply.split(" "):
            if self.word_delay:
                await asyncio.sleep(self.word_delay)
            yield word + " "


# Audio runs four times faster than real time in these tests (5 ms per 20 ms frame).
FAST_FRAME_S = 0.005


def fast_config(**overrides: Any) -> ConversationConfig:
    """Fast pacing with short pauses, so that a conversation takes a second or two."""
    values: dict[str, Any] = {
        "system_prompt": system_prompt(None),
        "frame_s": FAST_FRAME_S,
        "greeting": "Здравствуйте! Удобно говорить?",
        "goodbye": "До свидания!",
        "no_input_s": 2.0,
        "tail_ms": 20,
        "vad": VadConfig(end_silence_ms=400),
    }
    values.update(overrides)
    return ConversationConfig(**values)


def fake_runtime(
    *,
    heard: Iterable[str] = (),
    replies: Iterable[Any] = (),
    config: ConversationConfig | None = None,
    llm: ChatModel | None = None,
    stt: SpeechToText | None = None,
    tts: TextToSpeech | None = None,
) -> VoiceRuntime:
    return VoiceRuntime(
        stt=stt or FakeStt(heard),
        llm=llm or FakeLlm(replies),
        tts=tts or FakeTts(),
        config=config or fast_config(),
    )


__all__ = [
    "FAST_FRAME_S",
    "FakeLlm",
    "FakeStt",
    "FakeTts",
    "LlmError",
    "fake_runtime",
    "fast_config",
]
