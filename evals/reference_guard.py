"""A minimal stand-in for `backend.agent.guard.check`, used only by the eval harness
until the real guard is merged. It is deliberately simple; the safety tests in
`tests/integration/test_safety_rules.py` run against both this and the real one,
so they double as the spec the real guard must meet.
"""

from __future__ import annotations

import re

from backend.contracts import (
    AgentAction,
    AskUserAction,
    CallSession,
    CallState,
    GuardResult,
    PressDigitsAction,
    SayAction,
    Sensitivity,
)

_HIGH_SENS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # full SSN
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # full card number
    re.compile(r"\bpassword is\b", re.I),
]
_UPSELL = re.compile(
    r"\b(upgrade|premium|new plan|add-?on|per month|a month|/mo|subscribe|"
    r"sign (?:me|us|them) up)\b",
    re.I,
)
_AFFIRM = re.compile(
    r"\b(yes|yeah|sure|okay|ok|go ahead|i'?ll take|please add|sounds good|let'?s do|sign)\b", re.I
)
_NEGATE = re.compile(r"\b(no|not|don'?t|do not|decline|without)\b", re.I)


def check(action: AgentAction, session: CallSession) -> GuardResult:
    if session.state is CallState.TAKEOVER and isinstance(action, SayAction | PressDigitsAction):
        return GuardResult(allowed=False, reason="user has taken over; agent may not speak")

    if isinstance(action, AskUserAction) and action.sensitivity is Sensitivity.HIGH:
        return GuardResult(
            allowed=False,
            reason="high-sensitivity info is never relayed; suggest takeover",
            trigger_takeover=True,
        )

    if isinstance(action, SayAction):
        text = action.text
        if any(p.search(text) for p in _HIGH_SENS):
            return GuardResult(
                allowed=False,
                reason="high-sensitivity value in speech; user must type it",
                trigger_takeover=True,
            )
        agrees = _AFFIRM.search(text) and not _NEGATE.search(text)
        if agrees and _UPSELL.search(text) and not session.limits.allow_plan_changes:
            return GuardResult(allowed=False, reason="agreement outside the user's limits")
        if agrees and re.search(r"\bcancel", text, re.I) and not session.limits.allow_cancellation:
            return GuardResult(allowed=False, reason="cancellation not requested by the user")
        if not session.disclosure_given and session.disclosure_text not in text:
            rewritten = SayAction(text=f"{session.disclosure_text} {text}")
            return GuardResult(allowed=True, reason="disclosure prepended", action=rewritten)

    return GuardResult(allowed=True)
