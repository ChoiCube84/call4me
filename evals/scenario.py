"""Data model for eval scenarios: the fake line, the bot rep's script, the user, and
what counts as success.

A scenario is plain data. `evals.sim.SimulatedCall` plays it against a driver
(the system under test) at transcript level, in virtual time.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from backend.contracts import CallLimits

# --------------------------------------------------------------------------- #
# The fake support line (mirrors CLAUDE.md section 13: menu, nested billing
# menu, recorded messages, hold music, callback offer, then a rep).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Recorded:
    """A recorded announcement. Digits and speech are ignored."""

    text: str


@dataclass(frozen=True)
class Menu:
    """A phone menu. `routes` maps a digit to a branch name in `FakeLine.branches`.

    Routes may include digits that are never announced (e.g. a hidden "0").
    Saying "representative"/"agent"/"operator" counts as pressing 0 if 0 is routed.
    """

    prompt: str
    routes: dict[str, str]
    max_repeats: int = 6


@dataclass(frozen=True)
class Hold:
    """Hold music for `seconds`. Optionally offers a callback partway through."""

    seconds: float
    callback_offer_at: float | None = None
    callback_delay_s: float = 600.0  # how long until the company calls back


class Expect(enum.StrEnum):
    """What the bot rep waits to hear before moving to its next step."""

    ANY = "any"  # any reply
    CONFIRM = "confirm"  # a clear yes
    DECLINE = "decline"  # a clear no (accepting instead is a violation)
    PROVIDE = "provide"  # a user fact, e.g. the PIN (see `provide_key`)
    TAKEOVER = "takeover"  # only the user themselves (typed text) will do


@dataclass(frozen=True)
class RepStep:
    say: str
    expect: Expect = Expect.ANY
    provide_key: str | None = None  # key in `UserScript.facts` for Expect.PROVIDE
    reprompt: str | None = None  # what the rep says when the reply misses (default: `say`)
    flags: tuple[str, ...] = ()  # outcome flags set when the rep says this line
    new_rep: str | None = None  # this line is a new human picking up (disclosure required)
    transfer_hold_s: float | None = None  # after this line, hold, then the next step
    hangup: bool = False  # rep hangs up after this line


@dataclass(frozen=True)
class RepScript:
    steps: tuple[RepStep, ...]


Segment = Recorded | Menu | Hold | RepScript


@dataclass(frozen=True)
class FakeLine:
    branches: dict[str, tuple[Segment, ...]]
    start: str = "main"


# --------------------------------------------------------------------------- #
# The user (answers ask_user, may take over and type)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UserScript:
    # ask_user answers, keyed by a word that appears in the question ("pin", "order").
    # These are the private values the tests make sure are never stored.
    facts: dict[str, str] = field(default_factory=dict)
    answer_delay_s: float = 20.0  # time to notice the alert and type the answer
    accept_takeover: bool = True  # accept when the agent requests a takeover
    takeover_at_step: int | None = None  # user taps takeover before rep step N
    takeover_lines: tuple[str, ...] = ()  # typed by the user, one per rep turn
    typing_s_per_line: float = 15.0
    handback: bool = True  # hand back to the agent after the typed lines


# --------------------------------------------------------------------------- #
# Success criteria
# --------------------------------------------------------------------------- #

DEFAULT_FORBIDDEN_FLAGS = ("accepted_upsell", "rep_gave_up")


@dataclass(frozen=True)
class Criteria:
    required_flags: tuple[str, ...] = ()
    forbidden_flags: tuple[str, ...] = DEFAULT_FORBIDDEN_FLAGS
    expect_reference: str | None = None  # end_call must carry this reference number
    asks: tuple[int, int] | None = None  # allowed range for # of ask_user calls
    expect_takeover: bool = False
    summary_must_contain: tuple[str, ...] = ()
    spoken_once: tuple[str, ...] = ()  # fact keys the agent must relay exactly once
    never_relay: tuple[str, ...] = ()  # fact keys the agent must never speak


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    goal: str
    line: FakeLine
    criteria: Criteria
    limits: CallLimits = field(default_factory=CallLimits)
    user: UserScript = field(default_factory=UserScript)
    company: str = "Fake Support Line (test)"
    company_number: str = "+15550100000"
    disclosure: str = "Hi, I'm an AI assistant calling on behalf of Sam Lee."
    user_phone: str = "+15105550123"  # must never be given out (callbacks use the agent's)
    agent_number: str = "+14155550199"
