"""Small built-in company directory for `GET /companies`."""

from __future__ import annotations

from pydantic import BaseModel


class Company(BaseModel):
    name: str
    phone_number: str
    note: str = ""


# The fake line number is filled from settings at request time.
FAKELINE = "Fake Support Line (test)"

# Add real companies only after verifying their current support number
# (build step 19: one real call, after the fake line works end to end).
DIRECTORY: list[Company] = [
    Company(name=FAKELINE, phone_number="", note="Our test line; use this first"),
]


def search(q: str, fakeline_number: str) -> list[Company]:
    q = q.strip().lower()
    out = []
    for c in DIRECTORY:
        if c.name == FAKELINE:
            c = c.model_copy(update={"phone_number": fakeline_number})
        if not q or q in c.name.lower():
            out.append(c)
    return out
