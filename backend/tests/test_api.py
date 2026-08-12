"""End-to-end API tests (auth -> sessions -> chat -> streaming)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MYRA_LLM_BACKEND", "mock")
os.environ.setdefault("MYRA_JWT_SECRET", "test-secret")
_tmp_db = Path(tempfile.gettempdir()) / "myra-test.db"
_tmp_db.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = ""
os.environ["MYRA_SQLITE_PATH"] = str(_tmp_db)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    payload = {"name": "Denzel", "email": "denzel@example.com", "password": "supersecret"}
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    return data["token"], data["user"], payload


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_register_returns_token_and_user(auth):
    token, user, payload = auth
    assert token
    assert user["email"] == payload["email"]
    assert user["name"] == payload["name"]


def test_duplicate_registration_rejected(client, auth):
    _, _, payload = auth
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 409


def test_login_success_and_failure(client, auth):
    _, _, payload = auth
    ok = client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == payload["email"]

    bad = client.post("/auth/login", json={"email": payload["email"], "password": "wrong-pass"})
    assert bad.status_code == 401


def test_me_requires_token(client, auth):
    token, user, _ = auth
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=headers("garbage")).status_code == 401
    res = client.get("/auth/me", headers=headers(token))
    assert res.status_code == 200 and res.json()["id"] == user["id"]


def test_sessions_are_scoped_to_the_user(client, auth):
    token, _, _ = auth
    assert client.get("/sessions", headers=headers(token)).json() == []

    created = client.post("/sessions", headers=headers(token), json={})
    assert created.status_code == 201
    session_id = created.json()["id"]

    listed = client.get("/sessions", headers=headers(token)).json()
    assert [s["id"] for s in listed] == [session_id]

    other = client.post(
        "/auth/register",
        json={"name": "Other", "email": "other@example.com", "password": "supersecret"},
    ).json()
    assert client.get("/sessions", headers=headers(other["token"])).json() == []
    assert client.get(f"/sessions/{session_id}", headers=headers(other["token"])).status_code == 404


def test_chat_roundtrip_persists_messages(client, auth):
    token, _, _ = auth
    session_id = client.post("/sessions", headers=headers(token), json={}).json()["id"]

    res = client.post(
        f"/sessions/{session_id}/chat",
        headers=headers(token),
        json={"content": "How do I reverse a list in Python?"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["userMessage"]["content"].startswith("How do I reverse")
    assert body["assistantMessage"]["role"] == "assistant"
    assert body["assistantMessage"]["content"]
    # Title is derived from the first message.
    assert body["session"]["title"].startswith("How do I reverse")

    detail = client.get(f"/sessions/{session_id}", headers=headers(token)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_chat_stream_emits_sse_events(client, auth):
    token, _, _ = auth
    session_id = client.post("/sessions", headers=headers(token), json={}).json()["id"]

    with client.stream(
        "POST",
        f"/sessions/{session_id}/chat/stream",
        headers=headers(token),
        json={"content": "Explain list comprehensions."},
    ) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        raw = "".join(res.iter_text())

    for event in ("event: session", "event: user_message", "event: token", "event: assistant_message", "event: done"):
        assert event in raw, raw[:500]

    detail = client.get(f"/sessions/{session_id}", headers=headers(token)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"]


def test_chat_requires_auth_and_valid_session(client, auth):
    token, _, _ = auth
    assert client.post("/sessions/nope/chat", json={"content": "hi"}).status_code == 401
    assert (
        client.post("/sessions/nope/chat", headers=headers(token), json={"content": "hi"}).status_code
        == 404
    )


def test_rename_and_delete_session(client, auth):
    token, _, _ = auth
    session_id = client.post("/sessions", headers=headers(token), json={}).json()["id"]
    renamed = client.patch(
        f"/sessions/{session_id}", headers=headers(token), json={"title": "Renamed"}
    )
    assert renamed.status_code == 200 and renamed.json()["title"] == "Renamed"
    assert client.delete(f"/sessions/{session_id}", headers=headers(token)).status_code == 204
    assert client.get(f"/sessions/{session_id}", headers=headers(token)).status_code == 404


def test_model_status(client):
    res = client.get("/model")
    assert res.status_code == 200
    body = res.json()
    assert body["backend"] == "mock"
    assert body["ramGb"] > 0
    assert body["tier"]
