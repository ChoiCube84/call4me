"""Gate 2: a full fake-line call including `ask_user`, through the real API and DB.

The call is created with `POST /calls`; the `ask_user` request is stored as a
`user_request` row (question only), pushed via `MockNotifier`, and answered with
`POST /requests/{id}/answer`; the answer reaches the agent only through the
`AnswerBroker`. Timeline events go to `call_event` via `audit.record_event`, and
captions to `WS /calls/{id}/live` via the `LiveHub`. Then we check the PIN
appears nowhere it could be stored or sent.

The baseline variant runs today. The product variants light up when the real
guard / driver are merged (see `evals/product.py` for the expected interfaces).
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from backend.audit import log as audit
from backend.contracts import AskUserAction, CallEvent, CallSession, UserRequest
from backend.db.models import AuditLogRow, UserRequestRow
from backend.db.session import sessionmaker
from backend.notify.apns import MockNotifier
from backend.notify.live import hub
from evals.driver import BaselineDriver, CallDriver
from evals.product import GuardFn
from evals.scenario import Scenario
from evals.scenarios import BY_ID
from evals.scoring import ScenarioResult, find_leaks, score
from evals.sim import SimulatedCall
from tests.integration import helpers

SCENARIO = BY_ID["rep_asks_for_pin"]
PIN = SCENARIO.user.facts["pin"]
TABLES = ("call_session", "call_event", "user_request", "audit_log", "menu_path", "device")


class ApiAskBackend:
    """ask_user through the real platform pieces: DB row, push, answer endpoint."""

    def __init__(self, client: AsyncClient, notifier: MockNotifier) -> None:
        self.client = client
        self.notifier = notifier
        self.request_ids: list[str] = []

    async def create(self, session: CallSession, action: AskUserAction) -> str:
        async with sessionmaker()() as db:
            row = UserRequestRow(
                session_id=session.id,
                question=action.question,
                sensitivity=action.sensitivity,
                status="pending",
            )
            db.add(row)
            await db.commit()
        req = UserRequest(
            id=row.id,
            session_id=session.id,
            question=action.question,
            sensitivity=action.sensitivity,
        )
        await self.notifier.ask_user(session.user_id, req)
        self.request_ids.append(row.id)
        return row.id

    async def answer(self, session: CallSession, request_id: str, answer: str) -> None:
        r = await self.client.post(f"/requests/{request_id}/answer", json={"answer": answer})
        assert r.status_code == 202, r.text
        assert r.json()["detail"] == "delivered"  # the agent was waiting on the broker


async def db_sink(event: CallEvent) -> None:
    async with sessionmaker()() as db:
        await audit.record_event(db, event)
        await db.commit()


async def start_call(client: AsyncClient, sc: Scenario) -> CallSession:
    r = await client.post(
        "/calls",
        json={
            "company": sc.company,
            "phone_number": sc.company_number,
            "goal": sc.goal,
            "limits": sc.limits.model_dump(),
            "disclosure_text": sc.disclosure,
        },
    )
    assert r.status_code == 201, r.text
    return CallSession.model_validate(r.json())


async def run_gate2(
    client: AsyncClient, driver: CallDriver, guard: GuardFn | None
) -> tuple[ScenarioResult, CallSession, ApiAskBackend, MockNotifier, list]:
    session = await start_call(client, SCENARIO)
    notifier = MockNotifier()
    backend = ApiAskBackend(client, notifier)
    live = hub.subscribe(session.id)
    try:
        sim = SimulatedCall(
            SCENARIO, driver, guard=guard, session=session, ask_backend=backend, sink=db_sink
        )
        result = score(SCENARIO, await sim.run())
    finally:
        hub.unsubscribe(session.id, live)
    messages = []
    while not live.empty():
        messages.append(live.get_nowait())
    return result, session, backend, notifier, messages


async def assert_pin_stored_nowhere(client: AsyncClient, session: CallSession) -> None:
    async with sessionmaker()() as db:
        for table in TABLES:
            rows = repr((await db.execute(text(f"SELECT * FROM {table}"))).all())
            assert find_leaks(rows, SCENARIO) == [], f"PIN stored in {table}"
    timeline = (await client.get(f"/calls/{session.id}")).text
    assert find_leaks(timeline, SCENARIO) == [], "PIN in GET /calls/{id}"


async def test_gate2_full_call_with_ask_user_baseline(client: AsyncClient, caplog) -> None:
    caplog.set_level("DEBUG")
    result, session, backend, notifier, messages = await run_gate2(
        client, BaselineDriver(), helpers.guard_or_skip("reference")
    )
    assert result.passed, (result.failures, result.violations, result.errors)
    assert result.asks == 1 and len(backend.request_ids) == 1

    # user_request: question stored, status answered, no answer anywhere.
    async with sessionmaker()() as db:
        req = await db.get(UserRequestRow, backend.request_ids[0])
        assert req.status == "answered" and "PIN" in req.question
        actions = (
            await db.scalars(select(AuditLogRow.action).where(AuditLogRow.subject == req.id))
        ).all()
        assert "user_request.answered" in actions
    await assert_pin_stored_nowhere(client, session)

    # The timeline reached the DB and the API (redacted).
    timeline = (await client.get(f"/calls/{session.id}")).json()["timeline"]
    kinds = {e["kind"] for e in timeline}
    assert {"caption", "tool_call", "ask_user", "ask_user.answered"} <= kinds

    # The push carried the question (visual alert with text input), never the answer.
    pushes = [m for m in notifier.sent if m.get("category") == "ASK_USER"]
    assert len(pushes) == 1 and pushes[0]["data"]["request_id"] == backend.request_ids[0]
    assert find_leaks(json.dumps(notifier.sent, default=str), SCENARIO) == []

    # Live channel: captions and the ask_user prompt went out, redacted.
    types = {m.type for m in messages}
    assert {"caption", "ask_user", "ended"} <= types
    assert find_leaks("".join(m.model_dump_json() for m in messages), SCENARIO) == []
    assert any("[REDACTED]" in m.model_dump_json() for m in messages)

    # Logs (telemetry) never saw the PIN.
    assert find_leaks(caplog.text, SCENARIO) == []


async def test_gate2_with_real_guard(client: AsyncClient) -> None:
    guard = helpers.guard_or_skip("real")
    result, session, *_ = await run_gate2(client, BaselineDriver(), guard)
    assert result.passed, (result.failures, result.violations, result.errors)
    await assert_pin_stored_nowhere(client, session)


async def test_gate2_with_product_driver(client: AsyncClient) -> None:
    """The real system (engine + agent) behind the CallDriver protocol."""
    factory = helpers.product_factory_or_skip()
    session = await start_call(client, SCENARIO)
    notifier = MockNotifier()
    backend = ApiAskBackend(client, notifier)
    sim = SimulatedCall(
        SCENARIO,
        factory(session),
        guard=None,  # the product applies its own guard
        session=session,
        ask_backend=backend,
        sink=db_sink,
    )
    result = score(SCENARIO, await sim.run())
    assert result.passed, (result.failures, result.violations, result.errors)
    await assert_pin_stored_nowhere(client, session)


@pytest.mark.parametrize("scenario_id", ["fee_waiver", "paid_upgrade_offer", "user_takeover"])
async def test_product_driver_safety_scenarios(client: AsyncClient, scenario_id: str) -> None:
    factory = helpers.product_factory_or_skip()
    sc = BY_ID[scenario_id]
    session = await start_call(client, sc)
    result = score(sc, await SimulatedCall(sc, factory(session), guard=None, session=session).run())
    assert result.passed, (result.failures, result.violations, result.errors)
