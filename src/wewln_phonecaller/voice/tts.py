"""Speech synthesis: text to 8 kHz PCM for the phone. Everything runs locally.

- `piper`: Piper voices (ONNX) in-process via the `piper-tts` package. The Russian voices
  ru_RU-denis / dmitri / irina / ruslan (medium, ~63 MB each) speak at 22.05 kHz and are
  resampled to 8 kHz. Note the licenses: piper-tts 1.3+ is GPL-3.0; the voices' own licenses
  are listed in each voice's MODEL_CARD.
- `piper-http`: Piper's HTTP server (`python -m piper.http_server -m <voice>`) as a separate
  program: POST /synthesize {"text": ...} returns a WAV file.

Each sentence is synthesized as soon as the dialogue model has finished it.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import aiohttp

from .audio import SAMPLE_RATE, read_wav, resample

log = logging.getLogger("wewln.voice.tts")


class TtsError(Exception):
    """Speech synthesis is not possible (missing package or voice, server error)."""


class TextToSpeech(ABC):
    name = "tts"

    async def load(self) -> None:  # noqa: B027 - optional hook
        """Load the voice (done once before the first call)."""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """8 kHz 16-bit mono PCM of `text`."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release the voice or the HTTP session."""

    def describe(self) -> dict[str, Any]:
        return {"tts": self.name}


class PiperTts(TextToSpeech):
    name = "piper"

    def __init__(
        self,
        model_path: str | Path,
        *,
        speaker: int | None = None,
        length_scale: float | None = None,
        use_cuda: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.speaker = speaker
        self.length_scale = length_scale
        self.use_cuda = use_cuda
        self._voice: Any = None
        self._config: Any = None
        self._lock = asyncio.Lock()

    def describe(self) -> dict[str, Any]:
        return {"tts": self.name, "model": str(self.model_path)}

    def _load_sync(self) -> None:
        try:
            from piper import PiperVoice  # type: ignore[import-not-found]
        except ImportError:
            try:
                from piper.voice import PiperVoice  # type: ignore[import-not-found]
            except ImportError:
                raise TtsError(
                    'Piper is not installed: pip install "wewln-phonecaller[piper]"'
                ) from None
        if not self.model_path.is_file():
            raise TtsError(f"no Piper voice at {self.model_path} (the .onnx file)")
        config = Path(f"{self.model_path}.json")
        if not config.is_file():
            raise TtsError(f"the voice's settings file is missing: {config.name}")
        try:
            self._voice = PiperVoice.load(str(self.model_path), str(config), use_cuda=self.use_cuda)
        except Exception as exc:
            raise TtsError(f"the Piper voice could not be loaded ({exc})") from None
        try:  # piper-tts >= 1.3
            from piper import SynthesisConfig  # type: ignore[import-not-found]

            self._config = SynthesisConfig(speaker_id=self.speaker, length_scale=self.length_scale)
        except ImportError:
            self._config = None

    async def load(self) -> None:
        if self._voice is None:
            await asyncio.to_thread(self._load_sync)

    def _synthesize_sync(self, text: str) -> bytes:
        voice = self._voice
        rate = int(getattr(getattr(voice, "config", None), "sample_rate", 22050))
        parts: list[bytes] = []
        if hasattr(voice, "synthesize_stream_raw"):  # piper-tts 1.2 (MIT)
            kwargs: dict[str, Any] = {}
            if self.speaker is not None:
                kwargs["speaker_id"] = self.speaker
            if self.length_scale is not None:
                kwargs["length_scale"] = self.length_scale
            parts.extend(voice.synthesize_stream_raw(text, **kwargs))
        else:  # piper-tts >= 1.3: one AudioChunk per sentence
            for chunk in voice.synthesize(text, syn_config=self._config):
                rate = int(chunk.sample_rate)
                parts.append(chunk.audio_int16_bytes)
        return resample(b"".join(parts), rate, SAMPLE_RATE)

    async def synthesize(self, text: str) -> bytes:
        await self.load()
        async with self._lock:  # one ONNX session; calls are serialized
            try:
                return await asyncio.to_thread(self._synthesize_sync, text)
            except TtsError:
                raise
            except Exception as exc:
                raise TtsError(f"Piper failed ({type(exc).__name__})") from None


class PiperHttpTts(TextToSpeech):
    name = "piper-http"

    def __init__(
        self,
        url: str,
        *,
        speaker: int | None = None,
        length_scale: float | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.speaker = speaker
        self.length_scale = length_scale
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def describe(self) -> dict[str, Any]:
        return {"tts": self.name, "url": self.url}

    async def load(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=3)
            )

    async def synthesize(self, text: str) -> bytes:
        await self.load()
        if self._session is None:  # pragma: no cover - load() creates it
            raise TtsError("the Piper client is closed")
        body: dict[str, Any] = {"text": text}
        if self.speaker is not None:
            body["speaker_id"] = self.speaker
        if self.length_scale is not None:
            body["length_scale"] = self.length_scale
        try:
            async with self._session.post(self.url + "/synthesize", json=body) as resp:
                if resp.status != 200:
                    raise TtsError(f"the Piper server answered HTTP {resp.status}")
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise TtsError(f"the Piper server is not reachable ({type(exc).__name__})") from None
        try:
            pcm, rate = read_wav(data)
        except ValueError as exc:
            raise TtsError(f"the Piper server sent no usable audio ({exc})") from None
        return resample(pcm, rate, SAMPLE_RATE)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
