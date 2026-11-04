"""Speech recognition: the caller's utterance (8 kHz PCM) to text. Everything runs locally.

- `vosk`: Kaldi models in-process (Apache-2.0). vosk-model-small-ru (45 MB) runs on any CPU;
  vosk-model-ru (1.8 GB) is far more accurate and needs several GB of RAM.
- `faster-whisper`: Whisper in-process via CTranslate2 (MIT); "small" or larger for Russian,
  int8 on a CPU, float16 on an NVIDIA GPU. Models are never downloaded unless allowed
  (WEWLN_VOICE_ALLOW_DOWNLOADS): give a local model folder.
- `whisper-http`: a whisper.cpp server (`whisper-server`, MIT) on this computer: POST
  /inference with a 16 kHz WAV file, the text comes back as JSON.

Optional packages are imported only when their backend is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import aiohttp

from .audio import SAMPLE_RATE, resample, to_float32_16k, wav_bytes
from .text import is_hallucination, normalize_transcript

log = logging.getLogger("wewln.voice.stt")


class SttError(Exception):
    """Speech recognition is not possible (missing package or model, server error)."""


class SpeechToText(ABC):
    name = "stt"

    async def load(self) -> None:  # noqa: B027 - optional hook
        """Load the model (slow; done once before the first call)."""

    @abstractmethod
    async def transcribe(self, pcm: bytes) -> str:
        """Text of an utterance (8 kHz 16-bit mono PCM); "" when nothing was understood."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release the model or the HTTP session."""

    def describe(self) -> dict[str, Any]:
        return {"stt": self.name}


def _language_code(language: str) -> str:
    return language.split("-", 1)[0].lower() or "ru"


def _model_rate(model_dir: Path) -> int:
    """The sample rate a Vosk model expects (conf/mfcc.conf), 16 kHz when not stated."""
    conf = model_dir / "conf" / "mfcc.conf"
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("--sample-frequency="):
                return int(float(line.split("=", 1)[1]))
    except (OSError, ValueError):
        pass
    return 16000


class VoskStt(SpeechToText):
    name = "vosk"

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._model: Any = None
        self._rate = 16000

    def describe(self) -> dict[str, Any]:
        return {"stt": self.name, "model": str(self.model_path), "sample_rate": self._rate}

    def _load_sync(self) -> None:
        try:
            import vosk  # type: ignore[import-not-found]
        except ImportError:
            raise SttError('Vosk is not installed: pip install "wewln-phonecaller[vosk]"') from None
        if not (self.model_path / "am").is_dir() and not (self.model_path / "conf").is_dir():
            raise SttError(
                f"no Vosk model in {self.model_path} (unpack e.g. vosk-model-small-ru-0.22 there)"
            )
        vosk.SetLogLevel(-1)
        self._rate = _model_rate(self.model_path)
        try:
            self._model = vosk.Model(str(self.model_path))
        except Exception as exc:  # vosk raises a bare Exception
            raise SttError(f"the Vosk model could not be loaded ({exc})") from None

    async def load(self) -> None:
        if self._model is None:
            await asyncio.to_thread(self._load_sync)

    def _transcribe_sync(self, pcm: bytes) -> str:
        import vosk  # type: ignore[import-not-found]

        recognizer = vosk.KaldiRecognizer(self._model, self._rate)
        recognizer.AcceptWaveform(resample(pcm, SAMPLE_RATE, self._rate))
        result = json.loads(recognizer.FinalResult() or "{}")
        return normalize_transcript(str(result.get("text") or ""))

    async def transcribe(self, pcm: bytes) -> str:
        await self.load()
        return await asyncio.to_thread(self._transcribe_sync, pcm)


class FasterWhisperStt(SpeechToText):
    name = "faster-whisper"

    def __init__(
        self,
        model: str,
        *,
        language: str = "ru-RU",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        beam_size: int = 1,
        allow_download: bool = False,
    ) -> None:
        self.model = model
        self.language = _language_code(language)
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.allow_download = allow_download
        self._model: Any = None

    def describe(self) -> dict[str, Any]:
        return {
            "stt": self.name,
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
        }

    def _load_sync(self) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError:
            raise SttError(
                'faster-whisper is not installed: pip install "wewln-phonecaller[whisper]"'
            ) from None
        is_dir = Path(self.model).is_dir()
        if not is_dir and not self.allow_download and ("/" in self.model or "\\" in self.model):
            raise SttError(f"no faster-whisper model folder at {self.model}")
        try:
            self._model = WhisperModel(
                self.model,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                local_files_only=not self.allow_download and not is_dir,
            )
        except Exception as exc:
            hint = (
                ""
                if self.allow_download or is_dir
                else " (models are not downloaded automatically: give a local folder, or set "
                "WEWLN_VOICE_ALLOW_DOWNLOADS=true once)"
            )
            raise SttError(f"the Whisper model could not be loaded: {exc}{hint}") from None

    async def load(self) -> None:
        if self._model is None:
            await asyncio.to_thread(self._load_sync)

    def _transcribe_sync(self, pcm: bytes) -> str:
        audio = to_float32_16k(pcm)
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
        )
        texts = []
        for segment in segments:
            if segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0:
                continue
            texts.append(segment.text)
        text = normalize_transcript(" ".join(texts))
        return "" if is_hallucination(text) else text

    async def transcribe(self, pcm: bytes) -> str:
        await self.load()
        return await asyncio.to_thread(self._transcribe_sync, pcm)


class WhisperHttpStt(SpeechToText):
    name = "whisper-http"

    def __init__(self, url: str, *, language: str = "ru-RU", timeout: float = 15.0) -> None:
        self.url = url.rstrip("/")
        self.language = _language_code(language)
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def describe(self) -> dict[str, Any]:
        return {"stt": self.name, "url": self.url}

    async def load(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=3)
            )

    async def transcribe(self, pcm: bytes) -> str:
        await self.load()
        if self._session is None:  # pragma: no cover - load() creates it
            raise SttError("the whisper client is closed")
        form = aiohttp.FormData()
        form.add_field(
            "file",
            wav_bytes(resample(pcm, SAMPLE_RATE, 16000), 16000),
            filename="utterance.wav",
            content_type="audio/wav",
        )
        form.add_field("response_format", "json")
        form.add_field("language", self.language)
        form.add_field("temperature", "0.0")
        try:
            async with self._session.post(self.url + "/inference", data=form) as resp:
                if resp.status != 200:
                    raise SttError(f"the whisper server answered HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise SttError(f"the whisper server is not reachable ({type(exc).__name__})") from None
        text = normalize_transcript(str((data or {}).get("text") or ""))
        return "" if is_hallucination(text) else text

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
