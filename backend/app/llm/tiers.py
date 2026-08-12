"""RAM-aware model tier selection.

Myra only runs local Llama-family GGUF models — no external agentic APIs.
The largest model that comfortably fits in the panel's RAM is selected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import settings


@dataclass(frozen=True)
class ModelTier:
    name: str
    repo_id: str
    filename: str
    min_ram_gb: float  # usable RAM required (after the reserve)
    context_size: int
    description: str


# Ordered small -> large. Selection picks the last tier that fits.
TIERS: tuple[ModelTier, ...] = (
    ModelTier(
        name="compact",
        repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        min_ram_gb=3.0,
        context_size=8192,
        description="Llama 3.2 3B Instruct (Q4_K_M) — minimum viable panel",
    ),
    ModelTier(
        name="standard",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        min_ram_gb=7.0,
        context_size=16384,
        description="Llama 3.1 8B Instruct (Q4_K_M) — balanced default",
    ),
    ModelTier(
        name="quality",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        filename="Meta-Llama-3.1-8B-Instruct-Q6_K.gguf",
        min_ram_gb=11.0,
        context_size=32768,
        description="Llama 3.1 8B Instruct (Q6_K) — higher fidelity",
    ),
    ModelTier(
        name="large",
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        filename="Llama-3.3-70B-Instruct-Q3_K_M.gguf",
        min_ram_gb=38.0,
        context_size=32768,
        description="Llama 3.3 70B Instruct (Q3_K_M) — large panel",
    ),
    ModelTier(
        name="xlarge",
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        filename="Llama-3.3-70B-Instruct-Q5_K_M.gguf",
        min_ram_gb=56.0,
        context_size=32768,
        description="Llama 3.3 70B Instruct (Q5_K_M) — high-memory panel",
    ),
)


def _cgroup_limit_bytes() -> float | None:
    """Pterodactyl containers are memory-capped by cgroups, not by host RAM."""
    candidates = [
        Path("/sys/fs/cgroup/memory.max"),  # cgroup v2
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1
    ]
    for path in candidates:
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if raw in {"max", ""}:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # Unlimited cgroups report an absurdly large sentinel value.
        if 0 < value < (1 << 50):
            return value
    return None


def detect_total_ram_gb() -> float:
    """Total RAM available to this container, in GB."""
    if settings.ram_override_gb > 0:
        return settings.ram_override_gb

    limits: list[float] = []
    cgroup = _cgroup_limit_bytes()
    if cgroup:
        limits.append(cgroup)
    try:
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        pass
    if not limits:
        return 4.0
    return round(min(limits) / (1024**3), 2)


def select_tier(total_ram_gb: float | None = None) -> ModelTier:
    total = detect_total_ram_gb() if total_ram_gb is None else total_ram_gb
    usable = max(total - settings.ram_reserve_gb, 0.0)
    chosen = TIERS[0]
    for tier in TIERS:
        if usable >= tier.min_ram_gb:
            chosen = tier
    return chosen


def resolve_model_spec() -> tuple[str, str, ModelTier]:
    """Return (repo_id, filename, tier) honouring env overrides."""
    tier = select_tier()
    repo_id = settings.model_repo or tier.repo_id
    filename = settings.model_file or tier.filename
    return repo_id, filename, tier
