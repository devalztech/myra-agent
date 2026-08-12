"""Myra Agent backend — FastAPI application entry point.

Chat-only scope for now (no task execution / coding tools).
Inference is 100% local (llama.cpp + GGUF Llama models).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import init_db
from .llm.engine import get_engine
from .llm.tiers import detect_total_ram_gb, select_tier
from .routers import auth, chat, sessions

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("myra")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    ram = detect_total_ram_gb()
    tier = select_tier(ram)
    logger.info(
        "Database: %s", "sqlite (%s)" % settings.sqlite_path if settings.is_sqlite else "postgresql"
    )
    logger.info("Detected RAM: %.2f GB -> tier '%s' (%s)", ram, tier.name, tier.description)
    if settings.preload_model:
        engine = get_engine()
        try:
            engine.load()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover
            logger.warning("Model preload skipped: %s", exc)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=None if "*" in settings.cors_origins else r"https://.*\.trycloudflare\.com",
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(chat.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {
        "status": "ok",
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "llm_backend": settings.llm_backend,
    }


@app.get("/", tags=["health"])
def root() -> dict:
    return {"name": settings.app_name, "docs": "/docs", "health": "/health"}
