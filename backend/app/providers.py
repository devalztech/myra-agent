"""Multiple AI providers, switchable from the Myra UI.

* ``local``  — llama.cpp GGUF inference in-process (default, no network).
* ``agnes``  — Agnes AI over an OpenAI-compatible chat-completions endpoint.
* ``mock``   — deterministic, used by tests and by cold environments.

All providers expose the same tiny surface: ``stream(messages) -> Iterator[str]``
and ``complete(messages) -> str``, so the agent loop is provider-agnostic.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from .config import settings
from .llm.engine import LLMUnavailable, get_engine

logger = logging.getLogger("myra.providers")

Message = dict[str, str]


@dataclass
class ProviderInfo:
    id: str
    name: str
    kind: str  # local | remote | mock
    model: str | None
    available: bool
    detail: str | None = None

    def dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "model": self.model,
            "available": self.available,
            "detail": self.detail,
        }


class BaseProvider:
    id = "base"
    name = "Base"
    kind = "local"

    @property
    def model(self) -> str | None:
        return None

    @property
    def available(self) -> bool:
        return True

    @property
    def detail(self) -> str | None:
        return None

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            id=self.id,
            name=self.name,
            kind=self.kind,
            model=self.model,
            available=self.available,
            detail=self.detail,
        )

    def stream(self, messages: list[Message]) -> Iterator[str]:
        raise NotImplementedError

    def complete(self, messages: list[Message]) -> str:
        return "".join(self.stream(messages))


class LocalProvider(BaseProvider):
    """Local llama.cpp / GGUF model (or the mock engine in test mode)."""

    id = "local"
    name = "Local Llama"
    kind = "local"

    @property
    def _engine(self):
        return get_engine()

    @property
    def model(self) -> str | None:
        return self._engine.model_name

    @property
    def available(self) -> bool:
        engine = self._engine
        return engine.status in {"ready", "idle", "loading", "downloading"}

    @property
    def detail(self) -> str | None:
        engine = self._engine
        return engine.detail or f"status={engine.status}, ctx={engine.context_size}"

    def stream(self, messages: list[Message]) -> Iterator[str]:
        yield from self._engine.stream(messages)


class MockProvider(BaseProvider):
    id = "mock"
    name = "Mock (offline)"
    kind = "mock"

    @property
    def model(self) -> str | None:
        return "myra-mock"

    def stream(self, messages: list[Message]) -> Iterator[str]:
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        for chunk in f"[myra:mock] {last.strip()}".split(" "):
            yield chunk + " "


class AgnesProvider(BaseProvider):
    """Agnes AI — OpenAI-compatible chat completions with SSE streaming."""

    id = "agnes"
    name = "Agnes AI"
    kind = "remote"

    @property
    def model(self) -> str | None:
        return settings.agnes_model

    @property
    def available(self) -> bool:
        return bool(settings.agnes_api_key)

    @property
    def detail(self) -> str | None:
        if not settings.agnes_api_key:
            return "Set AGNES_API_KEY in .env to enable Agnes AI."
        return settings.agnes_base_url

    def _request(self, messages: list[Message], stream: bool) -> urllib.request.Request:
        if not settings.agnes_api_key:
            raise LLMUnavailable("Agnes AI is not configured (missing AGNES_API_KEY).")
        payload = {
            "model": settings.agnes_model,
            "messages": messages,
            "temperature": settings.temperature,
            "stream": stream,
        }
        return urllib.request.Request(
            settings.agnes_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.agnes_api_key}",
            },
            method="POST",
        )

    def stream(self, messages: list[Message]) -> Iterator[str]:
        request = self._request(messages, stream=True)
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data in {"", "[DONE]"}:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMUnavailable(f"Agnes AI error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"Agnes AI unreachable: {exc.reason}") from exc

    def complete(self, messages: list[Message]) -> str:
        request = self._request(messages, stream=False)
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMUnavailable(f"Agnes AI error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"Agnes AI unreachable: {exc.reason}") from exc
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


_PROVIDERS: dict[str, BaseProvider] = {
    LocalProvider.id: LocalProvider(),
    AgnesProvider.id: AgnesProvider(),
    MockProvider.id: MockProvider(),
}


def list_providers() -> list[dict[str, object]]:
    return [p.info().dict() for p in _PROVIDERS.values()]


def get_provider(provider_id: str | None = None) -> BaseProvider:
    key = (provider_id or settings.default_provider or "local").lower()
    if settings.llm_backend == "mock" and key == "local":
        key = "mock"
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise LLMUnavailable(f"Unknown provider '{provider_id}'.")
    return provider
