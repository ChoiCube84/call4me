from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import get_settings


@pytest.fixture
def token(monkeypatch) -> str:
    monkeypatch.setattr(get_settings(), "api_token", "s3cret")
    return "s3cret"


async def test_no_token_configured_leaves_api_open(client) -> None:
    assert (await client.get("/companies", params={"q": "a"})).status_code == 200


async def test_api_needs_the_token(client, token) -> None:
    r = await client.get("/companies", params={"q": "a"})
    assert r.status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get("/companies", params={"q": "a"}, headers=bad)).status_code == 401
    good = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/companies", params={"q": "a"}, headers=good)).status_code == 200
    r = await client.get("/companies", params={"q": "a", "token": token})
    assert r.status_code == 200


async def test_probe_pages_and_cartesia_stay_open(client, token) -> None:
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/phone")).status_code == 307
    # Cartesia's own secret check answers, not ours.
    r = await client.post("/cartesia/webhook", json={})
    assert r.status_code == 401 and "secret" in r.text


def test_websocket_needs_the_token(db, token) -> None:
    from backend.main import app

    app.state.start_calls = False
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect), tc.websocket_connect("/calls/nope/live"):
            pass
        with tc.websocket_connect(f"/calls/nope/live?token={token}") as ws:
            ws.close()


# Paths that are meant to be reachable without the token.
OPEN_ROUTES = {"/health", "/phone", "/phone/ws", "/", "/console"}


def test_every_api_route_needs_the_token() -> None:
    """A new API route outside PROTECTED_PREFIXES would be open to the internet."""
    from backend.auth import is_protected
    from backend.main import app

    def paths(routes, prefix=""):
        for r in routes:
            path = prefix + getattr(r, "path", "")
            if hasattr(r, "routes") and not hasattr(r, "app"):  # included router
                yield from paths(r.routes, prefix)
            else:
                yield path

    unprotected = {
        p
        for p in paths(app.routes)
        if p and not is_protected(p) and p not in OPEN_ROUTES and not p.startswith("/cartesia/")
    }
    assert unprotected == set()
