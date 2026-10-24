"""Cartesia calls: setup, dialing, the event webhook, and our webhook tools.

A fake Cartesia API (httpx.MockTransport) stands in for api.cartesia.ai.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from backend import calls
from backend.cartesia import setup as cartesia_setup
from backend.cartesia import spec
from backend.cartesia.api import CartesiaAgentsApi
from backend.cartesia.call import CartesiaCall, by_call_id
from backend.contracts import CallLimits, CallSession, CallState
from backend.notify.live import answers, hub

SETTINGS = SimpleNamespace(
    cartesia_api_key="sk_car_test",
    cartesia_voice_id="voice-1",
    cartesia_agent_model="claude-haiku-5.5",
    cartesia_webhook_secret="",
    public_base_url="https://call4me.example",
)
ORDER = "112-4567890-1234567"


class FakeCartesia:
    """Records requests; keeps agents/tools/webhooks/numbers in memory."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.webhooks: list[dict[str, Any]] = []
        self.numbers: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path, method = request.url.path, request.method
        self.requests.append((method, path, body))
        assert request.headers["Cartesia-Version"] == "2026-08-14"
        assert request.headers["Authorization"] == "Bearer sk_car_test"
        if path == "/agents/webhooks":
            if method == "GET":
                return httpx.Response(200, json={"data": self.webhooks})
            hook = {"id": f"wh_{len(self.webhooks)}", **body}
            self.webhooks.append(hook)
            return httpx.Response(200, json=hook)
        if path.startswith("/agents/webhooks/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        if path == "/v1/agents/tools":
            if method == "GET":
                return httpx.Response(200, json={"data": self.tools})
            tool = {"id": f"tool_{len(self.tools)}", **body}
            self.tools.append(tool)
            return httpx.Response(201, json=tool)
        if path.startswith("/v1/agents/tools/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        if path == "/v1/agents":
            if method == "GET":
                return httpx.Response(200, json={"data": self.agents})
            agent = {"id": f"agent_{len(self.agents)}", **body}
            self.agents.append(agent)
            return httpx.Response(201, json=agent)
        if path.startswith("/v1/agents/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        if path == "/agents/phone-numbers":
            return httpx.Response(200, json={"data": self.numbers})
        if path == "/agents/phone-numbers/provision":
            n = {"id": "pn_1", "number": "+14155550000", "label": body["label"]}
            self.numbers.append(n)
            return httpx.Response(201, json=n)
        if path == "/agents/calls" and method == "POST":
            return httpx.Response(200, json={"calls": [{"number": "x", "agent_call_id": "ac_1"}]})
        if path.endswith("/cancel"):
            self.cancelled.append(path.split("/")[3])
            return httpx.Response(200, json={"cancelled": True})
        if path.startswith("/agents/calls/") and method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        if path.startswith("/agents/calls/"):
            return httpx.Response(200, json={"status": "started"})
        return httpx.Response(404, text=f"no route {method} {path}")

    def api(self) -> CartesiaAgentsApi:
        return CartesiaAgentsApi("sk_car_test", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake() -> FakeCartesia:
    return FakeCartesia()


def session(**limits: Any) -> CallSession:
    return CallSession(
        user_id="u",
        company="Amazon Customer Service",
        phone_number="+14155551234",
        goal="Get a refund for headphones that arrived damaged.",
        limits=CallLimits(**limits),
        disclosure_text="Hi, I'm an AI assistant calling for my client.",
    )


async def started_call(fake: FakeCartesia, **limits: Any) -> CartesiaCall:
    await cartesia_setup.ensure(SETTINGS, fake.api())
    call = CartesiaCall(
        session(**limits),
        settings=SETTINGS,
        known_info={"Order number": ORDER},
        api=fake.api(),
        persist=False,
    )
    calls.registry[call.id] = call
    await call.start()
    return call


# --- setup ------------------------------------------------------------------------


async def test_setup_creates_everything_once_then_updates(fake: FakeCartesia) -> None:
    s = await cartesia_setup.ensure(SETTINGS, fake.api())
    assert s.agent_id == "agent_0" and s.from_number == "+14155550000"
    assert [t["name"] for t in fake.tools] == [
        "call4me_ask_user",
        "call4me_check_offer",
        "call4me_report_state",
        "call4me_finish",
    ]
    tool = fake.tools[0]
    assert tool["api_schema"]["url"] == "https://call4me.example/cartesia/tools/ask_user"
    body = tool["api_schema"]["request_body_schema"]["properties"]
    assert body["session_id"] == {"type": "string", "dynamic_variable": "session_id"}
    assert fake.webhooks[0]["url"] == "https://call4me.example/cartesia/webhook"
    config = fake.agents[0]["config"]
    assert config["model"]["id"] == "claude-haiku-5.5"
    assert config["initial_message"] is None  # the company speaks first
    assert config["system_tools"]["send_dtmf"] == {}

    n_requests = len(fake.requests)
    await cartesia_setup.ensure(SETTINGS, fake.api())  # idempotent: no new resources
    assert len(fake.agents) == 1 and len(fake.tools) == 4 and len(fake.numbers) == 1
    assert len(fake.requests) > n_requests


async def test_setup_needs_a_public_https_url(fake: FakeCartesia) -> None:
    bad = SimpleNamespace(**{**vars(SETTINGS), "public_base_url": ""})
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        await cartesia_setup.ensure(bad, fake.api())


# --- dialing ------------------------------------------------------------------------


async def test_start_dials_with_call_facts_as_variables(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    (_, _, body) = next(r for r in fake.requests if r[1] == "/agents/calls")
    assert body["agent_id"] == "agent_0" and body["from_number_id"] == "pn_1"
    (dest,) = body["outbound_calls"]
    assert dest["to_number"] == "+14155551234"
    v = dest["dynamic_variables"]
    assert v["session_id"] == call.id and v["company"] == "Amazon Customer Service"
    assert ORDER in v["known_info"] and "No new charges" in v["limits"]
    assert by_call_id["ac_1"] is call
    await call.finish({"status": "completed"})


# --- the event webhook ---------------------------------------------------------------


async def test_turns_become_redacted_captions(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    q = hub.subscribe(call.id)
    await call.on_event(
        {"type": "call_turn", "call_id": "ac_1", "turn": {"role": "user", "text": "Order number?"}}
    )
    await call.on_event(
        {
            "type": "call_turn",
            "call_id": "ac_1",
            "turn": {"role": "assistant", "text": f"The order number is {ORDER}."},
        }
    )
    rep, agent = q.get_nowait(), q.get_nowait()
    assert rep.data == {"speaker": "remote", "text": "Order number?", "final": True}
    assert agent.data["speaker"] == "agent" and ORDER not in agent.data["text"]
    hub.unsubscribe(call.id, q)
    await call.finish({"status": "completed"})


async def test_completed_call_wraps_up_and_deletes_cartesia_copy(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    await call.tool("finish", {"summary": "Refund of $49.99 issued.", "reference_number": "A7X291"})
    q = hub.subscribe(call.id)
    await call.on_event(
        {
            "type": "call_completed",
            "call_id": "ac_1",
            "call": {"status": "completed", "end_reason": "agent_hangup"},
        }
    )
    msgs = [q.get_nowait() for _ in range(q.qsize())]
    ended = next(m for m in msgs if m.type == "ended")
    assert ended.data["outcome"] == "Refund of $49.99 issued."
    assert ended.data["reference_number"] == "A7X291"
    assert call.session.state is CallState.ENDED
    assert fake.deleted == ["ac_1"]  # rule 8: nothing left on Cartesia's side
    assert call.id not in calls.registry and "ac_1" not in by_call_id
    hub.unsubscribe(call.id, q)


# --- webhook tools -------------------------------------------------------------------


async def test_ask_user_returns_the_typed_answer(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    q = hub.subscribe(call.id)

    async def user_types() -> None:
        while True:
            msg = await q.get()
            if msg.type == "ask_user":
                answers.deliver(msg.data["request_id"], "98109")
                return

    typing = asyncio.create_task(user_types())
    reply = await call.tool(
        "ask_user", {"question": "ZIP code on the account?", "sensitivity": "low"}
    )
    await typing
    assert "The customer answered: 98109" in reply
    assert call.session.state is CallState.IN_CONVERSATION
    hub.unsubscribe(call.id, q)
    await call.finish({"status": "completed"})


async def test_ask_user_refuses_high_sensitivity(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    reply = await call.tool("ask_user", {"question": "Full card number?", "sensitivity": "high"})
    assert reply.startswith("Not allowed")
    await call.finish({"status": "completed"})


@pytest.mark.parametrize(
    ("limits", "args", "allowed"),
    [
        ({}, {"kind": "plan_change", "description": "Prime trial", "amount_usd": 14.99}, False),
        (
            {"allow_plan_changes": True, "max_spend_usd": 20},
            {"kind": "plan_change", "description": "Prime", "amount_usd": 14.99},
            True,
        ),
        ({}, {"kind": "cancellation", "description": "cancel order", "amount_usd": 0}, False),
        ({"max_spend_usd": 5}, {"kind": "charge", "description": "fee", "amount_usd": 9}, False),
        ({"max_spend_usd": 10}, {"kind": "charge", "description": "fee", "amount_usd": 9}, True),
    ],
)
async def test_check_offer_follows_the_users_limits(
    fake: FakeCartesia, limits: dict[str, Any], args: dict[str, Any], allowed: bool
) -> None:
    call = await started_call(fake, **limits)
    reply = await call.tool("check_offer", args)
    assert reply.startswith("Allowed") is allowed
    await call.finish({"status": "completed"})


async def test_report_state_walks_the_state_machine(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    await call.tool("report_state", {"state": "menu", "note": "pressed 2"})
    assert call.session.state is CallState.MENU
    await call.tool("report_state", {"state": "person", "note": "Alex answered"})
    assert call.session.state is CallState.IN_CONVERSATION
    await call.finish({"status": "completed"})


async def test_typed_message_rides_on_the_next_tool_result(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    await call.type_text("Please email me the label.")
    reply = await call.tool("report_state", {"state": "person", "note": ""})
    assert "Please email me the label." in reply
    assert "Please email me the label." not in await call.tool(
        "report_state", {"state": "person", "note": ""}
    )
    await call.finish({"status": "completed"})


async def test_cancel_cancels_the_cartesia_call(fake: FakeCartesia) -> None:
    call = await started_call(fake)
    await call.cancel()
    assert fake.cancelled == ["ac_1"]
    await call.finish({"status": "completed", "end_reason": "api_cancelled"})


# --- HTTP endpoints --------------------------------------------------------------------


async def test_endpoints_check_the_shared_secret(client, fake: FakeCartesia) -> None:
    from backend.config import get_settings

    secret = cartesia_setup.shared_secret(get_settings())
    r = await client.post(
        "/cartesia/webhook", json={"type": "call_turn"}, headers={"X-Webhook-Secret": "nope"}
    )
    assert r.status_code == 401
    r = await client.post(
        "/cartesia/webhook", json={"call_id": "unknown"}, headers={"X-Webhook-Secret": secret}
    )
    assert r.status_code == 200 and r.json() == {"ok": False}
    r = await client.post("/cartesia/tools/report_state", json={"session_id": "x"})
    assert r.status_code == 401
    r = await client.post(
        "/cartesia/tools/report_state",
        json={"session_id": "gone", "state": "menu", "note": ""},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert r.status_code == 200 and "no longer active" in r.text


async def test_phone_numbers_are_normalized(client) -> None:
    base = {
        "company": "Amazon Customer Service",
        "goal": "Refund",
        "disclosure_text": "Hi, I'm an AI assistant.",
    }
    r = await client.post("/calls", json={**base, "phone_number": "(415) 555-1234"})
    assert r.status_code == 201 and r.json()["phone_number"] == "+14155551234"
    r = await client.post("/calls", json={**base, "phone_number": "555-12"})
    assert r.status_code == 422


def test_spec_instructions_use_every_call_variable() -> None:
    for name in ("company", "goal", "limits", "known_info", "disclosure"):
        assert "{{" + name + "}}" in spec.INSTRUCTIONS


def test_tool_schemas_never_require_injected_fields() -> None:
    # Cartesia: "A parameter with constant_value or dynamic_variable cannot be required".
    from backend.cartesia import spec

    for t in spec.tool_specs("https://x.example", "s"):
        body = t["api_schema"]["request_body_schema"]
        injected = {
            k
            for k, v in body["properties"].items()
            if "dynamic_variable" in v or "constant_value" in v
        }
        assert injected and not injected & set(body["required"]), t["name"]
