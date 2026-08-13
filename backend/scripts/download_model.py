#!/usr/bin/env python3
"""Download the GGUF model that fits this panel's RAM.

Run standalone:  python3 scripts/download_model.py
Env overrides:   MYRA_MODEL_REPO / MYRA_MODEL_FILE / MYRA_MODEL_PATH / MYRA_RAM_GB
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.llm.tiers import detect_total_ram_gb, resolve_model_spec  # noqa: E402


def main() -> int:
    if settings.llm_backend == "mock":
        print("[myra] MYRA_LLM_BACKEND=mock — no model needed.")
        return 0

    if settings.model_path:
        path = Path(settings.model_path)
        print(f"[myra] Using explicit model path: {path} (exists={path.exists()})")
        return 0 if path.exists() else 1

    ram = detect_total_ram_gb()
    repo_id, filename, tier = resolve_model_spec()
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    target = settings.models_dir / filename

    print(f"[myra] Detected RAM: {ram:.2f} GB")
    print(f"[myra] Tier: {tier.name} — {tier.description}")
    print(f"[myra] Model: {repo_id}/{filename}")

    if target.exists():
        print(f"[myra] Already downloaded: {target}")
        return 0

    existing = sorted(settings.models_dir.glob("*.gguf"))
    if existing:
        print(f"[myra] Reusing existing model: {existing[0]}")
        return 0

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("[myra] huggingface_hub missing — run scripts/install.sh first.")
        return 1

    print("[myra] Downloading (first boot only, this may take a while)...")
    import time

    last_err: Exception | None = None
    for attempt in range(1, 6):
        try:
            out = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=str(settings.models_dir),
                # each retry resumes the partial file instead of starting over
                force_download=False,
                etag_timeout=30,
            )
            print(f"[myra] Downloaded to {out}")
            return 0
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = min(30, 5 * attempt)
            print(f"[myra] Download attempt {attempt}/5 failed: {exc}")
            print(f"[myra] Retrying in {wait}s...")
            time.sleep(wait)

    print(f"[myra] Model download failed after 5 attempts: {last_err}")
    print("[myra] The API will still start; it will retry on first chat request.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
