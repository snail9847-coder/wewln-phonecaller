"""The dialogue model: streams the reply while it is being generated. Runs on this computer.

- `ollama`: Ollama's /api/chat (NDJSON stream). Reasoning ("thinking") is switched off by
  default: on the phone every second of silence counts. A model that is not pulled yet is
  reported with the command that pulls it.
- `openai`: any OpenAI-compatible server on this computer (llama.cpp `llama-server`,
  LM Studio, vLLM): /v1/chat/completions with server-sent events.

The conversation text goes only to the configured address (validated: this computer or a
private network unless it is https); nothing is logged except sizes and timings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

log = logging.getLogger("wewln.voice.llm")

Message = dict[str, str]


class LlmError(Exception):
    def __init__(self, message: str, *, code: str = "llm_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ChatModel(ABC):
    name = "llm"
    model = ""

    @abstractmethod
    def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Pieces of the reply as they are generated."""

    async def warm(self, prompt: str | None = None) -> None:  # noqa: B027 - optional hook
        """Load the model before the first call (the first reply would otherwise be slow);
        with `prompt` (the system prompt) the server also keeps it processed in its cache."""

    async def check(self) -> dict[str, Any]:
        return {"llm": self.name, "status": "ok"}

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release HTTP connections."""

    def describe(self) -> dict[str, Any]:
        return {"llm": self.name, "model": self.model}


class _HttpChatModel(ChatModel):
    def __init__(
        self,
        url: str,
        model: str,
        *,
        temperature: float = 0.5,
        max_tokens: int = 160,
        first_token_timeout: float = 15.0,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.first_token_timeout = first_token_timeout
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def describe(self) -> dict[str, Any]:
        return {"llm": self.name, "model": self.model, "url": self.url}

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Per-read limits are enforced in stream(); the total covers model loading.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _lines(self, resp: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
        """Response lines, with the first-piece and the total time limits."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        first = True
        while True:
            remaining = self.timeout - (loop.time() - started)
            wait = min(self.first_token_timeout, remaining) if first else remaining
            if wait <= 0:
                raise LlmError("the dialogue model took too long to answer", code="llm_timeout")
            try:
                line = await asyncio.wait_for(resp.content.readline(), wait)
            except TimeoutError:
                raise LlmError(
                    "the dialogue model took too long to answer", code="llm_timeout"
                ) from None
            if not line:
                return
            first = False
            yield line

    async def _get_json(self, path: str) -> tuple[int | None, Any]:
        try:
            async with self._http().get(
                self.url + path, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                return resp.status, body
        except (aiohttp.ClientError, TimeoutError, OSError):
            return None, None


class OllamaChat(_HttpChatModel):
    name = "ollama"

    def __init__(
        self,
        url: str,
        model: str,
        *,
        think: bool = False,
        keep_alive: str = "30m",
        num_ctx: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(url, model, **kwargs)
        self.think = think
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self._send_think = True

    def _body(self, messages: list[Message], *, stream: bool = True) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": self.temperature, "num_predict": self.max_tokens}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if self._send_think:
            body["think"] = self.think
        return body

    async def _error(self, resp: aiohttp.ClientResponse) -> LlmError:
        try:
            detail = str((await resp.json(content_type=None) or {}).get("error") or "")
        except (ValueError, aiohttp.ClientError):
            detail = ""
        if resp.status == 404 or "not found" in detail.lower():
            return LlmError(
                f"the model {self.model} is not in Ollama: run `ollama pull {self.model}`",
                code="llm_model_missing",
            )
        return LlmError(
            f"Ollama answered HTTP {resp.status}" + (f": {detail[:200]}" if detail else ""),
            code="llm_http_error",
        )

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        for attempt in range(2):
            try:
                async with self._http().post(
                    self.url + "/api/chat", json=self._body(messages)
                ) as resp:
                    if resp.status == 400 and self._send_think and attempt == 0:
                        error = await self._error(resp)
                        if "think" in error.message.lower():
                            # An older Ollama or a model without the switch: ask without it.
                            self._send_think = False
                            continue
                        raise error
                    if resp.status != 200:
                        raise await self._error(resp)
                    async for line in self._lines(resp):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except ValueError:
                            continue
                        if data.get("error"):
                            raise LlmError(f"Ollama: {str(data['error'])[:200]}")
                        content = str((data.get("message") or {}).get("content") or "")
                        if content:
                            yield content
                        if data.get("done"):
                            return
                    return
            except (aiohttp.ClientError, OSError) as exc:
                raise LlmError(
                    f"Ollama is not reachable at {self.url} ({type(exc).__name__}): is it running?",
                    code="llm_unreachable",
                ) from None

    async def warm(self, prompt: str | None = None) -> None:
        # Same options as the replies (another context size would reload the model).
        messages = [{"role": "system", "content": prompt}] if prompt else []
        body = self._body(messages, stream=False)
        body["options"]["num_predict"] = 1
        try:
            async with self._http().post(
                self.url + "/api/chat", json=body, timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                if resp.status == 400 and self._send_think:
                    self._send_think = False
                    return await self.warm(prompt)
                if resp.status != 200:
                    raise await self._error(resp)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise LlmError(
                f"Ollama is not reachable at {self.url} ({type(exc).__name__}): is it running?",
                code="llm_unreachable",
            ) from None

    async def check(self) -> dict[str, Any]:
        result: dict[str, Any] = {"llm": self.name, "url": self.url, "model": self.model}
        status, version = await self._get_json("/api/version")
        if status != 200:
            return {
                **result,
                "status": "error",
                "error": f"Ollama is not reachable at {self.url}: install and start it",
            }
        result["version"] = (version or {}).get("version")
        status, tags = await self._get_json("/api/tags")
        names = set()
        for item in (tags or {}).get("models") or []:
            for key in ("name", "model"):
                if item.get(key):
                    names.add(str(item[key]))
        wanted = {self.model, f"{self.model}:latest"} if ":" not in self.model else {self.model}
        if not names & wanted:
            return {
                **result,
                "status": "error",
                "error": f"the model is not pulled: run `ollama pull {self.model}`",
            }
        return {**result, "status": "ok"}


class OpenAiChat(_HttpChatModel):
    """An OpenAI-compatible local server (llama.cpp, LM Studio, vLLM)."""

    name = "openai"

    def __init__(self, url: str, model: str, *, api_key: str | None = None, **kwargs: Any):
        url = url.rstrip("/")
        super().__init__(url.removesuffix("/v1"), model, **kwargs)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        try:
            async with self._http().post(
                self.url + "/v1/chat/completions", json=body, headers=self._headers
            ) as resp:
                if resp.status != 200:
                    raise LlmError(
                        f"the model server answered HTTP {resp.status}", code="llm_http_error"
                    )
                async for raw in self._lines(resp):
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    for choice in data.get("choices") or []:
                        content = str((choice.get("delta") or {}).get("content") or "")
                        if content:
                            yield content
        except (aiohttp.ClientError, OSError) as exc:
            raise LlmError(
                f"the model server is not reachable at {self.url} ({type(exc).__name__})",
                code="llm_unreachable",
            ) from None

    async def warm(self, prompt: str | None = None) -> None:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": prompt or "Привет"}],
            "max_tokens": 1,
            "temperature": self.temperature,
        }
        try:
            async with self._http().post(
                self.url + "/v1/chat/completions",
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    raise LlmError(
                        f"the model server answered HTTP {resp.status}", code="llm_http_error"
                    )
                await resp.read()
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise LlmError(
                f"the model server is not reachable at {self.url} ({type(exc).__name__})",
                code="llm_unreachable",
            ) from None

    async def check(self) -> dict[str, Any]:
        result: dict[str, Any] = {"llm": self.name, "url": self.url, "model": self.model}
        status, models = await self._get_json("/v1/models")
        if status != 200:
            return {**result, "status": "error", "error": f"no model server at {self.url}"}
        ids = {str(m.get("id")) for m in (models or {}).get("data") or [] if isinstance(m, dict)}
        if ids and self.model not in ids:
            return {
                **result,
                "status": "error",
                "error": "the server does not offer this model (it has: "
                + ", ".join(sorted(ids)[:5])
                + ")",
            }
        return {**result, "status": "ok"}
