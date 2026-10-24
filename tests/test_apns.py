"""APNs notifier: payload shape, auth, routing, and failure handling (offline)."""

from __future__ import annotations

import json
import logging

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.audit import log as audit
from backend.config import Settings
from backend.contracts import CallState, Sensitivity, UserRequest
from backend.db.models import DeviceRow
from backend.db.session import sessionmaker
from backend.notify import apns
from backend.notify.apns import (
    APNS_PRODUCTION_HOST,
    APNS_SANDBOX_HOST,
    APNsNotifier,
    MockNotifier,
    get_notifier,
)

TOPIC = "com.call4me.app"


@pytest.fixture
def ec_key(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "AuthKey_TEST.p8"
    path.write_bytes(pem)
    return key, path


@pytest.fixture
def settings(ec_key) -> Settings:
    return Settings(
        _env_file=None,
        apns_key_id="KEY123",
        apns_team_id="TEAM456",
        apns_auth_key_path=str(ec_key[1]),
        apns_topic=TOPIC,
        use_mocks=False,
    )


class Recorder:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    def payload(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def make(settings, recorder, *, tokens=("dev-token", "la-token"), clock=None):
    async def lookup(user_id: str):
        return tokens

    kwargs = {"clock": clock} if clock else {}
    return APNsNotifier(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        device_lookup=lookup,
        **kwargs,
    )


async def test_alert_is_visible_time_sensitive_with_category_and_data(settings, ec_key):
    rec = Recorder()
    n = make(settings, rec)
    await n.alert(
        "u1", "Person answered", "A rep is on the line", category="HUMAN", data={"session_id": "s1"}
    )

    req = rec.requests[0]
    assert str(req.url) == f"{APNS_SANDBOX_HOST}/3/device/dev-token"
    assert req.headers["apns-topic"] == TOPIC
    assert req.headers["apns-push-type"] == "alert"
    assert req.headers["apns-priority"] == "10"
    body = rec.payload()
    aps = body["aps"]
    assert aps["alert"] == {"title": "Person answered", "body": "A rep is on the line"}
    assert aps["interruption-level"] == "time-sensitive"
    assert aps["category"] == "HUMAN"
    assert body["session_id"] == "s1"

    # ES256 provider token signed with our key, with kid/iss set.
    token = req.headers["authorization"].removeprefix("bearer ")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256" and header["kid"] == "KEY123"
    claims = jwt.decode(token, ec_key[0].public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM456" and "iat" in claims


async def test_alert_never_sound_only(settings):
    rec = Recorder()
    await make(settings, rec).alert("u1", "", "")
    assert rec.requests == []


async def test_ask_user_sends_question_only(settings):
    rec = Recorder()
    req = UserRequest(
        session_id="s1", question="What is your account PIN?", sensitivity=Sensitivity.MEDIUM
    )
    await make(settings, rec).ask_user("u1", req)
    body = rec.payload()
    assert body["aps"]["category"] == "ASK_USER"
    assert body["aps"]["alert"]["body"] == "What is your account PIN?"
    assert body["request_id"] == req.id and body["session_id"] == "s1"
    assert "answer" not in json.dumps(body).lower()


async def test_payload_is_redacted(settings):
    audit.register_secret("s1", "4821")
    rec = Recorder()
    await make(settings, rec).alert(
        "u1", "Update", "Rep confirmed PIN 4821", data={"answer": "4821", "session_id": "s1"}
    )
    raw = rec.requests[0].content.decode()
    assert "4821" not in raw
    assert rec.payload()["answer"] == audit.REDACTED


@pytest.mark.parametrize(
    ("state", "label", "event", "priority"),
    [
        (CallState.DIALING, "Calling", "update", "5"),
        (CallState.MENU, "Navigating menu", "update", "5"),
        (CallState.HOLD, "On hold", "update", "5"),
        (CallState.HUMAN_CONFIRMED, "Person answered", "update", "10"),
        (CallState.WAITING_ON_USER, "Needs your input", "update", "10"),
        (CallState.TAKEOVER, "You're typing", "update", "10"),
        (CallState.ENDED, "Done", "end", "10"),
        (CallState.FAILED, "Done", "end", "10"),
    ],
)
async def test_live_activity(settings, state, label, event, priority):
    rec = Recorder()
    n = make(settings, rec, clock=lambda: 1_700_000_000.0)
    await n.update_live_activity("u1", state, {"session_id": "s1"})

    req = rec.requests[0]
    assert str(req.url).endswith("/3/device/la-token")
    assert req.headers["apns-push-type"] == "liveactivity"
    assert req.headers["apns-topic"] == f"{TOPIC}.push-type.liveactivity"
    assert req.headers["apns-priority"] == priority
    aps = rec.payload()["aps"]
    assert aps["event"] == event
    assert aps["timestamp"] == 1_700_000_000
    expected = {"state": state.value, "startedAt": 1_700_000_000, "detail": label}
    if state is CallState.HOLD:
        expected["holdStartedAt"] = 1_700_000_000
    assert aps["content-state"] == expected
    assert ("dismissal-date" in aps) == (event == "end")


async def test_live_activity_timeline_tracks_call_start_and_hold_stretch(settings):
    now = [1000.0]
    rec = Recorder()
    n = make(settings, rec, clock=lambda: now[0])

    async def push(state):
        await n.update_live_activity("u1", state, {"session_id": "s1"})
        return rec.payload()["aps"]["content-state"]

    assert await push(CallState.DIALING) == {
        "state": "DIALING",
        "startedAt": 1000,
        "detail": "Calling",
    }
    now[0] = 1010
    assert "holdStartedAt" not in await push(CallState.MENU)
    now[0] = 1030
    assert (await push(CallState.HOLD))["holdStartedAt"] == 1030
    now[0] = 1090
    cs = await push(CallState.RECORDED_MESSAGE)  # still the same waiting stretch
    assert cs["holdStartedAt"] == 1030 and cs["startedAt"] == 1000
    now[0] = 1200
    cs = await push(CallState.HUMAN_CONFIRMED)
    assert "holdStartedAt" not in cs and cs["detail"] == "Person answered"
    now[0] = 1300
    assert (await push(CallState.HOLD))["holdStartedAt"] == 1300  # new stretch (transfer)
    now[0] = 1400
    cs = await push(CallState.ENDED)
    assert cs["startedAt"] == 1000 and rec.payload()["aps"]["event"] == "end"
    assert "s1" not in n._timelines  # dropped at the end of the call


async def test_live_activity_started_at_from_data(settings):
    from datetime import UTC, datetime

    rec = Recorder()
    n = make(settings, rec, clock=lambda: 2000.0)
    started = datetime.fromtimestamp(1500, UTC)
    await n.update_live_activity("u1", CallState.MENU, {"session_id": "s1", "started_at": started})
    assert rec.payload()["aps"]["content-state"]["startedAt"] == 1500


async def test_alert_passes_category_through(settings):
    rec = Recorder()
    for cat in ("HUMAN_ANSWERED", "HOLD_CONTINUE", "CALLBACK", "CALL_ENDED", "TAKEOVER"):
        await make(settings, rec).alert(
            "u1", "t", "b", category=cat, data={"session_id": "s1", "request_id": "r1"}
        )
        body = rec.payload()
        assert body["aps"]["category"] == cat
        assert body["session_id"] == "s1" and body["request_id"] == "r1"


async def test_every_state_has_a_label():
    assert all(apns.live_activity_label(s) for s in CallState)


async def test_token_cached_then_refreshed(settings):
    now = [1_000_000.0]
    rec = Recorder()
    n = make(settings, rec, clock=lambda: now[0])
    await n.alert("u1", "a", "b")
    await n.alert("u1", "a", "b")
    now[0] += 51 * 60
    await n.alert("u1", "a", "b")
    auths = [r.headers["authorization"] for r in rec.requests]
    assert auths[0] == auths[1] != auths[2]


async def test_production_host_when_sandbox_off(settings):
    settings.__dict__["apns_use_sandbox"] = False  # field added to config.py by the lead
    rec = Recorder()
    await make(settings, rec).alert("u1", "a", "b")
    assert str(rec.requests[0].url).startswith(APNS_PRODUCTION_HOST)


async def test_rejection_and_transport_errors_never_raise(settings, caplog):
    rec = Recorder(status=400, body={"reason": "BadDeviceToken"})
    with caplog.at_level(logging.WARNING):
        await make(settings, rec).alert("u1", "a", "b")
    assert "BadDeviceToken" in caplog.text

    def boom(request):
        raise httpx.ConnectError("down")

    async def lookup(user_id):
        return "t", "t"

    n = APNsNotifier(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        device_lookup=lookup,
    )
    await n.alert("u1", "a", "b")
    await n.update_live_activity("u1", CallState.HOLD, {"session_id": "s1"})


async def test_missing_key_file_never_raises(settings):
    settings.apns_auth_key_path = "/nonexistent/key.p8"
    rec = Recorder()
    await make(settings, rec).alert("u1", "a", "b")
    assert rec.requests == []


async def test_no_device_tokens_skips(settings):
    rec = Recorder()
    n = make(settings, rec, tokens=(None, None))
    await n.alert("u1", "a", "b")
    await n.update_live_activity("u1", CallState.HOLD)
    assert rec.requests == []


async def test_lookup_reads_device_table(db, settings):
    async with sessionmaker()() as s:
        s.add(DeviceRow(user_id="u1", apns_token="abc", live_activity_token="xyz"))
        await s.commit()
    rec = Recorder()
    n = APNsNotifier(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(rec)))
    await n.alert("u1", "a", "b")
    await n.update_live_activity("u1", CallState.MENU, {"session_id": "s1"})
    await n.alert("nobody", "a", "b")
    assert [str(r.url).rsplit("/", 1)[-1] for r in rec.requests] == ["abc", "xyz"]


def test_get_notifier_selection(monkeypatch, settings, caplog):
    monkeypatch.setattr(apns, "_apns", None)
    monkeypatch.setattr(
        apns, "get_settings", lambda: settings.model_copy(update={"use_mocks": True})
    )
    assert isinstance(get_notifier(), MockNotifier)

    monkeypatch.setattr(
        apns, "get_settings", lambda: settings.model_copy(update={"apns_topic": ""})
    )
    with caplog.at_level(logging.WARNING):
        assert isinstance(get_notifier(), MockNotifier)
    assert "APNs settings missing" in caplog.text

    monkeypatch.setattr(apns, "get_settings", lambda: settings)
    first = get_notifier()
    assert isinstance(first, APNsNotifier) and get_notifier() is first


async def test_key_content_setting_overrides_path(settings, ec_key):
    # Azure: the .p8 contents arrive from Key Vault as `apns_auth_key` (field added by the lead).
    pem = ec_key[1].read_text()
    settings.apns_auth_key_path = ""
    settings.__dict__["apns_auth_key"] = pem.replace("\n", "\\n")
    assert apns.apns_configured(settings)
    rec = Recorder()
    await make(settings, rec).alert("u1", "a", "b")
    token = rec.requests[0].headers["authorization"].removeprefix("bearer ")
    jwt.decode(token, ec_key[0].public_key(), algorithms=["ES256"])
