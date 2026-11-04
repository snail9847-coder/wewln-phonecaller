"""The HTTP backends (Ollama, OpenAI-compatible, whisper.cpp, Piper) against fake servers, the
runtime that builds them from the settings, and the voice settings themselves."""

import asyncio
import json

import pytest
from aiohttp import web

from voice_fakes import FakeLlm, fake_runtime
from wewln_phonecaller.config import load_settings
from wewln_phonecaller.errors import ConfigError
from wewln_phonecaller.voice import runtime as voice_runtime
from wewln_phonecaller.voice.audio import duration_ms, read_wav, tone, wav_bytes
from wewln_phonecaller.voice.llm import LlmError, OllamaChat, OpenAiChat
from wewln_phonecaller.voice.stt import FasterWhisperStt, SttError, VoskStt, WhisperHttpStt
from wewln_phonecaller.voice.tts import PiperHttpTts, PiperTts, TtsError


async def collect(stream):
    return "".join([piece async for piece in stream])


# ---------------------------------------------------------------------- Ollama


class FakeOllama:
    def __init__(self, models=("qwen3:4b",)):
        self.models = list(models)
        self.bodies = []
        self.reject_think = False
        self.first_delay = 0.0
        self.chunks = ["Здравствуйте! ", "Чем могу ", "помочь?"]
        self.stream_error = None

    async def version(self, request):
        return web.json_response({"version": "0.40.14"})

    async def tags(self, request):
        return web.json_response({"models": [{"name": m, "model": m} for m in self.models]})

    async def chat(self, request):
        body = await request.json()
        self.bodies.append(body)
        if self.reject_think and "think" in body:
            return web.json_response({"error": '"qwen2" does not support thinking'}, status=400)
        if body["model"] not in self.models:
            return web.json_response({"error": f"model '{body['model']}' not found"}, status=404)
        if not body.get("stream", True):
            return web.json_response({"message": {"content": ""}, "done": True})
        response = web.StreamResponse()
        await response.prepare(request)
        if self.first_delay:
            await asyncio.sleep(self.first_delay)
        for chunk in self.chunks:
            line = {"message": {"role": "assistant", "content": chunk}, "done": False}
            await response.write((json.dumps(line) + "\n").encode())
        if self.stream_error:
            await response.write((json.dumps({"error": self.stream_error}) + "\n").encode())
        await response.write(b'{"done": true}\n')
        return response

    def app(self):
        app = web.Application()
        app.router.add_get("/api/version", self.version)
        app.router.add_get("/api/tags", self.tags)
        app.router.add_post("/api/chat", self.chat)
        return app


@pytest.fixture
async def ollama(aiohttp_server):
    fake = FakeOllama()
    server = await aiohttp_server(fake.app())
    fake.url = str(server.make_url("")).rstrip("/")
    return fake


MESSAGES = [{"role": "system", "content": "Будь краток."}, {"role": "user", "content": "Алло?"}]


async def test_ollama_streams_the_reply_with_the_options(ollama):
    llm = OllamaChat(ollama.url, "qwen3:4b", temperature=0.4, max_tokens=120)
    try:
        assert await collect(llm.stream(MESSAGES)) == "Здравствуйте! Чем могу помочь?"
        (body,) = ollama.bodies
        assert body["messages"] == MESSAGES
        assert (body["stream"], body["think"], body["keep_alive"]) == (True, False, "30m")
        assert body["options"] == {"temperature": 0.4, "num_predict": 120}
        assert await llm.check() == {
            "llm": "ollama",
            "url": ollama.url,
            "model": "qwen3:4b",
            "version": "0.40.14",
            "status": "ok",
        }
        await llm.warm("Ты голосовой помощник.")
        warm = ollama.bodies[-1]
        assert warm["messages"] == [{"role": "system", "content": "Ты голосовой помощник."}]
        assert (warm["stream"], warm["options"]["num_predict"]) == (False, 1)
        # The same context options as the replies: Ollama does not reload the model.
        assert warm["options"]["temperature"] == 0.4
    finally:
        await llm.close()


async def test_ollama_without_the_thinking_switch_is_asked_again(ollama):
    ollama.reject_think = True
    llm = OllamaChat(ollama.url, "qwen3:4b")
    try:
        assert await collect(llm.stream(MESSAGES)) == "Здравствуйте! Чем могу помочь?"
        assert ["think" in b for b in ollama.bodies] == [True, False]
        await collect(llm.stream(MESSAGES))
        assert "think" not in ollama.bodies[-1]  # remembered
    finally:
        await llm.close()


async def test_ollama_errors_are_explained(ollama, unused_tcp_port):
    missing = OllamaChat(ollama.url, "llama3:70b")
    slow = OllamaChat(ollama.url, "qwen3:4b", first_token_timeout=0.2, timeout=5)
    down = OllamaChat(f"http://127.0.0.1:{unused_tcp_port}", "qwen3:4b")
    try:
        with pytest.raises(LlmError) as error:
            await collect(missing.stream(MESSAGES))
        assert error.value.code == "llm_model_missing" and "ollama pull" in str(error.value)
        check = await missing.check()
        assert check["status"] == "error" and "ollama pull llama3:70b" in check["error"]
        with pytest.raises(LlmError) as error:
            await collect(down.stream(MESSAGES))
        assert error.value.code == "llm_unreachable"
        assert (await down.check())["status"] == "error"
        ollama.first_delay = 1.0
        with pytest.raises(LlmError) as error:
            await collect(slow.stream(MESSAGES))
        assert error.value.code == "llm_timeout"
        ollama.first_delay = 0
        ollama.stream_error = "out of memory"
        with pytest.raises(LlmError, match="out of memory"):
            await collect(slow.stream(MESSAGES))
    finally:
        for llm in (missing, slow, down):
            await llm.close()


# ---------------------------------------------------------------------- OpenAI-compatible


@pytest.fixture
async def openai_server(aiohttp_server):
    seen = []

    async def models(request):
        return web.json_response({"data": [{"id": "local-model"}]})

    async def chat(request):
        body = await request.json()
        seen.append((dict(request.headers), body))
        if not body.get("stream"):
            return web.json_response({"choices": [{"message": {"content": "."}}]})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for piece in ("Да, ", "слышу ", "вас."):
            chunk = {"choices": [{"delta": {"content": piece}}]}
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    server = await aiohttp_server(app)
    return str(server.make_url("")).rstrip("/"), seen


async def test_an_openai_compatible_server(openai_server):
    url, seen = openai_server
    llm = OpenAiChat(url + "/v1", "local-model", api_key="local-key", max_tokens=100)
    try:
        assert await collect(llm.stream(MESSAGES)) == "Да, слышу вас."
        headers, body = seen[0]
        assert headers["Authorization"] == "Bearer local-key"
        assert (body["model"], body["max_tokens"], body["stream"]) == ("local-model", 100, True)
        assert (await llm.check())["status"] == "ok"
        other = await OpenAiChat(url, "another-model").check()
        assert other["status"] == "error" and "local-model" in other["error"]
        await llm.warm("Подсказка")
        assert seen[-1][1]["max_tokens"] == 1
    finally:
        await llm.close()


# ---- whisper.cpp and Piper servers


@pytest.fixture
async def speech_servers(aiohttp_server):
    state = {"text": " Добрый день ", "status": 200, "forms": [], "bodies": [], "wav": True}

    async def inference(request):
        form = await request.post()
        upload = form["file"]
        state["forms"].append({k: v for k, v in form.items() if k != "file"})
        state["uploaded"] = read_wav(upload.file.read())
        if state["status"] != 200:
            return web.json_response({}, status=state["status"])
        return web.json_response({"text": state["text"]})

    async def synthesize(request):
        state["bodies"].append(await request.json())
        if state["status"] != 200:
            return web.Response(status=state["status"])
        if not state["wav"]:
            return web.Response(body=b"not audio")
        return web.Response(body=wav_bytes(tone(220.0, 500, rate=22050), rate=22050))

    app = web.Application()
    app.router.add_post("/inference", inference)
    app.router.add_post("/synthesize", synthesize)
    server = await aiohttp_server(app)
    state["url"] = str(server.make_url("")).rstrip("/")
    return state


async def test_whisper_server_recognition(speech_servers):
    stt = WhisperHttpStt(speech_servers["url"], language="ru-RU")
    try:
        assert await stt.transcribe(tone(300.0, 1000)) == "Добрый день"
        (form,) = speech_servers["forms"]
        assert (form["language"], form["response_format"]) == ("ru", "json")
        pcm, rate = speech_servers["uploaded"]
        assert rate == 16000 and abs(duration_ms(pcm, 16000) - 1000) <= 2
        speech_servers["text"] = "Продолжение следует..."
        assert await stt.transcribe(tone(300.0, 500)) == ""  # Whisper invents this on noise
        speech_servers["status"] = 500
        with pytest.raises(SttError, match="HTTP 500"):
            await stt.transcribe(tone(300.0, 500))
    finally:
        await stt.close()


async def test_piper_server_synthesis(speech_servers):
    tts = PiperHttpTts(speech_servers["url"], speaker=2, length_scale=1.1)
    try:
        pcm = await tts.synthesize("Здравствуйте")
        assert abs(duration_ms(pcm) - 500) <= 2  # resampled to the line's 8 kHz
        assert speech_servers["bodies"][0] == {
            "text": "Здравствуйте",
            "speaker_id": 2,
            "length_scale": 1.1,
        }
        speech_servers["wav"] = False
        with pytest.raises(TtsError, match="no usable audio"):
            await tts.synthesize("Тест")
        speech_servers["status"] = 503
        with pytest.raises(TtsError, match="HTTP 503"):
            await tts.synthesize("Тест")
    finally:
        await tts.close()


# ---------------------------------------------------------------------- runtime and settings


def test_the_runtime_is_built_from_the_settings(make_settings, tmp_path):
    settings = make_settings(voice_mode="ai")
    models = tmp_path / "data" / "models"
    stt = voice_runtime.build_stt(settings)
    assert isinstance(stt, VoskStt) and stt.model_path == models / "vosk-model-small-ru-0.22"
    tts = voice_runtime.build_tts(settings)
    assert isinstance(tts, PiperTts)
    assert str(tts.describe()["model"]) == str(models / "ru_RU-denis-medium.onnx")
    llm = voice_runtime.build_llm(settings)
    assert isinstance(llm, OllamaChat) and llm.model == "qwen3:4b" and llm.think is False
    other = make_settings(
        voice_stt="faster-whisper",
        voice_tts="piper-http",
        voice_llm="openai",
        voice_llm_url="http://192.168.1.20:8000/v1",
        voice_llm_api_key="local-key",
    )
    whisper = voice_runtime.build_stt(other)
    assert isinstance(whisper, FasterWhisperStt) and whisper.model == "small"
    assert whisper.allow_download is False
    assert isinstance(voice_runtime.build_tts(other), PiperHttpTts)
    assert isinstance(voice_runtime.build_llm(other), OpenAiChat)
    assert isinstance(
        voice_runtime.build_stt(make_settings(voice_stt="whisper-http")), WhisperHttpStt
    )
    with pytest.raises(voice_runtime.VoiceUnavailableError, match="wewln voice download"):
        voice_runtime.create_voice_runtime(settings)


async def test_the_runtime_does_not_start_without_the_dialogue_model():
    runtime = fake_runtime(llm=FakeLlm(status="Ollama is not reachable"))
    with pytest.raises(voice_runtime.VoiceUnavailableError, match="not reachable"):
        await runtime.start()
    ready = fake_runtime()
    await ready.start()
    assert ready.llm.warmed and set(ready.startup) == {
        "stt_load_s",
        "tts_load_s",
        "llm_load_s",
        "phrases_s",
    }
    await ready.close()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"voice_llm_url": "http://example.com:11434"}, "plain http"),
        ({"voice_llm_url": "ftp://127.0.0.1"}, "http:// or https://"),
        ({"voice_stt_url": "http://user:pw@127.0.0.1:8080"}, "user name"),
        ({"voice_audiosocket_advertise": "bad host!"}, "host:port"),
        ({"voice_audiosocket_host": "0.0.0.0"}, "WEWLN_VOICE_AUDIOSOCKET_ADVERTISE"),
        ({"voice_mode": "ai", "voice_audiosocket_port": 8787, "http_port": 8787}, "must differ"),
        ({"voice_greeting": "a"}, "2-400"),
        ({"voice_test_script": "коротко"}, "10-4000"),
        ({"voice_max_turns": 0}, "WEWLN_VOICE_MAX_TURNS"),
    ],
)
def test_voice_settings_are_checked(tmp_path, overrides, message):
    with pytest.raises(ConfigError) as error:
        load_settings(env_file=None, data_dir=tmp_path, **overrides)
    assert message in " ".join(error.value.problems)


def test_voice_settings_accept_the_local_network_and_hide_the_key(make_settings):
    settings = make_settings(
        voice_mode="ai",
        voice_llm="openai",
        voice_llm_url="http://10.0.0.5:8000",
        voice_llm_api_key="local-secret-key",
        voice_audiosocket_host="0.0.0.0",
        voice_audiosocket_advertise="172.28.160.1",
    )
    summary = settings.redacted_summary()
    assert summary["voice_llm_api_key"] == "set"
    assert "local-secret-key" not in json.dumps(summary, default=str)
    assert "local-secret-key" in settings.secret_values()
    assert settings.audiosocket_advertise == "172.28.160.1:9092"
    assert make_settings(voice_llm_url="https://llm.example.org").voice_llm_url == (
        "https://llm.example.org"
    )
