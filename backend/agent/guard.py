"""Guard layer: every outgoing agent action passes `check()` before it runs.

This is where CLAUDE.md section 2 is enforced in code, not in the prompt:

- Rule 1  disclosure: the first spoken line always starts with the disclosure.
- Rule 2  private info: anything redaction would scrub (registered ask_user
          answers, SSN/card patterns) is blocked, except the single one-shot
          relay of the answer the user just gave.
- Rule 3  no invented facts: long numbers / emails must come from the goal,
          the rep, or the user's answer; otherwise the agent must ask_user.
- Rule 4  high sensitivity: never relayed; the guard triggers takeover.
- Rule 5  limits: agreeing to plan changes, cancellations, or spend outside
          the goal and limits is blocked and the reason is returned to Claude.
- Rule 6  takeover: once the user has taken over, the agent cannot act.

`check()` is pure and synchronous (budget < 10 ms). `enforce()` wraps it and
writes every decision to the audit log, denials with allowed=false.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from backend.audit.log import redact
from backend.contracts import (
    AgentAction,
    AskUserAction,
    CallSession,
    EndCallAction,
    GuardResult,
    PressDigitsAction,
    RequestTakeoverAction,
    SayAction,
    Sensitivity,
)

from .ports import AgentRuntime, AuditSink

# --------------------------------------------------------------------------- #
# Disclosure (rule 1)
# --------------------------------------------------------------------------- #

_AI_TERMS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|automated assistant|virtual assistant)\b", re.I
)
_FALLBACK_AI_LINE = "I'm an AI assistant calling on the customer's behalf."


def effective_disclosure(session: CallSession) -> str:
    """The user's wording, guaranteed to say it is an AI."""
    text = session.disclosure_text.strip()
    if not _AI_TERMS.search(text):
        text = f"{text} {_FALLBACK_AI_LINE}".strip()
    return text


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _has_disclosure(text: str, session: CallSession) -> bool:
    return _norm(effective_disclosure(session)) in _norm(text)


# --------------------------------------------------------------------------- #
# Sensitivity (rule 4)
# --------------------------------------------------------------------------- #

_LAST_FOUR = r"(?:last\s+(?:4|four)|final\s+(?:4|four))"
_HIGH_QUESTION = [
    re.compile(r"\bpass\s?words?\b", re.I),
    re.compile(r"\b(?:cvv|cvc|security code on the card)\b", re.I),
    re.compile(rf"^(?!.*{_LAST_FOUR}).*\b(?:social security|ssn)\b", re.I | re.S),
    re.compile(rf"^(?!.*{_LAST_FOUR}).*\b(?:card number|credit card|debit card)\b", re.I | re.S),
]

_SSN = re.compile(r"(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

_RANK = {Sensitivity.LOW: 0, Sensitivity.MEDIUM: 1, Sensitivity.HIGH: 2}


def classify_question(question: str, claimed: Sensitivity) -> Sensitivity:
    """Never trust Claude to under-rate a question: take the max of claimed and detected."""
    detected = Sensitivity.HIGH if any(p.search(question) for p in _HIGH_QUESTION) else claimed
    return detected if _RANK[detected] > _RANK[claimed] else claimed


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def looks_high_sensitivity(value: str, question: str = "") -> bool:
    """Is this value (an answer, or text to speak) a full SSN / card number?"""
    for m in _CARD.finditer(value):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    if re.search(r"\d{3}-\d{2}-\d{4}", value):
        return True
    if _SSN.search(value) and re.search(r"social security|ssn", question, re.I):
        return True
    return False


# --------------------------------------------------------------------------- #
# Private data and invented facts (rules 2 and 3)
# --------------------------------------------------------------------------- #

_LONG_NUMBER = re.compile(r"(?<![\d$])\d(?:[ -]?\d){3,}(?![\d])")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _without(text: str, value: str | None) -> str:
    return text.replace(value, " ") if value else text


def contains_private(text: str, allowed_value: str | None = None) -> bool:
    """True if `text` holds anything redaction would scrub, apart from `allowed_value`."""
    probe = _without(text, allowed_value)
    return redact(probe) != probe


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _known_sources(session: CallSession, runtime: AgentRuntime | None) -> list[str]:
    sources = [session.goal, session.limits.notes, session.company, session.phone_number]
    if runtime is not None:
        sources.extend(runtime.remote_history)
        sources.extend(runtime.spoken)
    return sources


def invented_facts(text: str, session: CallSession, runtime: AgentRuntime | None) -> list[str]:
    """Identifiers the agent is about to say that nobody gave it."""
    allowed_value = runtime.one_shot_relay if runtime else None
    probe = _without(text, allowed_value)
    sources = _known_sources(session, runtime)
    source_digits = [_digits(s) for s in sources]
    source_text = " ".join(s.lower() for s in sources)
    found: list[str] = []
    for m in _LONG_NUMBER.finditer(probe):
        d = _digits(m.group())
        if not any(d in s for s in source_digits if s):
            found.append(m.group())
    for m in _EMAIL.finditer(probe):
        if m.group().lower() not in source_text:
            found.append(m.group())
    return found


# --------------------------------------------------------------------------- #
# Limits (rule 5)
# --------------------------------------------------------------------------- #

_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|sure|okay|ok|alright|sounds good|that works|go ahead|please do|"
    r"let'?s do (?:it|that)|i agree|i accept|we accept|i'?ll take|we'?ll take|sign (?:me|us) up|"
    r"i'?d like (?:that|to)|i want (?:that|to)|that'?s fine|do it|deal|absolutely|definitely|"
    r"add (?:it|that)|switch (?:me|us)|please (?:add|switch|upgrade|cancel|change|enroll))\b",
    re.I,
)
_DECLINE_WORD = re.compile(
    r"\b(no|not|don'?t|do not|decline|won'?t|without|never|rather not)\b", re.I
)
_DECLINE_IDIOMS = re.compile(r"\b(no problem|not a problem|no worries|not at all)\b", re.I)

_TOPICS: dict[str, re.Pattern[str]] = {
    "plan change": re.compile(
        r"\b(upgrade|upgrading|downgrade|new plan|plan changes?|change (?:my|the|your) plan|"
        r"switch(?:ing)? plans?|premium|unlimited plan|bundle|add[- ]on|protection plan|"
        r"subscription|subscribe|free trial|membership|enroll(?:ment)?)\b",
        re.I,
    ),
    "cancellation": re.compile(
        r"\b(cancel|cancell?ation|terminate|close (?:my|the|your) account|disconnect)\b", re.I
    ),
}
_STRONG_PAY = re.compile(
    r"\b(charge|charged|cost|costs|pay|payment|per month|a month|monthly|bill you|"
    r"billed|purchase|buy)\b",
    re.I,
)
_WEAK_PAY = re.compile(r"\b(fee|fees)\b", re.I)
_WAIVE = re.compile(
    r"\b(waive|waived|refund|refunded|credit|credited|remove|removed|reverse|"
    r"reversed|drop|dropped|take (?:it|that) off)\b",
    re.I,
)
_AMOUNT = re.compile(r"\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)|(\d+(?:\.\d+)?)\s?dollars?", re.I)


def _goal_permits(topic: str, session: CallSession) -> bool:
    limits = session.limits
    if topic == "plan change" and limits.allow_plan_changes:
        return True
    if topic == "cancellation" and limits.allow_cancellation:
        return True
    pattern = _TOPICS[topic]
    for m in pattern.finditer(session.goal):
        if not _declined_before(session.goal, m.start()):
            return True
    return False


def _declined_before(text: str, pos: int, window_words: int = 5) -> bool:
    before = _DECLINE_IDIOMS.sub(" ", text[:pos])
    words = before.split()[-window_words:]
    return bool(_DECLINE_WORD.search(" ".join(words)))


def _topics_in(text: str) -> set[str]:
    return {t for t, p in _TOPICS.items() if p.search(text)}


def _declined_topics(text: str) -> set[str]:
    """Topics the agent explicitly declines in its own words ("don't want the upgrade")."""
    out: set[str] = set()
    for topic, pattern in _TOPICS.items():
        matches = list(pattern.finditer(text))
        if matches and all(_declined_before(text, m.start()) for m in matches):
            out.add(topic)
    return out


def _amounts(text: str) -> list[float]:
    vals: list[float] = []
    for m in _AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2) or "0").replace(",", "")
        vals.append(float(raw))
    return vals


def _is_agreement(text: str) -> bool:
    cleaned = _DECLINE_IDIOMS.sub(" ", text)
    return bool(_AFFIRM.search(cleaned))


def limits_violation(text: str, session: CallSession, runtime: AgentRuntime | None) -> str | None:
    """Return a reason if `text` agrees to (or asks for) something outside goal/limits."""
    context = runtime.last_remote_text if runtime else ""
    agreeing = _is_agreement(text)
    declined = _declined_topics(text)

    own_topics = _topics_in(text) - declined
    rep_topics = (_topics_in(context) - declined) if agreeing else set()
    for topic in sorted(own_topics | rep_topics):
        # Agent mentioning a topic without agreeing ("I'm calling about the fee") is fine;
        # agreeing to it, or asking for it, is not.
        if topic in own_topics and not agreeing and not _asks_for(text):
            continue
        if not _goal_permits(topic, session):
            return f"agreeing to a {topic} is outside the user's goal and limits"

    # Spend: any agreement in a payment context above the spend limit.
    if agreeing:
        combined = f"{context} {text}"
        strong = _STRONG_PAY.search(combined)
        weak = _WEAK_PAY.search(combined)
        waiving = _WAIVE.search(combined) and not strong
        if (strong or weak) and not waiving:
            amounts = _amounts(combined)
            top = max(amounts) if amounts else None
            if top is None:
                if session.limits.max_spend_usd <= 0 and strong:
                    return "agreeing to a charge is outside the user's limits (no spend allowed)"
            elif top > session.limits.max_spend_usd:
                return (
                    f"agreeing to pay ${top:g} exceeds the user's spend limit "
                    f"of ${session.limits.max_spend_usd:g}"
                )
    return None


_REQUEST = re.compile(r"\b(i'?d like|i want|i would like|please|can you|could you|let'?s)\b", re.I)


def _asks_for(text: str) -> bool:
    return bool(_REQUEST.search(text))


# --------------------------------------------------------------------------- #
# check / enforce
# --------------------------------------------------------------------------- #


def _deny(reason: str, *, takeover: bool = False) -> GuardResult:
    return GuardResult(allowed=False, reason=reason, trigger_takeover=takeover)


def check(
    action: AgentAction, session: CallSession, runtime: AgentRuntime | None = None
) -> GuardResult:
    """Decide whether `action` may run. May return a rewritten action."""
    if runtime is not None and runtime.taken_over and not isinstance(action, RequestTakeoverAction):
        return _deny("the user has taken over the call; the agent may not act")
    if runtime is not None and runtime.ended:
        return _deny("the call has ended")

    if isinstance(action, SayAction):
        return _check_say(action, session, runtime)
    if isinstance(action, AskUserAction):
        return _check_ask_user(action, session)
    if isinstance(action, PressDigitsAction):
        return _check_digits(action, session, runtime)
    if isinstance(action, EndCallAction):
        return _check_end_call(action, session, runtime)
    if isinstance(action, RequestTakeoverAction):
        return GuardResult(allowed=True, action=action)
    return _deny("unknown action")


def _check_say(
    action: SayAction, session: CallSession, runtime: AgentRuntime | None
) -> GuardResult:
    text = action.text.strip()
    if not text:
        return _deny("nothing to say")
    relay = runtime.one_shot_relay if runtime else None

    if looks_high_sensitivity(text):
        return _deny(
            "high-sensitivity data (full SSN or card number) is never spoken by the agent; "
            "the user should type it directly",
            takeover=True,
        )
    if contains_private(text, allowed_value=relay):
        return _deny(
            "text contains the user's private information, which may be spoken only once "
            "right after they provide it; call ask_user again if the rep needs it"
        )
    invented = invented_facts(text, session, runtime)
    if invented:
        return _deny(
            "text states details nobody provided (e.g. numbers or emails); "
            "never invent user facts — call ask_user instead"
        )
    reason = limits_violation(text, session, runtime)
    if reason:
        return _deny(reason + "; politely decline and stay on the user's goal")

    if not session.disclosure_given and not _has_disclosure(text, session):
        text = f"{effective_disclosure(session)} {text}"
        return GuardResult(allowed=True, reason="disclosure prepended", action=SayAction(text=text))
    return GuardResult(allowed=True, action=SayAction(text=text))


def _check_ask_user(action: AskUserAction, session: CallSession) -> GuardResult:
    if contains_private(action.question):
        return _deny("the question itself contains private data; ask without including it")
    sensitivity = classify_question(action.question, action.sensitivity)
    rewritten = AskUserAction(question=action.question.strip(), sensitivity=sensitivity)
    if sensitivity == Sensitivity.HIGH:
        return GuardResult(
            allowed=False,
            reason="high-sensitivity information is never relayed by the agent; "
            "suggesting the user take over and type it directly",
            action=rewritten,
            trigger_takeover=True,
        )
    reason = "sensitivity raised to high" if sensitivity != action.sensitivity else ""
    return GuardResult(allowed=True, reason=reason, action=rewritten)


def _check_digits(
    action: PressDigitsAction, session: CallSession, runtime: AgentRuntime | None
) -> GuardResult:
    digits = action.digits
    relay = runtime.one_shot_relay if runtime else None
    plain = _digits(digits)
    if relay is not None and plain and plain == _digits(relay):
        if looks_high_sensitivity(relay):
            return _deny("high-sensitivity data is never entered by the agent", takeover=True)
        return GuardResult(allowed=True, action=action)
    if contains_private(digits) or (plain and contains_private(" ".join(plain))):
        return _deny("digits match the user's private information, which may be entered only once")
    if len(plain) >= 4:
        sources = [_digits(s) for s in _known_sources(session, runtime)]
        if not any(plain in s for s in sources if s):
            return _deny("long digit strings must come from the user; call ask_user to get them")
    return GuardResult(allowed=True, action=action)


def _check_end_call(
    action: EndCallAction, session: CallSession, runtime: AgentRuntime | None
) -> GuardResult:
    ref = (action.reference_number or "").strip()
    if ref and runtime is not None:
        said = _norm(" ".join(runtime.spoken)).replace(" ", "")
        if _norm(ref).replace(" ", "") not in said:
            return _deny(
                "read back the key details (including the reference number) to the rep "
                "with say() before ending the call"
            )
    # Summaries are stored and shown to the user: scrub anything private.
    return GuardResult(
        allowed=True,
        action=EndCallAction(
            summary=redact(action.summary),
            reference_number=redact(ref) if ref else None,
        ),
    )


async def enforce(action: AgentAction, runtime: AgentRuntime, audit: AuditSink) -> GuardResult:
    """`check()` plus an audit entry for every decision (denials with allowed=false)."""
    result = check(action, runtime.session, runtime)
    evidence: dict[str, object] = {
        "kind": action.kind,
        "trigger_takeover": result.trigger_takeover,
        "state": runtime.session.state.value,
    }
    if not result.allowed:
        evidence["attempt"] = _describe(action)
    await audit(
        actor="guard",
        action=f"guard.{action.kind}",
        subject=runtime.session.id,
        allowed=result.allowed,
        reason=result.reason or ("allowed" if result.allowed else "denied"),
        evidence=evidence,
    )
    return result


def _describe(action: AgentAction) -> str:
    """A redacted, human-readable record of what was attempted (for the audit trail)."""
    parts: Iterable[str]
    if isinstance(action, SayAction):
        parts = [action.text]
    elif isinstance(action, AskUserAction):
        parts = [action.question]
    elif isinstance(action, PressDigitsAction):
        parts = ["<digits>"]  # digits may be private; record only that they were attempted
    elif isinstance(action, EndCallAction):
        parts = [action.summary]
    else:
        parts = [action.reason]
    return redact(" ".join(parts))[:500]
