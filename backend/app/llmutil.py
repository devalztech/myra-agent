"""Shared LLM helpers kept after removing the local-inference engine.

The old ``llm/`` package hosted llama.cpp local inference, model tiers and a
chat prompt. Myra is remote-only now (providers in ``app/providers.py``), so
all that is gone. These two small utilities are still used across the app:
the provider error type and the session-title helper.
"""

from __future__ import annotations


class LLMUnavailable(RuntimeError):
    """Raised when a provider cannot be used (missing key, error, unreachable)."""


def title_from_message(content: str, limit: int = 48) -> str:
    """Derive a short session title from the first user message."""
    text = " ".join(content.split())
    if len(text) <= limit:
        return text or "New session"
    return text[: limit - 1].rstrip() + "…"
