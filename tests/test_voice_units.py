"""The voice engine's building blocks: audio, voice activity, text, the AudioSocket protocol."""

import asyncio
import struct
import uuid

import pytest

from wewln_phonecaller.voice import audio, transport
from wewln_phonecaller.voice.audio import FRAME_BYTES, dbfs, frames, gain, silence, tone
from wewln_phonecaller.voice.text import (
    SentenceSplitter,
    asks_not_to_call,
    is_hallucination,
    speakable,
    take_marks,
)
from wewln_phonecaller.voice.transport import (
    HANGUP_MESSAGE,
    KIND_AUDIO,
    AudioSocketServer,
    audio_message,
    read_message,
    uuid_message,
)
from wewln_phonecaller.voice.vad import EnergyVad, VadConfig, VadEventKind

# ---------------------------------------------------------------------- audio


def test_frames_levels_and_durations():
    pcm = tone(440.0, 50)
    chunks = list(frames(pcm))
    assert [len(c) for c in chunks] == [FRAME_BYTES] * 3  # 50 ms -> three 20 ms frames
    assert chunks[-1].endswith(bytes(FRAME_BYTES // 2))  # the last one padded with silence
    assert audio.duration_ms(silence(1500)) == 1500
    assert dbfs(silence(20)) == audio.SILENCE_DBFS
    loud, quiet = dbfs(tone(440.0, 100, level=0.5)), dbfs(gain(tone(440.0, 100, level=0.5), 0.1))
    assert -10 < loud < -6
    assert abs((loud - quiet) - 20) < 0.5


@pytest.mark.parametrize("use_numpy", [False, True])
def test_resampling_keeps_length_and_pitch(monkeypatch, use_numpy):
    if use_numpy:
        pytest.importorskip("numpy")
    else:
        monkeypatch.setattr(audio, "_np", None)
    pcm = tone(300.0, 1000, rate=22050)
    down = audio.resample(pcm, 22050, 8000)
    assert abs(audio.duration_ms(down) - 1000) <= 2
    # The tone keeps its pitch: about 600 zero crossings per second for 300 Hz.
    assert 560 < audio.zero_crossings_per_s(down) < 640
    up = audio.resample(down, 8000, 16000)
    assert abs(audio.duration_ms(up, 16000) - 1000) <= 2
    assert audio.resample(pcm, 8000, 8000) is pcm
    with pytest.raises(ValueError):
        audio.resample(pcm, 0, 8000)


def test_wav_files_round_trip_and_stereo_is_mixed_down():
    pcm = tone(440.0, 200)
    assert audio.read_wav(audio.wav_bytes(pcm)) == (pcm, 8000)
    stereo = audio.wav_bytes(audio.interleave(pcm, silence(200)), channels=2)
    mono, rate = audio.read_wav(stereo)
    assert rate == 8000 and len(mono) == len(pcm)
    assert abs(dbfs(mono) - (dbfs(pcm) - 6.0)) < 0.5  # half of one channel
    with pytest.raises(ValueError, match="WAV"):
        audio.read_wav(b"not a wav file")


# ---------------------------------------------------------------------- voice activity


def feed(vad, pcm):
    return [e for e in (vad.process(f) for f in frames(pcm)) if e is not None]


def test_speech_between_pauses_is_one_utterance():
    vad = EnergyVad(VadConfig(end_silence_ms=400))
    assert feed(vad, silence(500)) == []
    events = feed(vad, tone(300.0, 600) + silence(600))
    assert [e.kind for e in events] == [VadEventKind.SPEECH_START, VadEventKind.SPEECH_END]
    end = events[-1]
    assert 560 <= end.speech_ms <= 620
    # The audio keeps the start of the speech (pre-roll) and only a short tail of the pause.
    assert 600 <= audio.duration_ms(end.audio) <= 600 + 240 + 200


def test_clicks_and_short_noises_are_not_utterances():
    vad = EnergyVad()
    assert feed(vad, silence(300) + tone(1000.0, 60) + silence(900)) == []
    events = feed(vad, tone(1000.0, 200) + silence(900))
    assert [e.kind for e in events] == [VadEventKind.SPEECH_START, VadEventKind.NOISE]


def test_the_noise_floor_follows_a_noisy_line():
    vad = EnergyVad()
    hiss = gain(tone(3000.0, 1000), 0.05)  # steady line noise at about -40 dBFS
    feed(vad, hiss * 4)  # at first it may sound like speech; the floor then rises
    assert vad.noise_dbfs > -45 and not vad.in_speech
    assert feed(vad, hiss * 2) == []
    events = feed(vad, tone(300.0, 600, level=0.5) + hiss)
    assert [e.kind for e in events] == [VadEventKind.SPEECH_START, VadEventKind.SPEECH_END]
    # Speech has quiet gaps between syllables: the floor stays low on a quiet line.
    quiet = EnergyVad()
    syllables = (tone(300.0, 180) + silence(60)) * 12
    events = feed(quiet, syllables + silence(900))
    assert [e.kind for e in events] == [VadEventKind.SPEECH_START, VadEventKind.SPEECH_END]
    assert quiet.noise_dbfs < -60


def test_a_monologue_is_cut_and_echo_floors_apply():
    vad = EnergyVad(VadConfig(max_utterance_ms=1000))
    events = feed(vad, tone(300.0, 1500))
    assert VadEventKind.SPEECH_END in [e.kind for e in events]
    guarded = EnergyVad()
    guarded.floor_dbfs = dbfs(tone(300.0, 100)) + 1  # quieter than this is our own echo
    assert feed(guarded, tone(300.0, 600) + silence(800)) == []


def test_frames_must_be_20_ms():
    with pytest.raises(ValueError):
        EnergyVad().process(b"\x00" * 100)


# ---------------------------------------------------------------------- text


def split(pieces):
    splitter = SentenceSplitter()
    out = []
    for piece in pieces:
        out += splitter.feed(piece)
    return out + splitter.flush(), splitter


def test_streamed_text_becomes_sentences_and_marks():
    sentences, splitter = split(["Здравствуйте! Как", " у вас дела? Всего", " доброго. [END]"])
    assert sentences == ["Здравствуйте!", "Как у вас дела?", "Всего доброго."]
    assert splitter.marks.end and not splitter.marks.opt_out
    _, splitter = split(["Извините. [OPT_OUT] [END]"])
    assert splitter.marks.opt_out and splitter.marks.end
    text, marks = take_marks("До свидания [конец]")
    assert marks.end and "конец" not in text


def test_reasoning_and_markup_are_not_spoken():
    sentences, _ = split(["<think>сначала подумаю</think>Да, конечно. "])
    assert sentences == ["Да, конечно."]
    assert speakable("**Важно:** звоните 😀 [END]") == "Важно: звоните"


def test_long_sentences_are_spoken_in_parts():
    long = "Мы проводим проверку связи, " * 8 + "спасибо."
    sentences, _ = split([long])
    assert len(sentences) >= 2
    assert all(len(s) <= 260 for s in sentences)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Не звоните мне больше", True),
        ("больше не беспокойте", True),
        ("Удалите мой номер, пожалуйста", True),
        ("не надо мне звонить", True),
        ("Звоните завтра после обеда", False),
        ("Я не могу сейчас говорить", False),
    ],
)
def test_requests_not_to_be_called(text, expected):
    assert asks_not_to_call(text) is expected


def test_known_recognizer_inventions_are_dropped():
    assert is_hallucination("Субтитры сделал DimaTorzok")
    assert not is_hallucination("Да, слушаю")


# ---------------------------------------------------------------------- AudioSocket


async def connect(server, call_uuid):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(uuid_message(call_uuid))
    await writer.drain()
    return reader, writer


async def test_audiosocket_carries_audio_both_ways():
    server = AudioSocketServer("127.0.0.1", 0)
    await server.start()
    try:
        call_uuid, connected = server.expect()
        reader, writer = await connect(server, call_uuid)
        line = await asyncio.wait_for(connected, 2)
        writer.write(audio_message(tone(440.0, 20)))
        await writer.drain()
        assert await asyncio.wait_for(line.read(), 2) == tone(440.0, 20)
        await line.write(bytes(100_000))  # more than one message can carry
        received = 0
        while received < 100_000:
            kind, payload = await asyncio.wait_for(read_message(reader), 2)
            assert kind == KIND_AUDIO
            received += len(payload)
        writer.write(HANGUP_MESSAGE)
        await writer.drain()
        assert await asyncio.wait_for(line.read(), 2) is None
        assert line.closed and not line.closed_here
        writer.close()
    finally:
        await server.close()


async def test_audiosocket_rejects_unknown_and_silent_connections(monkeypatch):
    monkeypatch.setattr(transport, "HELLO_TIMEOUT_S", 0.2)
    server = AudioSocketServer("127.0.0.1", 0)
    await server.start()
    try:
        reader, writer = await connect(server, str(uuid.uuid4()))  # not expected
        assert await asyncio.wait_for(read_message(reader), 2) == (0, b"")
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)  # says nothing
        assert await asyncio.wait_for(reader.read(), 2) == HANGUP_MESSAGE
        writer.close()
        assert server.rejected == 2
        call_uuid, connected = server.expect()
        server.forget(call_uuid, RuntimeError("the call ended"))
        with pytest.raises(RuntimeError, match="the call ended"):
            await connected
        # A forgotten call's connection is not accepted either.
        reader, writer = await connect(server, call_uuid)
        assert await asyncio.wait_for(read_message(reader), 2) == (0, b"")
        writer.close()
    finally:
        await server.close()


def test_only_local_peers_and_short_messages():
    assert transport._peer_allowed(("127.0.0.1", 5000))
    assert transport._peer_allowed(("172.28.160.1", 5000))  # WSL2 (NAT)
    assert transport._peer_allowed(("::ffff:192.168.1.20", 5000, 0, 0))
    assert not transport._peer_allowed(("8.8.8.8", 5000))
    assert not transport._peer_allowed(None)
    with pytest.raises(ValueError):
        audio_message(bytes(70_000))
    assert uuid_message(str(uuid.UUID(int=1)))[:3] == struct.pack(">BH", 1, 16)
