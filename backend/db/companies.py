"""Small built-in company directory for `GET /companies`."""

from __future__ import annotations

from pydantic import BaseModel


class Company(BaseModel):
    name: str
    phone_number: str
    note: str = ""


# The default test company. With our own pipeline (TELEPHONY=phone/mock) the fake
# support line plays an Amazon-style menu, hold, and rep (fakeline/script.py) at a
# placeholder number. With Cartesia, the user enters the number that plays the rep.
FAKELINE = "Amazon Customer Service"
FAKELINE_PLACEHOLDER_NUMBER = "+15550100100"

# Add real companies only after verifying their current support number
# (build step 19: one real call, after the fake line works end to end).
DIRECTORY: list[Company] = [
    Company(name=FAKELINE, phone_number="", note="Test line (fake Amazon support)"),
]


def search(q: str, test_number: str) -> list[Company]:
    q = q.strip().lower()
    out = []
    for c in DIRECTORY:
        if c.name == FAKELINE:
            c = c.model_copy(update={"phone_number": test_number})
        if not q or q in c.name.lower() or q in c.note.lower():
            out.append(c)
    return out
