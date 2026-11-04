"""Voice activity detection: where the caller speaks, frame by frame (20 ms, 8 kHz).

An energy detector with an adaptive noise floor: a frame is speech when it is clearly louder
than the line noise (and louder than an absolute minimum). The noise floor is the quietest
level of the last `noise_window_ms` (speech always has quieter gaps between syllables, steady
noise does not), so a noisy line stops counting as speech after a second or two. It needs no
model and no extra package and reacts within a few frames (needed for barge-in). Short clicks
are ignored; the end of a turn is a pause of `end_silence_ms`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from .audio import FRAME_BYTES, FRAME_MS, dbfs


@dataclass(frozen=True)
class VadConfig:
    # This much speech in a row starts an utterance.
    start_ms: int = 160
    # A pause this long ends it (the caller finished the turn).
    end_silence_ms: int = 700
    # Utterances with less speech than this are noise (a click, a cough) and are dropped.
    min_speech_ms: int = 240
    # A monologue is cut here and answered (a stuck line or background talk).
    max_utterance_ms: int = 15000
    # Audio kept from before the start, so that the first syllable is not lost.
    pre_roll_ms: int = 240
    # Speech is this many dB above the noise floor ...
    threshold_db: float = 10.0
    # ... and above this absolute level.
    min_level_dbfs: float = -48.0
    # The noise floor starts here and follows the quietest level of the recent audio.
    initial_noise_dbfs: float = -65.0
    noise_window_ms: int = 1500
    # The floor never rises above this (a line this noisy is unusable anyway).
    max_noise_dbfs: float = -30.0


class VadEventKind(StrEnum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    # What started as speech was too short (a click, a cough): no utterance.
    NOISE = "noise"


@dataclass(frozen=True)
class VadEvent:
    kind: VadEventKind
    audio: bytes = b""
    speech_ms: int = 0


class EnergyVad:
    def __init__(self, config: VadConfig | None = None) -> None:
        self.cfg = config or VadConfig()
        self.noise_dbfs = self.cfg.initial_noise_dbfs
        self.extra_db = 0.0  # raised while wewln speaks (echo must not count as speech)
        self.floor_dbfs: float | None = None  # and frames this quiet are not speech then
        self._pre: deque[bytes] = deque(maxlen=max(1, self.cfg.pre_roll_ms // FRAME_MS))
        self._recent: deque[float] = deque(maxlen=max(1, self.cfg.noise_window_ms // FRAME_MS))
        self._run = 0  # speech frames in a row before the start
        self._buffer: list[bytes] = []
        self._speech_frames = 0
        self._silence_run = 0
        self.in_speech = False
        self.last_level = self.cfg.initial_noise_dbfs

    @property
    def speech_ms(self) -> int:
        """Speech so far in the current utterance (0 outside one)."""
        return self._speech_frames * FRAME_MS if self.in_speech else self._run * FRAME_MS

    def reset(self) -> None:
        self._pre.clear()
        self._run = 0
        self._buffer = []
        self._speech_frames = 0
        self._silence_run = 0
        self.in_speech = False

    def is_speech(self, level: float) -> bool:
        threshold = max(self.cfg.min_level_dbfs, self.noise_dbfs + self.cfg.threshold_db)
        if self.in_speech:
            threshold -= 3.0  # hysteresis: words fade out
        if self.floor_dbfs is not None and level <= self.floor_dbfs:
            return False
        return level > threshold + self.extra_db

    def _track_noise(self, level: float) -> None:
        # The quietest recent level: fast down (a quieter line), slowly up (steady noise).
        self._recent.append(level)
        floor = min(self._recent)
        if floor < self.noise_dbfs:
            self.noise_dbfs += 0.3 * (floor - self.noise_dbfs)
        elif len(self._recent) == self._recent.maxlen:
            self.noise_dbfs += 0.05 * (floor - self.noise_dbfs)
        self.noise_dbfs = max(-90.0, min(self.noise_dbfs, self.cfg.max_noise_dbfs))

    def process(self, frame: bytes) -> VadEvent | None:
        if len(frame) != FRAME_BYTES:
            raise ValueError("the VAD takes 20 ms frames of 320 bytes")
        level = dbfs(frame)
        self.last_level = level
        speech = self.is_speech(level)
        self._track_noise(level)
        cfg = self.cfg
        if not self.in_speech:
            if not speech:
                self._run = 0
                self._pre.append(frame)
                return None
            self._run += 1
            self._pre.append(frame)
            if self._run * FRAME_MS < cfg.start_ms:
                return None
            self.in_speech = True
            self._buffer = list(self._pre)
            self._pre.clear()
            self._speech_frames = self._run
            self._silence_run = 0
            self._run = 0
            return VadEvent(VadEventKind.SPEECH_START, speech_ms=self._speech_frames * FRAME_MS)
        self._buffer.append(frame)
        if speech:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
        length_ms = len(self._buffer) * FRAME_MS
        if self._silence_run * FRAME_MS < cfg.end_silence_ms and length_ms < cfg.max_utterance_ms:
            return None
        speech_ms = self._speech_frames * FRAME_MS
        # Keep a short tail of the pause; the rest is silence.
        keep_tail = min(self._silence_run, max(1, 200 // FRAME_MS))
        cut = len(self._buffer) - self._silence_run + keep_tail
        audio = b"".join(self._buffer[:cut])
        self.reset()
        if speech_ms < cfg.min_speech_ms:
            return VadEvent(VadEventKind.NOISE, speech_ms=speech_ms)
        return VadEvent(VadEventKind.SPEECH_END, audio=audio, speech_ms=speech_ms)
