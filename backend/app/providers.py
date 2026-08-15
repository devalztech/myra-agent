"""Multiple remote AI providers, switchable from the Myra UI.

All remote-only now (no local model, no mock). Providers expose the same tiny
surface: ``stream(messages) -> Iterator[str]`` and ``complete(messages) -> str``,
so the agent loop is provider-agnostic. If every provider fails, the agent
surfaces a clear error instead of returning a canned/mocked reply.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from .config import settings
from .llmutil import LLMUnavailable

logger = logging.getLogger("myra.providers")

Message = dict[str, str]


@dataclass
class ProviderInfo:
    id: str
    name: str
    kind: str  # local | remote
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


class GeminiProvider(BaseProvider):
    """Google Generative Language API (v1beta) — `generateContent` REST.

    Not keyless on the real endpoint: Google always requires an API key.
    The provider stays switchable in the UI and returns a clear message
    when GEMINI_API_KEY is missing, so users know exactly why it isn't
    available rather than it being silently absent.
    """

    id = "gemini"
    name = "Google Gemini (v1beta)"
    kind = "remote"

    @property
    def model(self) -> str | None:
        return settings.gemini_model

    @property
    def available(self) -> bool:
        return bool(settings.gemini_api_key)

    @property
    def detail(self) -> str | None:
        if not settings.gemini_api_key:
            return "Set GEMINI_API_KEY in .env to enable Google Gemini."
        return settings.gemini_base_url

    def _request(self, messages: list[Message]) -> urllib.request.Request:
        if not settings.gemini_api_key:
            raise LLMUnavailable("Google Gemini is not configured (missing GEMINI_API_KEY).")
        contents: list[dict[str, object]] = []
        for message in messages:
            role = "model" if message.get("role") in ("assistant", "system") else "user"
            contents.append({"role": role, "parts": [{"text": message.get("content", "")}]})
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": settings.temperature,
                "maxOutputTokens": settings.max_tokens,
            },
        }
        url = (
            settings.gemini_base_url.rstrip("/")
            + f"/models/{settings.gemini_model}:generateContent"
            + f"?key={urllib.parse.quote(settings.gemini_api_key)}"
        )
        return urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def complete(self, messages: list[Message]) -> str:
        request = self._request(messages)
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMUnavailable(f"Gemini error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"Gemini unreachable: {exc.reason}") from exc
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable(f"Gemini returned no text: {json.dumps(data)[:300]}")

    def stream(self, messages: list[Message]) -> Iterator[str]:
        yield self.complete(messages)


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


class OpenAICompatibleProvider(BaseProvider):
    """Shared base for remote OpenAI-compatible chat-completions endpoints.

    Subclasses only declare id/name/kind plus the three config getters
    (api_key, base_url, model) and optionally a timeout. Streaming and
    non-streaming both go through urllib; errors surface as LLMUnavailable.
    """

    kind = "remote"

    # Per-request overrides set by the UI (per-user provider settings).
    # api_key / base_url / model. Cleared after each run.
    def configure(self, *, api_key: str | None = None,
                  base_url: str | None = None, model: str | None = None) -> None:
        self._override_key = api_key
        self._override_url = base_url
        self._override_model = model

    def _clear_override(self) -> None:
        self._override_key = None
        self._override_url = None
        self._override_model = None

    # --- subclass overrides ---------------------------------------------
    @property
    def api_key(self) -> str:
        if getattr(self, "_override_key", None):
            return self._override_key
        return self._default_api_key

    def _default_api_key(self) -> str:
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        if getattr(self, "_override_url", None):
            return self._override_url
        return self._default_base_url

    def _default_base_url(self) -> str:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        if getattr(self, "_override_model", None):
            return self._override_model
        return self._default_model

    def _default_model(self) -> str:
        raise NotImplementedError

    _timeout = 180

    # --- shared behaviour ------------------------------------------------
    @property
    def model(self) -> str | None:
        return self.model_name

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def detail(self) -> str | None:
        if not self.api_key:
            return f"Set {self._key_env} in .env to enable {self.name}."
        return self.base_url

    @property
    def _key_env(self) -> str:
        # Derive an env var name like GROQ_API_KEY from the class name.
        name = self.id.upper()
        return f"{name}_API_KEY"

    def _request(self, messages: list[Message], stream: bool) -> urllib.request.Request:
        if not self.api_key:
            raise LLMUnavailable(f"{self.name} is not configured (missing {self._key_env}).")
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": settings.temperature,
            "stream": stream,
        }
        return urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

    def stream(self, messages: list[Message]) -> Iterator[str]:
        request = self._request(messages, stream=True)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
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
            raise LLMUnavailable(f"{self.name} error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"{self.name} unreachable: {exc.reason}") from exc

    def complete(self, messages: list[Message]) -> str:
        request = self._request(messages, stream=False)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMUnavailable(f"{self.name} error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"{self.name} unreachable: {exc.reason}") from exc
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


class GroqProvider(OpenAICompatibleProvider):
    id = "groq"
    name = "Groq (fast, free tier)"
    _timeout = 180

    @property
    def _default_api_key(self) -> str:
        return settings.groq_api_key

    @property
    def _default_base_url(self) -> str:
        return settings.groq_base_url

    @property
    def _default_model(self) -> str:
        return settings.groq_model


class SambaNovaProvider(OpenAICompatibleProvider):
    id = "sambanova"
    name = "SambaNova (free hosted)"
    _timeout = 180

    @property
    def _default_api_key(self) -> str:
        return settings.sambanova_api_key

    @property
    def _default_base_url(self) -> str:
        return settings.sambanova_base_url

    @property
    def _default_model(self) -> str:
        return settings.sambanova_model


class ScalewayProvider(OpenAICompatibleProvider):
    id = "scaleway"
    name = "Scaleway (free hosted)"
    _timeout = 180

    @property
    def _default_api_key(self) -> str:
        return settings.scaleway_api_key

    @property
    def _default_base_url(self) -> str:
        return settings.scaleway_base_url

    @property
    def _default_model(self) -> str:
        return settings.scaleway_model


class PollinationsProvider(BaseProvider):
    """Pollinations.ai — fully keyless OpenAI-compatible chat completions.

    Uses httpx rather than urllib: Pollinations sits behind Cloudflare and
    TLS-fingerprints clients, returning 402/403 to Python's default urllib
    stack, while httpx + a browser User-Agent sails through. No API key.
    Verified working anonymously against its legacy text API.
    """

    id = "pollinations"
    name = "Pollinations (keyless)"
    kind = "remote"

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    }

    @property
    def model(self) -> str | None:
        return settings.pollinations_model

    @property
    def available(self) -> bool:
        return True

    @property
    def detail(self) -> str | None:
        return "Keyless — no API key required."

    def _payload(self, messages: list[Message], stream: bool) -> dict[str, object]:
        return {
            "model": settings.pollinations_model,
            "messages": messages,
            "temperature": settings.temperature,
            "stream": stream,
        }

    def complete(self, messages: list[Message]) -> str:
        import httpx

        try:
            resp = httpx.post(
                settings.pollinations_base_url.rstrip("/"),
                json=self._payload(messages, stream=False),
                headers=self._HEADERS,
                timeout=120,
            )
        except httpx.RequestError as exc:
            raise LLMUnavailable(f"Pollinations unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise LLMUnavailable(f"Pollinations error {resp.status_code}: {resp.text[:400]}")
        try:
            data = resp.json()
        except ValueError:
            raise LLMUnavailable(f"Pollinations returned invalid JSON: {resp.text[:200]}")
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    def stream(self, messages: list[Message]) -> Iterator[str]:
        import httpx

        try:
            with httpx.stream(
                "POST",
                settings.pollinations_base_url.rstrip("/"),
                json=self._payload(messages, stream=True),
                headers=self._HEADERS,
                timeout=180,
            ) as resp:
                if resp.status_code != 200:
                    body = "".join(resp.iter_text()) or ""
                    raise LLMUnavailable(f"Pollinations error {resp.status_code}: {body[:400]}")
                for line in resp.iter_lines():
                    line = line.strip()
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
        except httpx.RequestError as exc:
            raise LLMUnavailable(f"Pollinations unreachable: {exc}") from exc


_PROVIDERS: dict[str, BaseProvider] = {
    AgnesProvider.id: AgnesProvider(),
    GeminiProvider.id: GeminiProvider(),
    GroqProvider.id: GroqProvider(),
    SambaNovaProvider.id: SambaNovaProvider(),
    ScalewayProvider.id: ScalewayProvider(),
    PollinationsProvider.id: PollinationsProvider(),
}


def list_providers() -> list[dict[str, object]]:
    return [p.info().dict() for p in _PROVIDERS.values()]


def get_provider(provider_id: str | None = None) -> BaseProvider:
    key = (provider_id or settings.default_provider or "groq").lower()
    if key not in _PROVIDERS:
        raise LLMUnavailable(f"Unknown provider '{provider_id}'.")
    return _PROVIDERS[key]
