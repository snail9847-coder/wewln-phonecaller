"""With the real models (the CI job `voice-models`): Piper speaks, Vosk hears it, and the
engine holds a conversation with them (the dialogue model is a fake: Ollama is not needed).

Needs the voice extras and the models (docs/TESTING.md):

    pip install -e ".[dev,voice]"
    WEWLN_DATA_DIR=.models-cache wewln voice download all
    WEWLN_TEST_MODELS_DIR=.models-cache/models pytest -m voice_models -s

Skipped without them; with WEWLN_TEST_MODELS_REQUIRED=1 (CI) missing models are an error.
"""

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

from voice_fakes import FakeLlm
from wewln_phonecaller.cli import main
from wewln_phonecaller.cli_voice import word_match
from wewln_phonecaller.config import DEFAULT_PIPER_VOICE, DEFAULT_VOSK_MODEL
from wewln_phonecaller.voice.audio import SAMPLE_RATE, dbfs, duration_ms, silence
from wewln_phonecaller.voice.engine import CONVERSATION_END, ConversationConfig, ConversationSession
from wewln_phonecaller.voice.runtime import system_prompt
from wewln_phonecaller.voice.simulate import SimulatedCaller
from wewln_phonecaller.voice.stt import VoskStt
from wewln_phonecaller.voice.transport import memory_pair
from wewln_phonecaller.voice.tts import PiperTts

MODELS = Path(os.environ.get("WEWLN_TEST_MODELS_DIR") or "no-models").resolve()
VOSK = MODELS / DEFAULT_VOSK_MODEL
PIPER = MODELS / DEFAULT_PIPER_VOICE


def _missing() -> str:
    if not os.environ.get("WEWLN_TEST_MODELS_DIR"):
        return "no models (set WEWLN_TEST_MODELS_DIR)"
    for module in ("vosk", "piper", "numpy"):
        if importlib.util.find_spec(module) is None:
            return f'{module} is not installed (pip install -e ".[voice]")'
    for path in (VOSK, PIPER, PIPER.with_name(PIPER.name + ".json")):
        if not path.exists():
            return f"no {path.name} in {MODELS} (wewln voice download all)"
    return ""


MISSING = _missing()
if MISSING and os.environ.get("WEWLN_TEST_MODELS_REQUIRED") == "1":
    raise RuntimeError(f"the voice model tests cannot run: {MISSING}")

pytestmark = [
    pytest.mark.voice_models,
    pytest.mark.skipif(bool(MISSING), reason=MISSING),
]


@pytest.fixture
async def voices():
    stt, tts = VoskStt(VOSK), PiperTts(PIPER)
    await asyncio.gather(stt.load(), tts.load())
    return stt, tts


async def test_piper_speaks_and_vosk_hears_it_at_8_khz(voices):
    stt, tts = voices
    phrase = "Добрый день! Я хотел бы узнать подробности о доставке."
    audio = await tts.synthesize(phrase)
    assert 1500 < duration_ms(audio) < 8000 and SAMPLE_RATE == 8000
    assert dbfs(audio) > -35  # loud enough for a phone line
    heard = await stt.transcribe(audio)
    print(f"\nPiper -> Vosk: «{heard}»")
    assert word_match(phrase, heard) >= 0.6
    # Silence is not words.
    assert await stt.transcribe(silence(1500)) == ""


async def test_a_conversation_with_the_real_voices(voices):
    stt, tts = voices
    call, phone = memory_pair()
    config = ConversationConfig(
        system_prompt=system_prompt(None),
        greeting="Здравствуйте! Это тестовый звонок. Вам удобно говорить?",
        goodbye="До свидания!",
        tail_ms=100,
    )
    llm = FakeLlm(
        [
            "Нисколько, мы проверяем голосового помощника.",
            "Спасибо, до свидания! [END]",
        ]
    )
    session = ConversationSession(call, stt=stt, llm=llm, tts=tts, config=config)
    said = ["Хорошо, а сколько это стоит?", "Понятно, спасибо."]
    caller = SimulatedCaller(phone, list(said), tts, pause_ms=1200, patience_s=30)
    result, _ = await asyncio.wait_for(asyncio.gather(session.run(), caller.run()), 90)

    assert [t.kind for t in result.turns] == ["greeting", "speech", "reply", "speech", "reply"]
    assert result.reason == CONVERSATION_END
    heard = [t for t in result.turns if t.role == "caller"]
    for expected, turn in zip(said, heard, strict=True):
        print(f"\nsaid «{expected}», heard «{turn.text}» ({turn.stt_ms} ms)")
        assert word_match(expected, turn.text) >= 0.5
    for turn in result.turns:
        if turn.kind == "reply":
            print(f"answered in {turn.response_ms} ms: «{turn.text}»")
            assert turn.response_ms is not None and turn.response_ms < 10_000


def test_voice_check_passes_with_the_real_models(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WEWLN_VOICE_STT_MODEL", str(VOSK))
    monkeypatch.setenv("WEWLN_VOICE_TTS_MODEL", str(PIPER))
    code = main(["voice", "check", "--no-llm", "--save-wav", str(tmp_path / "check.wav")])
    out = capsys.readouterr().out
    print(out)
    assert code == 0, out
    assert "[FAIL]" not in out and "[ok] loaded" in out
    assert (tmp_path / "check.wav").stat().st_size > 10_000
