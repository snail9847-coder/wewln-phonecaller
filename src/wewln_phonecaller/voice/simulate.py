"""A simulated caller: the far end of a call that never reaches a phone network.

It answers like a person: waits until the assistant stops talking, says its next phrase
(synthesized speech, so recognition really runs), and hangs up after its last phrase. Between
phrases it sends line silence in real time, as a phone line does. Both directions are
recorded, so the conversation can be listened to as a stereo WAV file (left: the caller,
right: the assistant).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .audio import FRAME_BYTES, FRAME_MS, frames, interleave, wav_bytes
from .transport import MediaStream
from .tts import TextToSpeech

DEFAULT_PHRASES = (
    "Алло, слушаю.",
    "Да, удобно. А что вы хотели?",
    "Понятно, спасибо. До свидания.",
)


@dataclass
class SimulatedCaller:
    phone: MediaStream
    phrases: list[str]
    tts: TextToSpeech
    frame_s: float = 0.02
    # The assistant's silence after which the caller speaks.
    pause_ms: int = 1500
    # How long the caller waits for the assistant to start, and to answer, at most.
    patience_s: float = 20.0
    hangup_after: bool = True
    caller_track: list[bytes] = field(default_factory=list)
    assistant_track: list[bytes] = field(default_factory=list)
    said: list[str] = field(default_factory=list)

    async def _listen(self, heard: asyncio.Queue[bytes | None]) -> None:
        while True:
            chunk = await self.phone.read()
            await heard.put(chunk)
            if chunk is None:
                return

    async def run(self) -> None:
        heard: asyncio.Queue[bytes | None] = asyncio.Queue()
        listener = asyncio.create_task(self._listen(heard))
        loop = asyncio.get_running_loop()
        silence = bytes(FRAME_BYTES)
        pending = b""
        queue = list(self.phrases)
        speaking: list[bytes] = []
        assistant_spoke = False
        quiet_ms = 0
        waited_ms = 0
        next_at = loop.time()
        try:
            while True:
                # Everything the assistant sent since the last tick.
                closed = False
                while not heard.empty():
                    chunk = heard.get_nowait()
                    if chunk is None:
                        closed = True
                        break
                    pending += chunk
                if closed:
                    return
                got = False
                while len(pending) >= FRAME_BYTES:
                    self.assistant_track.append(pending[:FRAME_BYTES])
                    pending = pending[FRAME_BYTES:]
                    got = True
                if got:
                    assistant_spoke = True
                    quiet_ms = 0
                else:
                    self.assistant_track.append(silence)
                    quiet_ms += FRAME_MS
                if speaking:
                    frame = speaking.pop(0)
                    waited_ms = 0
                else:
                    frame = silence
                    waited_ms += FRAME_MS
                    ready = (assistant_spoke and quiet_ms >= self.pause_ms) or (
                        waited_ms >= self.patience_s * 1000
                    )
                    if ready and not got:
                        if not queue:
                            if self.hangup_after:
                                return
                        else:
                            text = queue.pop(0)
                            audio = await self.tts.synthesize(text)
                            self.said.append(text)
                            speaking = list(frames(audio))
                            assistant_spoke = False
                            waited_ms = 0
                await self.phone.write(frame)
                self.caller_track.append(frame)
                next_at += self.frame_s
                delay = next_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    if delay < -0.2:
                        next_at = loop.time()
                    await asyncio.sleep(0)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
            await self.phone.close()

    def recording(self) -> bytes:
        """Stereo WAV (8 kHz): left the caller, right the assistant."""
        return wav_bytes(
            interleave(b"".join(self.caller_track), b"".join(self.assistant_track)), channels=2
        )


def simulated_person(
    tts: TextToSpeech, phrases: Sequence[str] = DEFAULT_PHRASES, *, frame_s: float = 0.02
) -> Callable[[Any, MediaStream], Awaitable[None]]:
    """The person on an answered mock call (telephony.mock: `caller`): says `phrases`."""

    async def person(_request: Any, phone: MediaStream) -> None:
        await SimulatedCaller(phone, list(phrases), tts, frame_s=frame_s).run()

    return person
