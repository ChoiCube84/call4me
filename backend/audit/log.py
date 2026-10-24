"""Append-only audit log and redaction.

Every payload headed for logs, the database, or telemetry goes through
`redact()` first. `ask_user` answers are registered with `register_secret()`
the moment they arrive, so later transcripts that echo them (our TTS, the rep
repeating a PIN back) are scrubbed too. Registered secrets live only in
process memory and are dropped with `forget_session()` when the call ends.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.contracts import CallEvent
from backend.db.models import AuditLogRow, CallEventRow

REDACTED = "[REDACTED]"

# Keys whose values are always private, wherever they appear.
SENSITIVE_KEYS = frozenset(
    {
        "answer",
        "pin",
        "passcode",
        "password",
        "ssn",
        "social_security_number",
        "dob",
        "date_of_birth",
        "account_number",
        "card_number",
        "cvv",
        "security_answer",
    }
)

_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card numbers
]

_secrets: dict[str, set[str]] = defaultdict(set)


def register_secret(session_id: str, value: str) -> None:
    value = value.strip()
    if value:
        _secrets[session_id].add(value)


def forget_session(session_id: str) -> None:
    _secrets.pop(session_id, None)


def _all_secrets() -> list[str]:
    # Longest first so a secret containing another is fully replaced.
    return sorted({s for vals in _secrets.values() for s in vals}, key=len, reverse=True)


def _redact_str(text: str) -> str:
    for secret in _all_secrets():
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", REDACTED, text)
        # Spoken digits often come back spaced out ("1 2 3 4"), and dashed numbers
        # (order numbers like 112-4567890-1234567) lose or move their dashes.
        digits = re.sub(r"\D", "", secret)
        if len(digits) > 1 and re.fullmatch(r"[\d\s-]+", secret):
            spaced = r"[\s-]*".join(digits)
            text = re.sub(rf"(?<!\d){spaced}(?!\d)", REDACTED, text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(payload: Any) -> Any:
    """Return a redacted deep copy of `payload` (str, dict, list, or scalar)."""
    if isinstance(payload, str):
        return _redact_str(payload)
    if isinstance(payload, dict):
        return {
            k: REDACTED if str(k).lower() in SENSITIVE_KEYS else redact(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list | tuple):
        return [redact(v) for v in payload]
    return payload


async def append(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    subject: str | None = None,
    allowed: bool = True,
    reason: str = "",
    diff: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> AuditLogRow:
    """Append one audit entry. Fixes are new entries, never edits."""
    row = AuditLogRow(
        actor=actor,
        action=action,
        subject=subject,
        allowed=allowed,
        reason=redact(reason),
        diff_json=redact(diff or {}),
        evidence_json=redact(evidence or {}),
    )
    db.add(row)
    await db.flush()
    return row


async def record_event(db: AsyncSession, event: CallEvent) -> CallEventRow:
    """Store a timeline event (redacted)."""
    row = CallEventRow(
        id=event.id,
        session_id=event.session_id,
        at=event.at,
        kind=event.kind,
        from_state=event.from_state,
        to_state=event.to_state,
        confidence=event.confidence,
        detail_json=redact(event.detail),
    )
    db.add(row)
    await db.flush()
    return row
