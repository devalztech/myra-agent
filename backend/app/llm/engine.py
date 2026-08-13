"""Local LLM engines.

Two backends:
  * ``llama_cpp`` — real local GGUF inference via llama-cpp-python (default).
  * ``mock``      — deterministic, dependency-free engine used by the test
                    suite and by environments without a downloaded model.

No external/agentic APIs are used anywhere: inference is always local.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from ..config import settings
from .prompts import SYSTEM_PROMPT
from .tiers import ModelTier, detect_total_ram_gb, resolve_model_spec, select_tier

logger = logging.getLogger("myra.llm")

Message = dict[str, str]


class LLMUnavailable(RuntimeError):
    """Raised when the local model cannot be loaded."""


class BaseEngine:
    backend = "base"

    def __init__(self) -> None:
        self.tier: ModelTier = select_tier()
        self.ram_gb: float = detect_total_ram_gb()

    @property
    def model_name(self) -> str | None:
        return None

    @property
    def loaded(self) -> bool:
        return False

    @property
    def status(self) -> str:
        """One of: idle | downloading | loading | ready | error."""
        return "ready" if self.loaded else "idle"

    @property
    def detail(self) -> str | None:
        return None

    @property
    def context_size(self) -> int:
        return settings.context_size or self.tier.context_size

    def stream(self, messages: list[Message]) -> Iterator[str]:
        raise NotImplementedError

    def complete(self, messages: list[Message]) -> str:
        return "".join(self.stream(messages))


class MockEngine(BaseEngine):
    """Deterministic echo-style engine — used for tests and smoke runs."""

    backend = "mock"

    @property
    def model_name(self) -> str | None:
        return "mock-local-llama"

    @property
    def loaded(self) -> bool:
        return True

    def stream(self, messages: list[Message]) -> Iterator[str]:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        reply = (
            f"[myra:mock] I received your message: {last_user.strip()} "
            "Local inference is running in mock mode, so this is a canned reply."
        )
        for token in reply.split(" "):
            yield token + " "


class LlamaCppEngine(BaseEngine):
    """Local GGUF inference through llama-cpp-python (lazy-loaded)."""

    backend = "llama_cpp"

    def __init__(self) -> None:
        super().__init__()
        self._llm = None
        self._lock = threading.Lock()  # llama.cpp contexts are not thread-safe
        self._load_lock = threading.Lock()
        self._model_path: Path | None = None
        self._status = "idle"
        self._detail: str | None = None

    # -- observable state ----------------------------------------------
    @property
    def status(self) -> str:
        return self._status

    @property
    def detail(self) -> str | None:
        return self._detail

    # -- model file ----------------------------------------------------
    def resolve_model_path(self, download: bool = True) -> Path:
        if settings.model_path:
            path = Path(settings.model_path)
            if not path.exists():
                raise LLMUnavailable(f"MYRA_MODEL_PATH does not exist: {path}")
            return path

        repo_id, filename, _tier = resolve_model_spec()
        settings.models_dir.mkdir(parents=True, exist_ok=True)
        local = settings.models_dir / filename
        if local.exists():
            return local

        # Any already-present .gguf beats a multi-GB download.
        existing = sorted(settings.models_dir.glob("*.gguf"))
        if existing:
            logger.info("Using already-downloaded model %s", existing[0].name)
            return existing[0]

        if not download:
            raise LLMUnavailable(
                f"Model {filename} not found in {settings.models_dir}. "
                "Run scripts/download_model.py first."
            )

        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:  # pragma: no cover - install-time issue
            raise LLMUnavailable("huggingface_hub is not installed.") from exc

        logger.info("Downloading %s/%s ...", repo_id, filename)
        self._status = "downloading"
        self._detail = f"Downloading {filename} (~{_tier.size_gb:.1f} GB)"
        try:
            downloaded = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=str(settings.models_dir),
            )
        except Exception as exc:
            self._status = "error"
            self._detail = f"Model download failed: {exc}"
            raise LLMUnavailable(self._detail) from exc
        return Path(downloaded)

    # -- lifecycle -----------------------------------------------------
    def load(self) -> None:
        if self._llm is not None:
            return
        with self._load_lock:
            if self._llm is not None:
                return
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                self._status = "error"
                self._detail = "llama-cpp-python is not installed."
                raise LLMUnavailable(
                    "llama-cpp-python is not installed. Run scripts/install.sh."
                ) from exc

            path = self.resolve_model_path()
            self._status = "loading"
            self._detail = f"Loading {path.name}"
            logger.info("Loading local model: %s", path)
            logger.info(
                "llama.cpp params: n_ctx=%s n_threads=%s n_batch=%s n_gpu_layers=%s",
                self.context_size,
                settings.threads,
                settings.batch_size,
                settings.gpu_layers,
            )
            started = time.time()
            try:
                self._llm = Llama(
                    model_path=str(path),
                    n_ctx=self.context_size,
                    n_threads=settings.threads,
                    # Prompt ingest runs wider than generation; both are capped
                    # by settings.threads so we never oversubscribe the CPU.
                    n_threads_batch=settings.threads,
                    n_batch=settings.batch_size,
                    n_gpu_layers=settings.gpu_layers,
                    use_mmap=settings.use_mmap,
                    use_mlock=settings.use_mlock,
                    verbose=settings.debug,
                )
            except Exception as exc:
                self._status = "error"
                self._detail = f"Failed to load {path.name}: {exc}"
                logger.exception("Model load failed")
                raise LLMUnavailable(self._detail) from exc
            self._model_path = path
            self._status = "ready"
            self._detail = None
            logger.info("Model ready in %.1fs (%s)", time.time() - started, path.name)

    @property
    def model_name(self) -> str | None:
        if self._model_path:
            return self._model_path.name
        if settings.model_path:
            return Path(settings.model_path).name
        return resolve_model_spec()[1]

    @property
    def loaded(self) -> bool:
        return self._llm is not None

    # -- inference -----------------------------------------------------
    def stream(self, messages: list[Message]) -> Iterator[str]:
        self.load()
        assert self._llm is not None
        with self._lock:
            chunks = self._llm.create_chat_completion(
                messages=messages,
                temperature=settings.temperature,
                top_p=settings.top_p,
                max_tokens=settings.max_tokens,
                stream=True,
            )
            for chunk in chunks:
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece


_engine: BaseEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> BaseEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = (
                    MockEngine() if settings.llm_backend == "mock" else LlamaCppEngine()
                )
                logger.info("LLM backend: %s", _engine.backend)
    return _engine


def reset_engine() -> None:
    """Test helper."""
    global _engine
    with _engine_lock:
        _engine = None


def _approx_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) — no tokenizer needed."""
    return max(1, len(text) // 4)


def build_messages(history: list[Message], user_content: str) -> list[Message]:
    """System prompt + a trimmed history window + the new user message.

    History is trimmed by *both* message count and an approximate token
    budget. Without the token budget a few long pasted snippets can overflow
    the context window, which makes llama.cpp re-ingest (or truncate) the
    whole prompt on every turn — the single biggest cause of a chat that
    gets slower and slower the longer it runs.
    """
    window = settings.history_window
    trimmed = list(history[-window:] if window > 0 else history)

    engine = get_engine()
    # Leave room for the reply and the system prompt.
    budget = max(
        512,
        engine.context_size - settings.max_tokens - _approx_tokens(SYSTEM_PROMPT) - 64,
    )
    used = _approx_tokens(user_content)
    kept: list[Message] = []
    for message in reversed(trimmed):
        cost = _approx_tokens(message.get("content", ""))
        if used + cost > budget:
            break
        used += cost
        kept.append(message)
    kept.reverse()

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *kept,
        {"role": "user", "content": user_content},
    ]
