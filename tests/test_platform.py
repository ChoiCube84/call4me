"""Phase 0 platform tests: contracts, redaction, append-only audit, endpoints."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, text

from backend.audit import log as audit
from backend.contracts import AgentAction, AskUserAction, PressDigitsAction, Sensitivity
from backend.db.models import AppendOnlyError, AuditLogRow, UserRequestRow
from backend.db.session import sessionmaker
from backend.notify.live import answers

CALL = {
    "company": "Fake Support Line (test)",
    "phone_number": "+15550000000",
    "goal": "Remove the $40 late fee",
    "limits": {"notes": "no plan changes"},
    "disclosure_text": "Hi, I'm an AI assistant calling on behalf of Sam.",
}


# --- contracts --------------------------------------------------------------


def test_agent_action_discriminates_on_kind() -> None:
    action = TypeAdapter(AgentAction).validate_python(
        {"kind": "ask_user", "question": "PIN?", "sensitivity": "medium"}
    )
    assert isinstance(action, AskUserAction)
    assert action.sensitivity is Sensitivity.MEDIUM


def test_press_digits_rejects_non_dtmf() -> None:
    with pytest.raises(ValidationError):
        PressDigitsAction(digits="1; DROP TABLE")


def test_user_request_model_has_no_answer_field() -> None:
    assert "answer" not in UserRequestRow.__table__.columns


# --- redaction --------------------------------------------------------------


def test_redact_sensitive_keys_and_patterns() -> None:
    out = audit.redact(
        {"pin": "1234", "note": "ssn 123-45-6789, card 4111 1111 1111 1111", "ok": "A7X291"}
    )
    assert out["pin"] == audit.REDACTED
    assert "123-45-6789" not in out["note"] and "4111" not in out["note"]
    assert out["ok"] == "A7X291"  # reference numbers survive


def test_redact_registered_secret_including_spoken_digits() -> None:
    audit.register_secret("s1", "4821")
    out = audit.redact(["my pin is 4821", "that's 4 8 2 1, right?", "ref 948211"])
    assert out[0] == f"my pin is {audit.REDACTED}"
    assert out[1] == f"that's {audit.REDACTED}, right?"
    assert out[2] == "ref 948211"  # only whole matches
    audit.forget_session("s1")
    assert audit.redact("4821") == "4821"


# --- audit log is append-only -------------------------------------------------


async def test_audit_log_rejects_update_and_delete(db) -> None:
    async with sessionmaker()() as s:
        row_id = (await audit.append(s, actor="test", action="x", reason="r")).id
        await s.commit()
    async with sessionmaker()() as s:
        (await s.get(AuditLogRow, row_id)).reason = "edited"
        with pytest.raises(AppendOnlyError):
            await s.commit()
    async with sessionmaker()() as s:
        await s.delete(await s.get(AuditLogRow, row_id))
        with pytest.raises(AppendOnlyError):
            await s.commit()


# --- endpoints ----------------------------------------------------------------


async def test_start_and_get_call(client) -> None:
    r = await client.post("/calls", json=CALL)
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["state"] == "DIALING"

    r = await client.get(f"/calls/{call['id']}")
    assert r.status_code == 200
    assert r.json()["timeline"][0]["to_state"] == "DIALING"


async def test_unknown_call_is_404(client) -> None:
    assert (await client.get("/calls/nope")).status_code == 404
    assert (await client.post("/calls/nope/takeover")).status_code == 404


@pytest.mark.parametrize("action", ["takeover", "handback", "cancel"])
async def test_call_control_stubs(client, action: str) -> None:
    call_id = (await client.post("/calls", json=CALL)).json()["id"]
    r = await client.post(f"/calls/{call_id}/{action}")
    assert r.status_code == 202


async def test_answer_is_delivered_but_never_stored(client) -> None:
    call_id = (await client.post("/calls", json=CALL)).json()["id"]
    async with sessionmaker()() as s:
        req = UserRequestRow(
            session_id=call_id, question="Account PIN?", sensitivity="medium", status="pending"
        )
        s.add(req)
        await s.commit()
        req_id = req.id

    waiter = answers.expect(req_id)
    r = await client.post(f"/requests/{req_id}/answer", json={"answer": "zq4821"})
    assert r.status_code == 202 and r.json()["detail"] == "delivered"
    assert waiter.result() == "zq4821"

    # Second answer is rejected.
    assert (
        await client.post(f"/requests/{req_id}/answer", json={"answer": "x"})
    ).status_code == 409

    # The value appears nowhere in the database.
    async with sessionmaker()() as s:
        for table in ("call_session", "call_event", "user_request", "audit_log"):
            rows = (await s.execute(text(f"SELECT * FROM {table}"))).all()
            assert "zq4821" not in repr(rows), table
        req_row = await s.get(UserRequestRow, req_id)
        assert req_row.status == "answered"
        entries = (await s.scalars(select(AuditLogRow.action))).all()
        assert "user_request.answered" in entries


async def test_type_to_talk_text_not_logged(client) -> None:
    call_id = (await client.post("/calls", json=CALL)).json()["id"]
    r = await client.post(f"/calls/{call_id}/type", json={"text": "my secret zq99887"})
    assert r.status_code == 202
    async with sessionmaker()() as s:
        rows = (await s.execute(text("SELECT * FROM audit_log"))).all()
        assert "zq99887" not in repr(rows)


async def test_companies_and_health(client) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    r = await client.get("/companies", params={"q": "fake"})
    assert r.status_code == 200 and len(r.json()) == 1


async def test_twilio_status(client) -> None:
    r = await client.post("/twilio/status", data={"CallSid": "CA1", "CallStatus": "ringing"})
    assert r.status_code == 200
