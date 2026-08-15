"""Preview router (TOOLS/Preview).

Starts a local dev server for a workspace project and exposes it so the
frontend can render it in an iframe preview panel. The user gets a live
preview of whatever myra hosted in its sandbox — static HTML/CSS/JS sites
interactively, and for interactive-only previews myra (and only myra) can
drive the browser.

Endpoints:
  POST /preview/start      - start a preview for a workspace dir
  POST /preview/stop       - stop the preview
  GET  /preview/health     - health + running state
  GET  /preview/serve/...  - proxy into the running preview (rendered by iframe)
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..deps import get_current_user
from ..models import User
from ..services import preview as preview_svc
from ..workspace import safe_path, workspace_root

logger = logging.getLogger("myra.preview")

router = APIRouter(prefix="/preview", tags=["preview"])


class PreviewStartPayload(BaseModel):
    path: str = Field(default="", max_length=500)
    command: str | None = Field(default=None, max_length=500)


def _resolve(rel: str):
    root = workspace_root()
    rel = rel.strip().strip("/")
    return root if not rel else safe_path(root / rel)


@router.post("/start")
def preview_start(
    payload: PreviewStartPayload,
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    target = _resolve(payload.path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Workspace directory not found.")
    return preview_svc.start(target)


@router.post("/stop")
def preview_stop(
    payload: PreviewStartPayload,
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    return preview_svc.stop(_resolve(payload.path))


@router.get("/health")
def preview_health(path: str = "", user: User = Depends(get_current_user)) -> dict[str, object]:
    return preview_svc.health(_resolve(path))


@router.get("/serve/{dir_path:path}")
async def preview_serve(
    dir_path: str,
    user: User = Depends(get_current_user),
) -> Response:
    """Proxy a request into a running preview server.

    The preview runs on a random local port that the public Cloudflare tunnel
    does NOT forward. Instead of needing a second tunnel per preview, this
    endpoint proxies /preview/serve/<workspace-dir>/<path...> to the local
    preview process. The frontend renders this in an iframe, so the user can
    view hosted static HTML/CSS/JS sites through the SAME public URL as the
    app — no extra tunnel or exposed port required.
    """
    if "/" in dir_path:
        rel_dir, file_path = dir_path.split("/", 1)
    else:
        rel_dir, file_path = dir_path, ""

    target_dir = _resolve(rel_dir)
    health = preview_svc.health(target_dir)
    port = health.get("port") if health.get("ok") else 0
    if not port:
        raise HTTPException(status_code=404, detail="No preview running for this directory.")

    origin = f"http://127.0.0.1:{port}"
    target_url = f"{origin}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.request("GET", target_url, headers={"Host": "127.0.0.1"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Preview proxy failed: {exc}") from exc

    content_type = upstream.headers.get("content-type", "text/html")
    media = content_type.split(";")[0] if ";" in content_type else content_type
    return Response(content=upstream.content, media_type=media, status_code=upstream.status_code)
