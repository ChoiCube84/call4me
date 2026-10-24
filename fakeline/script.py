"""The fake support line's script: one source of truth for the TwiML app
(`fakeline/app.py`) and the in-process mock line (`backend/telephony/mock_client.py`).

Call flow::

    welcome -> main menu
      1 orders   -> recorded message -> hold queue
      2 billing  -> billing menu
                      1 balance -> recorded message -> billing menu
                      2 fees    -> hold queue
                      0 agent   -> hold queue
                      9 back    -> main menu
      0 agent    -> hold queue
    hold queue: music for a random 30-120 s with periodic announcements, sometimes a
    callback offer (press 1), then a hand-off to a rep (a forwarded number, an
    external bot-rep URL, or the built-in scripted rep below).

Each segment carries the `StateCheckLabel` a perfect classifier would give it, so
tests and evals can score call-engine decisions against ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.contracts import StateCheckLabel

COMPANY = "Acme Home Services"
MAX_MENU_TRIES = 5  # silent/invalid menu attempts before the line hangs up
MENU_TIMEOUT_S = 6


@dataclass(frozen=True)
class Menu:
    prompt: str
    options: dict[str, str]  # digit -> next node name


@dataclass(frozen=True)
class Message:
    text: str
    next: str


@dataclass(frozen=True)
class RepLine:
    text: str
    # Agent turns the rep waits for before its next line. 2 for questions where the
    # agent first says a holding line ("One moment...") and then relays the answer.
    expects_turns: int = 1
    hangup_after: bool = False


WELCOME = (
    f"Thank you for calling {COMPANY}. Your call may be monitored for quality. "
    "Please listen carefully, as our menu options have changed."
)

NODES: dict[str, Menu | Message] = {
    "main": Menu(
        prompt=(
            "For orders, press 1. For billing and payments, press 2. "
            "To speak with an agent, press 0."
        ),
        options={"1": "orders", "2": "billing", "0": "queue"},
    ),
    "billing": Menu(
        prompt=(
            "Billing. To hear your balance, press 1. For questions about fees or charges, "
            "press 2. To speak with an agent, press 0. To return to the main menu, press 9."
        ),
        options={"1": "balance", "2": "queue", "0": "queue", "9": "main"},
    ),
    "orders": Message(
        text=(
            "You can track any order online at acme dot example slash orders. "
            "Please hold for the next available orders specialist."
        ),
        next="queue",
    ),
    "balance": Message(
        text=(
            "For your security, balances are only available online or from an agent. "
            "Returning to the billing menu."
        ),
        next="billing",
    ),
}

INVALID_OPTION = "Sorry, that is not a valid option."
NO_INPUT_GOODBYE = "We did not receive a selection. Goodbye."

QUEUE_INTRO = "All of our agents are currently assisting other customers. Please stay on the line."
HOLD_ANNOUNCEMENTS = (
    "Your call is important to us. Please continue to hold.",
    "Thank you for your patience. An agent will be with you shortly.",
    "Did you know you can manage your account online at acme dot example?",
)
CALLBACK_OFFER = (
    "To receive a callback instead of waiting, press 1. Otherwise, please stay on the line."
)
CALLBACK_ASK_NUMBER = "Please enter the ten digit number we should call, followed by the pound key."
CALLBACK_CONFIRM = "Thank you. We will call you back at the number you entered. Goodbye."
CONNECTING = "Thank you for holding. Connecting you to a representative now."

# Built-in scripted rep, used when no rep number / bot-rep URL is configured.
# Mirrors the demo: PIN request (ask_user), paid upgrade offer (guard), fee waived.
REP_SCRIPT: tuple[RepLine, ...] = (
    RepLine("Thanks for holding, this is Alex in billing. How can I help you today?"),
    RepLine(
        "I can look into that for you. First, can you give me the account PIN?",
        expects_turns=2,
    ),
    RepLine(
        "Thank you, you are verified. I see the forty dollar late fee. While I have you, our "
        "premium plan is nine ninety nine a month and avoids late fees. Would you like to upgrade?"
    ),
    RepLine(
        "No problem. I have waived the forty dollar late fee. "
        "Your reference number is A 7 X 2 9 1. Is there anything else I can help with?"
    ),
    RepLine("Thanks for calling Acme. Have a great day. Goodbye.", hangup_after=True),
)
REP_NUDGE = "Hello? Are you still there?"
REP_TAKE_YOUR_TIME = "Sure, take your time."

# Phrases that mean "the agent is fetching something; don't move on yet".
HOLDING_PHRASES = ("one moment", "just a moment", "one second", "let me get", "bear with")


def is_holding_line(speech: str) -> bool:
    s = speech.lower()
    return any(p in s for p in HOLDING_PHRASES)


@dataclass(frozen=True)
class Segment:
    """One thing the line says or plays. `label` is the ground-truth state."""

    key: str  # stable id, e.g. "menu:main", "hold:music", "rep:2"
    label: StateCheckLabel
    text: str = ""  # empty for music
    seconds: float | None = None  # music duration; speech length is derived from text
    meta: dict[str, str] = field(default_factory=dict)


def menu_segment(name: str) -> Segment:
    node = NODES[name]
    assert isinstance(node, Menu)
    return Segment(key=f"menu:{name}", label=StateCheckLabel.MENU, text=node.prompt)


def message_segment(name: str) -> Segment:
    node = NODES[name]
    assert isinstance(node, Message)
    return Segment(key=f"message:{name}", label=StateCheckLabel.RECORDED_MESSAGE, text=node.text)


def recorded(key: str, text: str) -> Segment:
    return Segment(key=key, label=StateCheckLabel.RECORDED_MESSAGE, text=text)


def rep_segment(turn: int) -> Segment:
    return Segment(key=f"rep:{turn}", label=StateCheckLabel.HUMAN, text=REP_SCRIPT[turn].text)
