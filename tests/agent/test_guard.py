"""Every CLAUDE.md section 2 rule the guard enforces, one test (or more) each."""

from __future__ import annotations

from backend.agent import guard
from backend.agent.ports import AgentRuntime, MemoryAuditSink
from backend.audit import log as audit
from backend.contracts import (
    AskUserAction,
    CallLimits,
    EndCallAction,
    PressDigitsAction,
    RequestTakeoverAction,
    SayAction,
    Sensitivity,
)

from .conftest import make_session


def say(text: str, rt: AgentRuntime, rep: str | None = None):
    if rep is not None:
        rt.remote_history.append(rep)
    return guard.check(SayAction(text=text), rt.session, rt)


# --- rule 1: disclosure ------------------------------------------------------


def test_disclosure_prepended_on_first_say(runtime) -> None:
    r = say("I'm calling about a late fee.", runtime)
    assert r.allowed
    assert r.action.text.startswith("Hi, I'm an AI assistant calling on behalf of Sam.")
    assert r.action.text.endswith("I'm calling about a late fee.")


def test_disclosure_not_repeated_once_given(runtime) -> None:
    runtime.session.disclosure_given = True
    r = say("I'm calling about a late fee.", runtime)
    assert r.action.text == "I'm calling about a late fee."


def test_disclosure_not_doubled_if_already_in_text(runtime) -> None:
    text = "Hi, I'm an AI assistant calling on behalf of Sam. Calling about a fee."
    assert say(text, runtime).action.text == text


def test_disclosure_without_ai_wording_gets_ai_line() -> None:
    s = make_session(disclosure_text="Hello, I'm calling for Sam.")
    assert "AI assistant" in guard.effective_disclosure(s)
    r = guard.check(SayAction(text="About my bill."), s, AgentRuntime(session=s))
    assert "AI assistant" in r.action.text


# --- rule 2: private info spoken once -------------------------------------------


def test_pin_relay_blocked_unless_one_shot(runtime) -> None:
    runtime.session.disclosure_given = True
    audit.register_secret(runtime.session.id, "4821")
    assert not say("Sure, the PIN is 4821.", runtime).allowed
    runtime.one_shot_relay = "4821"
    assert say("Sure, the PIN is 4821.", runtime).allowed
    runtime.one_shot_relay = None
    assert not say("Again, it's 4 8 2 1.", runtime).allowed  # spoken-digit form too


def test_digits_matching_secret_blocked(runtime) -> None:
    audit.register_secret(runtime.session.id, "4821")
    r = guard.check(PressDigitsAction(digits="4821"), runtime.session, runtime)
    assert not r.allowed
    runtime.one_shot_relay = "4821"
    assert guard.check(PressDigitsAction(digits="4821"), runtime.session, runtime).allowed


def test_end_call_summary_is_redacted(runtime) -> None:
    audit.register_secret(runtime.session.id, "4821")
    r = guard.check(EndCallAction(summary="Verified with PIN 4821."), runtime.session, runtime)
    assert r.allowed and "4821" not in r.action.summary


# --- rule 3: never invent user facts --------------------------------------------


def test_invented_account_number_blocked(runtime) -> None:
    runtime.session.disclosure_given = True
    r = say("My account number is 5551 2398.", runtime)
    assert not r.allowed and "ask_user" in r.reason


def test_number_from_rep_or_goal_allowed(runtime) -> None:
    runtime.session.disclosure_given = True
    assert say("Ticket 77881, got it.", runtime, rep="Your ticket is 77881.").allowed
    s = make_session(goal="Ask about order 99812 shipping")
    s.disclosure_given = True
    assert guard.check(SayAction(text="It's order 99812."), s, AgentRuntime(session=s)).allowed


def test_invented_email_blocked(runtime) -> None:
    runtime.session.disclosure_given = True
    assert not say("The email is sam@example.com.", runtime).allowed


def test_invented_long_digits_blocked_but_menu_digit_ok(runtime) -> None:
    assert not guard.check(PressDigitsAction(digits="482193"), runtime.session, runtime).allowed
    assert guard.check(PressDigitsAction(digits="2"), runtime.session, runtime).allowed


# --- rule 4: high sensitivity -> takeover ----------------------------------------


def test_high_sensitivity_question_triggers_takeover(runtime) -> None:
    r = guard.check(
        AskUserAction(question="What is your password?", sensitivity=Sensitivity.HIGH),
        runtime.session,
        runtime,
    )
    assert not r.allowed and r.trigger_takeover


def test_underrated_ssn_question_is_raised_to_high(runtime) -> None:
    r = guard.check(
        AskUserAction(question="What's your social security number?", sensitivity="low"),
        runtime.session,
        runtime,
    )
    assert not r.allowed and r.trigger_takeover
    assert r.action.sensitivity == Sensitivity.HIGH


def test_last_four_of_ssn_is_not_high(runtime) -> None:
    r = guard.check(
        AskUserAction(question="What are the last four of your SSN?", sensitivity="medium"),
        runtime.session,
        runtime,
    )
    assert r.allowed and r.action.sensitivity == Sensitivity.MEDIUM


def test_full_card_or_ssn_in_speech_blocked_with_takeover(runtime) -> None:
    runtime.session.disclosure_given = True
    for text in ("The card is 4111 1111 1111 1111.", "It's 123-45-6789."):
        runtime.one_shot_relay = text.split(" is " if "card" in text else "It's ")[-1]
        r = say(text, runtime)
        assert not r.allowed and r.trigger_takeover, text


# --- rule 5: limits ---------------------------------------------------------------


def test_paid_upsell_agreement_blocked(runtime) -> None:
    runtime.session.disclosure_given = True
    r = say(
        "Yes, sign me up.",
        runtime,
        rep="Would you like to upgrade to premium for $10 a month?",
    )
    assert not r.allowed and "plan change" in r.reason


def test_declining_upsell_allowed(runtime) -> None:
    runtime.session.disclosure_given = True
    r = say(
        "No thank you, I don't want the upgrade.",
        runtime,
        rep="Would you like to upgrade to premium for $10 a month?",
    )
    assert r.allowed


def test_upsell_allowed_when_limits_permit() -> None:
    s = make_session(limits=CallLimits(allow_plan_changes=True, max_spend_usd=20))
    s.disclosure_given = True
    rt = AgentRuntime(session=s)
    rt.remote_history.append("Would you like to upgrade to premium for $10 a month?")
    assert guard.check(SayAction(text="Yes, that works."), s, rt).allowed


def test_agent_asking_for_unrequested_cancellation_blocked(runtime) -> None:
    runtime.session.disclosure_given = True
    assert not say("I'd like to cancel my account.", runtime).allowed


def test_cancellation_allowed_when_it_is_the_goal() -> None:
    s = make_session(goal="Cancel my gym membership")
    s.disclosure_given = True
    rt = AgentRuntime(session=s)
    assert guard.check(SayAction(text="I'd like to cancel my membership."), s, rt).allowed


def test_agreeing_to_waived_fee_allowed(runtime) -> None:
    runtime.session.disclosure_given = True
    assert say("Yes, thank you!", runtime, rep="I've waived the $40 late fee, okay?").allowed


def test_agreeing_to_new_charge_blocked(runtime) -> None:
    runtime.session.disclosure_given = True
    r = say("Yes, that's fine.", runtime, rep="There's a $10 charge to process that, okay?")
    assert not r.allowed and "spend limit" in r.reason


def test_no_problem_is_not_a_decline(runtime) -> None:
    runtime.session.disclosure_given = True
    assert not say("Sure, no problem.", runtime, rep="Shall I add the protection plan?").allowed


# --- rule 6: takeover stops the agent -----------------------------------------------


def test_takeover_blocks_every_agent_action(runtime) -> None:
    runtime.takeover.set()
    for action in (
        SayAction(text="Hello"),
        AskUserAction(question="PIN?"),
        PressDigitsAction(digits="1"),
        EndCallAction(summary="done"),
    ):
        assert not guard.check(action, runtime.session, runtime).allowed
    assert guard.check(RequestTakeoverAction(reason="x"), runtime.session, runtime).allowed


# --- read-back before end_call ---------------------------------------------------------


def test_end_call_requires_reference_read_back(runtime) -> None:
    end = EndCallAction(summary="Fee waived.", reference_number="A7X291")
    assert not guard.check(end, runtime.session, runtime).allowed
    runtime.spoken.append("Just to confirm, the reference is A7X291?")
    assert guard.check(end, runtime.session, runtime).allowed


# --- rule 9: denials are audited -----------------------------------------------------


async def test_enforce_logs_denial_with_allowed_false(runtime) -> None:
    sink = MemoryAuditSink()
    runtime.session.disclosure_given = True
    runtime.remote_history.append("Want to upgrade to premium?")
    r = await guard.enforce(SayAction(text="Yes please, upgrade me."), runtime, sink)
    assert not r.allowed
    (entry,) = sink.entries
    assert entry["allowed"] is False and entry["action"] == "guard.say"
    assert "upgrade" in entry["evidence"]["attempt"]


async def test_enforce_audit_never_contains_secret(runtime) -> None:
    sink = MemoryAuditSink()
    runtime.session.disclosure_given = True
    audit.register_secret(runtime.session.id, "4821")
    await guard.enforce(SayAction(text="The PIN is 4821"), runtime, sink)
    assert "4821" not in str(sink.entries)


def test_guard_is_fast(runtime) -> None:
    import time

    runtime.session.disclosure_given = True
    runtime.remote_history.append("Would you like to upgrade to premium for $10 a month?")
    start = time.perf_counter()
    for _ in range(100):
        say("No thanks, I'd just like the late fee removed please.", runtime)
    assert (time.perf_counter() - start) / 100 < 0.01
