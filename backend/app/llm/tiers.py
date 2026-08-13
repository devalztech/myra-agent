"""RAM-aware model tier selection.

Myra only runs local Llama-family GGUF models — no external agentic APIs.
The largest model that *actually fits and stays fast* on the panel is
selected. "Fits" means both:

  * the quantised weights fit in usable RAM alongside the KV cache and the
    Python/API process itself, and
  * the weights are under the speed cap (MYRA_MAX_AUTO_MODEL_GB), because a
    model can fit in RAM and still be unusably slow on a CPU-only panel.

This used to only compare a hand-written ``min_ram_gb`` per tier and fall
back to the *first* tier when nothing matched — which meant a 3 GB panel got
the 3B/2 GB "compact" model. With 1.5 GB reserved for the OS + API that model
does not fit at all, so the process either got OOM-killed or thrashed swap at
well under 1 token/second. A "nano" 1B tier is now the floor, and selection
checks the real weight size against usable RAM.
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
    size_gb: float  # approximate on-disk weight size
    context_size: int
    description: str


# Ordered small -> large. Selection picks the largest tier that fits.
# The two "coder" tiers at the floor are agentic/coding-tuned models chosen
# for tiny panels (3 GB RAM class): they follow tool-call JSON protocols far
# better than a general 1B chat model at the same footprint.
TIERS: tuple[ModelTier, ...] = (
    ModelTier(
        name="coder-nano",
        repo_id="bartowski/Qwen2.5-Coder-0.5B-Instruct-GGUF",
        filename="Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf",
        min_ram_gb=0.7,
        size_gb=0.4,
        context_size=4096,
        description="Qwen2.5-Coder 0.5B Instruct (Q4_K_M) — absolute floor, very fast",
    ),
    ModelTier(
        name="nano",
        repo_id="bartowski/Llama-3.2-1B-Instruct-GGUF",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        min_ram_gb=1.2,
        size_gb=0.81,
        context_size=4096,
        description="Llama 3.2 1B Instruct (Q4_K_M) — small/low-RAM panel, fastest",
    ),
    ModelTier(
        name="coder",
        repo_id="bartowski/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        filename="Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf",
        min_ram_gb=1.3,
        size_gb=1.1,
        context_size=8192,
        description="Qwen2.5-Coder 1.5B Instruct (Q4_K_M) — recommended for a 3 GB panel",
    ),

    ModelTier(
        name="compact",
        repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        min_ram_gb=2.8,
        size_gb=2.02,
        context_size=4096,
        description="Llama 3.2 3B Instruct (Q4_K_M) — mid panel",
    ),
    ModelTier(
        name="standard",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        min_ram_gb=6.5,
        size_gb=4.92,
        context_size=8192,
        description="Llama 3.1 8B Instruct (Q4_K_M) — balanced default",
    ),
    ModelTier(
        name="quality",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        filename="Meta-Llama-3.1-8B-Instruct-Q6_K.gguf",
        min_ram_gb=9.0,
        size_gb=6.6,
        context_size=8192,
        description="Llama 3.1 8B Instruct (Q6_K) — higher fidelity",
    ),
    ModelTier(
        name="large",
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        filename="Llama-3.3-70B-Instruct-Q3_K_M.gguf",
        min_ram_gb=38.0,
        size_gb=34.0,
        context_size=8192,
        description="Llama 3.3 70B Instruct (Q3_K_M) — large panel",
    ),
    ModelTier(
        name="xlarge",
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        filename="Llama-3.3-70B-Instruct-Q5_K_M.gguf",
        min_ram_gb=56.0,
        size_gb=50.0,
        context_size=8192,
        description="Llama 3.3 70B Instruct (Q5_K_M) — high-memory panel",
    ),
)

TIERS_BY_NAME = {tier.name: tier for tier in TIERS}


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


def kv_cache_gb(context_size: int) -> float:
    """Rough KV-cache footprint for a Llama-class model at this context.

    ~0.13 GB per 4k of context for the small models, which is close enough
    to keep a 3 GB panel from over-committing.
    """
    return 0.13 * (context_size / 4096)


def select_tier(total_ram_gb: float | None = None) -> ModelTier:
    """Pick the largest tier that fits BOTH RAM and the speed budget.

    An explicit MYRA_MODEL_TIER always wins. Otherwise selection is capped by
    MYRA_MAX_AUTO_MODEL_GB (default 8 GB of weights): a 70B model technically
    "fits" in 250 GB of RAM but generates well under 1 token/second on CPU,
    which reads as a broken chat to the user. Raise the cap deliberately if
    you have GPU offload configured.
    """
    forced = getattr(settings, "model_tier", "")
    if forced and forced in TIERS_BY_NAME:
        return TIERS_BY_NAME[forced]

    total = detect_total_ram_gb() if total_ram_gb is None else total_ram_gb
    usable = max(total - settings.ram_reserve_gb, 0.0)
    cap = getattr(settings, "max_auto_model_gb", 8.0)
    # With GPU offload the size cap is irrelevant — VRAM does the work.
    if settings.gpu_layers != 0:
        cap = float("inf")

    chosen = TIERS[0]  # nano is the floor — always runnable
    for tier in TIERS:
        if tier.size_gb > cap:
            continue
        needed = max(tier.min_ram_gb, tier.size_gb + kv_cache_gb(tier.context_size) + 0.35)
        if usable >= needed:
            chosen = tier
    return chosen


def resolve_model_spec() -> tuple[str, str, ModelTier]:
    """Return (repo_id, filename, tier) honouring env overrides."""
    tier = select_tier()
    repo_id = settings.model_repo or tier.repo_id
    filename = settings.model_file or tier.filename
    return repo_id, filename, tier
