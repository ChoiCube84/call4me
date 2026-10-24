"""Every transition in the state table, legal and illegal."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

from backend.contracts import TERMINAL_STATES, CallState
from backend.db.models import AuditLogRow, CallEventRow, CallSessionRow
from backend.db.session import sessionmaker
from backend.engine import states as st
from tests.engine.fakes import make_session

S = CallState
ALL_PAIRS = [(a, b) for a, b in itertools.product(CallState, CallState) if a != b]
LEGAL = [(a, b) for a, b in ALL_PAIRS if st.can_transition(a, b)]
ILLEGAL = [(a, b) for a, b in ALL_PAIRS if not st.can_transition(a, b)]


@pytest.mark.parametrize(("src", "dst"), LEGAL, ids=lambda s: s.value)
async def test_legal_transition_changes_state_and_is_recorded(src, dst) -> None:
    session = make_session(state=src)
    rec = st.MemoryRecorder()
    assert await st.transition(session, dst, reason="test", confidence=0.9, recorder=rec)
    assert session.state is dst
    [r] = rec.transitions
    assert (r.from_state, r.to_state, r.reason, r.confidence, r.allowed) == (
        src,
        dst,
        "test",
        0.9,
        True,
    )


@pytest.mark.parametrize(("src", "dst"), ILLEGAL, ids=lambda s: s.value)
async def test_illegal_transition_raises_and_is_audited_as_denied(src, dst) -> None:
    session = make_session(state=src)
    rec = st.MemoryRecorder()
    with pytest.raises(st.IllegalTransition):
        await st.transition(session, dst, reason="bad", recorder=rec)
    assert session.state is src
    [r] = rec.transitions
    assert r.allowed is False and r.to_state is dst


def test_section_6_diagram_edges_are_legal() -> None:
    edges = [
        (S.DIALING, S.MENU),
        (S.MENU, S.HOLD),
        (S.HOLD, S.RECORDED_MESSAGE),
        (S.RECORDED_MESSAGE, S.HOLD),
        (S.HOLD, S.CALLBACK_OFFERED),
        (S.HOLD, S.HUMAN_SUSPECTED),
        (S.HUMAN_SUSPECTED, S.HUMAN_CONFIRMED),
        (S.HUMAN_CONFIRMED, S.IN_CONVERSATION),
        (S.IN_CONVERSATION, S.WAITING_ON_USER),
        (S.WAITING_ON_USER, S.IN_CONVERSATION),
        (S.IN_CONVERSATION, S.TAKEOVER),
        (S.IN_CONVERSATION, S.ENDED),
        (S.MENU, S.VOICEMAIL),
        (S.MENU, S.FAILED),
        (S.MENU, S.ENDED),
        (S.IN_CONVERSATION, S.HOLD),  # transfer
    ]
    for a, b in edges:
        assert st.can_transition(a, b), (a, b)


def test_shortcuts_that_skip_confirmation_are_illegal() -> None:
    assert not st.can_transition(S.HOLD, S.IN_CONVERSATION)
    assert not st.can_transition(S.MENU, S.HUMAN_CONFIRMED)
    assert not st.can_transition(S.DIALING, S.IN_CONVERSATION)
    assert not st.can_transition(S.HUMAN_SUSPECTED, S.IN_CONVERSATION)


def test_user_can_take_over_from_every_live_state() -> None:
    for state in set(CallState) - TERMINAL_STATES - {S.TAKEOVER}:
        assert st.can_transition(state, S.TAKEOVER), state


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert st.VALID_TRANSITIONS[state] == frozenset()


async def test_same_state_is_a_noop() -> None:
    session = make_session(state=S.HOLD)
    rec = st.MemoryRecorder()
    assert not await st.transition(session, S.HOLD, reason="again", recorder=rec)
    assert rec.transitions == []


async def test_recorder_failure_does_not_block_the_call() -> None:
    class Broken(st.MemoryRecorder):
        async def transition(self, record):
            raise RuntimeError("db down")

    session = make_session()
    assert await st.transition(session, S.MENU, reason="x", recorder=Broken())
    assert session.state is S.MENU


async def test_db_recorder_writes_event_audit_and_session_state(db) -> None:
    session = make_session()
    async with sessionmaker()() as s:
        s.add(
            CallSessionRow(
                id=session.id,
                user_id=session.user_id,
                company=session.company,
                phone_number=session.phone_number,
                goal=session.goal,
                limits_json={},
                disclosure_text=session.disclosure_text,
                state=session.state,
            )
        )
        await s.commit()
    rec = st.DbRecorder()
    await st.transition(session, S.MENU, reason="jev: menu", confidence=0.91, recorder=rec)
    with pytest.raises(st.IllegalTransition):
        await st.transition(session, S.IN_CONVERSATION, reason="skip", recorder=rec)

    async with sessionmaker()() as s:
        events = (await s.scalars(select(CallEventRow))).all()
        audits = (await s.scalars(select(AuditLogRow).order_by(AuditLogRow.at))).all()
        row = await s.get(CallSessionRow, session.id)
    assert [(e.from_state, e.to_state, e.confidence) for e in events] == [("DIALING", "MENU", 0.91)]
    assert events[0].detail_json["reason"] == "jev: menu"
    assert [(a.action, a.allowed) for a in audits] == [
        ("state.transition", True),
        ("state.transition", False),
    ]
    assert audits[0].diff_json == {"from": "DIALING", "to": "MENU"}
    assert audits[0].evidence_json["confidence"] == 0.91
    assert row.state == "MENU"
