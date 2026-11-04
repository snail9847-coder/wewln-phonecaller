"""PCM audio helpers: 16-bit signed little-endian mono, 8 kHz on the phone side.

Telephone audio (Asterisk `slin`, AudioSocket) is 8000 samples/s, 2 bytes per sample, sent in
20 ms frames of 320 bytes. Speech recognizers want 16 kHz; Piper voices speak at 16-22 kHz.
numpy makes resampling fast and is used when installed (every speech backend brings it); a
pure-Python fallback keeps the core free of dependencies.
"""

from __future__ import annotations

import io
import math
import sys
import wave
from array import array
from collections.abc import Iterator

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH  # 320
_BIG_ENDIAN = sys.byteorder == "big"
SILENCE_DBFS = -96.0

try:  # pragma: no cover - depends on the environment
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is optional
    _np = None


def _samples(pcm: bytes) -> array:
    data = array("h")
    data.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if _BIG_ENDIAN:  # pragma: no cover - x86 and ARM are little-endian
        data.byteswap()
    return data


def _to_bytes(data: array) -> bytes:
    if _BIG_ENDIAN:  # pragma: no cover
        data = array("h", data)
        data.byteswap()
    return data.tobytes()


def duration_ms(pcm: bytes, rate: int = SAMPLE_RATE) -> int:
    return len(pcm) // SAMPLE_WIDTH * 1000 // rate


def silence(ms: int, rate: int = SAMPLE_RATE) -> bytes:
    return bytes(rate * ms // 1000 * SAMPLE_WIDTH)


def frames(pcm: bytes, size: int = FRAME_BYTES) -> Iterator[bytes]:
    """Fixed-size frames; the last one is padded with silence."""
    for start in range(0, len(pcm), size):
        chunk = pcm[start : start + size]
        if len(chunk) < size:
            chunk += bytes(size - len(chunk))
        yield chunk


def rms(pcm: bytes) -> float:
    samples = _samples(pcm)
    if not samples:
        return 0.0
    if _np is not None and len(samples) > 64:
        values = _np.frombuffer(pcm[: len(samples) * 2], dtype="<i2").astype(_np.float64)
        return float(math.sqrt(float(_np.mean(values * values))))
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def dbfs(pcm: bytes) -> float:
    """Level relative to full scale: about -90 for digital silence, -20 for normal speech."""
    level = rms(pcm)
    if level <= 0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20 * math.log10(level / 32768.0))


def tone(freq: float, ms: int, *, rate: int = SAMPLE_RATE, level: float = 0.3) -> bytes:
    """A sine tone (tests, CI and the fake speech engines)."""
    count = rate * ms // 1000
    amplitude = max(0.0, min(1.0, level)) * 32767
    step = 2 * math.pi * freq / rate
    data = array("h", (int(amplitude * math.sin(step * i)) for i in range(count)))
    return _to_bytes(data)


def gain(pcm: bytes, factor: float) -> bytes:
    data = _samples(pcm)
    out = array("h", (max(-32768, min(32767, int(s * factor))) for s in data))
    return _to_bytes(out)


def zero_crossings_per_s(pcm: bytes, rate: int = SAMPLE_RATE) -> float:
    """Rough dominant frequency x2 of a tone (used by live tests to recognise a test tone)."""
    data = _samples(pcm)
    if len(data) < 2:
        return 0.0
    crossings = sum(1 for a, b in zip(data, data[1:], strict=False) if (a < 0) != (b < 0))
    return crossings * rate / len(data)


# ---------------------------------------------------------------------- resampling


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Change the sample rate (low-pass filtered when going down)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    if _np is not None:
        return _resample_numpy(pcm, src_rate, dst_rate)
    return _resample_python(pcm, src_rate, dst_rate)


def _lowpass_taps(cutoff: float, count: int = 63) -> list[float]:
    """Windowed-sinc low-pass FIR; `cutoff` in cycles per sample (0 < cutoff < 0.5)."""
    middle = (count - 1) / 2
    taps = []
    for n in range(count):
        x = n - middle
        sinc = 2 * cutoff if x == 0 else math.sin(2 * math.pi * cutoff * x) / (math.pi * x)
        window = 0.54 - 0.46 * math.cos(2 * math.pi * n / (count - 1))
        taps.append(sinc * window)
    total = sum(taps)
    return [t / total for t in taps]


def _resample_numpy(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    np = _np
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
    if dst_rate < src_rate:
        taps = np.asarray(_lowpass_taps(0.45 * dst_rate / src_rate), dtype=np.float32)
        x = np.convolve(x, taps, mode="same")
    count = int(round(len(x) * dst_rate / src_rate))
    if count <= 0:
        return b""
    positions = np.arange(count, dtype=np.float64) * (src_rate / dst_rate)
    y = np.interp(positions, np.arange(len(x), dtype=np.float64), x)
    if dst_rate > src_rate:
        # Interpolation leaves mirror images of the band above the old Nyquist frequency;
        # recognizers hear them as hiss (telephone audio is recognized much worse with them).
        taps = np.asarray(_lowpass_taps(0.45 * src_rate / dst_rate), dtype=np.float64)
        y = np.convolve(y, taps, mode="same")
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def _resample_python(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    data = _samples(pcm)
    if not data:
        return b""
    values: list[float] = [float(s) for s in data]
    if dst_rate < src_rate:
        # A short moving average as the anti-aliasing filter (cheap, adequate for speech).
        width = max(1, round(src_rate / dst_rate))
        acc = 0.0
        smoothed = []
        for i, v in enumerate(values):
            acc += v
            if i >= width:
                acc -= values[i - width]
            smoothed.append(acc / min(i + 1, width))
        values = smoothed
    count = int(round(len(values) * dst_rate / src_rate))
    step = src_rate / dst_rate
    last = len(values) - 1
    out = array("h")
    for i in range(count):
        pos = i * step
        left = int(pos)
        if left >= last:
            sample = values[last]
        else:
            frac = pos - left
            sample = values[left] * (1 - frac) + values[left + 1] * frac
        out.append(max(-32768, min(32767, int(round(sample)))))
    return _to_bytes(out)


def to_float32_16k(pcm8k: bytes) -> object:
    """numpy float32 array in [-1, 1] at 16 kHz (Whisper's input). Needs numpy."""
    if _np is None:
        raise RuntimeError("numpy is required for this speech recognizer")
    pcm16k = resample(pcm8k, SAMPLE_RATE, 16000)
    return _np.frombuffer(pcm16k, dtype="<i2").astype(_np.float32) / 32768.0


# ---------------------------------------------------------------------- WAV files


def wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def read_wav(data: bytes) -> tuple[bytes, int]:
    """(16-bit mono PCM, sample rate) from WAV bytes; stereo is mixed down."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a readable WAV file ({exc})") from None
    if width != SAMPLE_WIDTH:
        raise ValueError(f"only 16-bit WAV is supported (got {width * 8}-bit)")
    if channels == 1:
        return raw, rate
    samples = _samples(raw)
    mono = array(
        "h",
        (
            int(sum(samples[i : i + channels]) / channels)
            for i in range(0, len(samples) - channels + 1, channels)
        ),
    )
    return _to_bytes(mono), rate


def interleave(left: bytes, right: bytes) -> bytes:
    """Two mono tracks as one stereo track (conversation recordings: caller | assistant)."""
    a, b = _samples(left), _samples(right)
    length = max(len(a), len(b))
    a.extend([0] * (length - len(a)))
    b.extend([0] * (length - len(b)))
    out = array("h", [0] * (2 * length))
    out[0::2] = a
    out[1::2] = b
    return _to_bytes(out)
