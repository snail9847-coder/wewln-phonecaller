"""The configured voice engine: backends built from the settings, loaded once, reused per call."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..errors import WewlnError
from .engine import ConversationConfig, ConversationSession, PhraseCache, TurnSink
from .llm import ChatModel, LlmError, OllamaChat, OpenAiChat
from .phrases import DEFAULT_TEST_SCRIPT, SYSTEM_PROMPT
from .stt import FasterWhisperStt, SpeechToText, SttError, VoskStt, WhisperHttpStt
from .transport import MediaStream
from .tts import PiperHttpTts, PiperTts, TextToSpeech, TtsError
from .vad import VadConfig

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger("wewln.voice")


class VoiceUnavailableError(WewlnError):
    code = "voice_unavailable"
    http_status = 503


def system_prompt(script: str | None) -> str:
    text = (script or "").strip() or DEFAULT_TEST_SCRIPT
    return SYSTEM_PROMPT.format(script=text[:4000])


@dataclass
class VoiceRuntime:
    stt: SpeechToText
    llm: ChatModel
    tts: TextToSpeech
    config: ConversationConfig
    test_script: str = DEFAULT_TEST_SCRIPT
    phrases: PhraseCache = field(init=False)
    startup: dict[str, Any] = field(default_factory=dict)
    _started: bool = False

    def __post_init__(self) -> None:
        self.phrases = PhraseCache(self.tts)

    def describe(self) -> dict[str, Any]:
        return {**self.stt.describe(), **self.llm.describe(), **self.tts.describe()}

    async def start(self) -> None:
        """Load the models and synthesize the fixed phrases; raises VoiceUnavailableError."""
        if self._started:
            return
        timings: dict[str, Any] = {}
        try:
            started = time.perf_counter()
            await self.stt.load()
            timings["stt_load_s"] = round(time.perf_counter() - started, 2)
            started = time.perf_counter()
            await self.tts.load()
            timings["tts_load_s"] = round(time.perf_counter() - started, 2)
            check = await self.llm.check()
            if check.get("status") != "ok":
                raise LlmError(str(check.get("error") or "the dialogue model is not available"))
            started = time.perf_counter()
            await self.llm.warm(system_prompt(self.test_script))
            timings["llm_load_s"] = round(time.perf_counter() - started, 2)
            started = time.perf_counter()
            cfg = self.config
            for text in (cfg.greeting, cfg.goodbye, cfg.reprompt, cfg.sorry, cfg.failure):
                await self.phrases.get(text)
            timings["phrases_s"] = round(time.perf_counter() - started, 2)
        except (SttError, TtsError, LlmError) as exc:
            raise VoiceUnavailableError(f"the voice engine cannot start: {exc}") from None
        self.startup = timings
        self._started = True
        log.info("voice_ready", extra=timings)

    async def close(self) -> None:
        for component in (self.stt, self.llm, self.tts):
            try:
                await component.close()
            except Exception:  # pragma: no cover - closing must not fail the shutdown
                log.exception("voice_close_failed")
        self._started = False

    def session(
        self,
        media: MediaStream,
        *,
        script: str | None = None,
        on_turn: TurnSink | None = None,
        label: str = "",
        **overrides: Any,
    ) -> ConversationSession:
        config = replace(
            self.config, system_prompt=system_prompt(script or self.test_script), **overrides
        )
        return ConversationSession(
            media,
            stt=self.stt,
            llm=self.llm,
            tts=self.tts,
            config=config,
            phrases=self.phrases,
            on_turn=on_turn,
            label=label,
        )


def build_stt(settings: Settings) -> SpeechToText:
    if settings.voice_stt == "vosk":
        return VoskStt(settings.resolved_stt_model)
    if settings.voice_stt == "faster-whisper":
        return FasterWhisperStt(
            settings.resolved_stt_model,
            language=settings.default_language,
            device=settings.voice_stt_device,
            compute_type=settings.voice_stt_compute_type,
            allow_download=settings.voice_allow_downloads,
        )
    return WhisperHttpStt(settings.voice_stt_url, language=settings.default_language)


def build_llm(settings: Settings) -> ChatModel:
    common: dict[str, Any] = {
        "temperature": settings.voice_llm_temperature,
        "max_tokens": settings.voice_llm_max_tokens,
        "first_token_timeout": settings.voice_llm_timeout,
        "timeout": settings.voice_llm_timeout * 2,
    }
    if settings.voice_llm == "openai":
        key = settings.voice_llm_api_key
        return OpenAiChat(
            settings.voice_llm_url,
            settings.voice_llm_model,
            api_key=key.get_secret_value() if key else None,
            **common,
        )
    return OllamaChat(
        settings.voice_llm_url, settings.voice_llm_model, think=settings.voice_llm_think, **common
    )


def build_tts(settings: Settings) -> TextToSpeech:
    if settings.voice_tts == "piper-http":
        return PiperHttpTts(
            settings.voice_tts_url,
            speaker=settings.voice_tts_speaker,
            length_scale=settings.voice_tts_length_scale,
        )
    return PiperTts(
        settings.resolved_tts_model,
        speaker=settings.voice_tts_speaker,
        length_scale=settings.voice_tts_length_scale,
    )


def conversation_config(settings: Settings) -> ConversationConfig:
    return ConversationConfig(
        system_prompt=system_prompt(None),
        greeting=settings.voice_greeting,
        goodbye=settings.voice_goodbye,
        max_turns=settings.voice_max_turns,
        no_input_s=settings.voice_no_input_timeout,
        barge_in=settings.voice_barge_in,
        vad=VadConfig(end_silence_ms=settings.voice_end_silence_ms),
    )


def create_voice_runtime(settings: Settings) -> VoiceRuntime:
    """The engine for WEWLN_VOICE_MODE=ai (models are loaded by start())."""
    problems = settings.voice_problems
    if problems:
        raise VoiceUnavailableError("; ".join(problems))
    return VoiceRuntime(
        stt=build_stt(settings),
        llm=build_llm(settings),
        tts=build_tts(settings),
        config=conversation_config(settings),
        test_script=settings.voice_test_script,
    )
