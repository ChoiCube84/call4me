"""The fake support line's script, played by the in-process mock line
(`backend/telephony/mock_client.py`) for the browser fake phone and tests.

Call flow::

    welcome -> main menu
      1 orders   -> recorded message -> hold queue
      2 refunds  -> returns and refunds menu
                      1 refund status   -> recorded message -> refunds menu
                      2 damaged/missing -> hold queue
                      0 associate       -> hold queue
                      9 back            -> main menu
      0 associate -> hold queue
    hold queue: music for a random 30-120 s with periodic announcements, sometimes a
    callback offer (press 1), then a hand-off to a rep (a forwarded number, an
    external bot-rep URL, or the built-in scripted rep below).

Each segment carries the `StateCheckLabel` a perfect classifier would give it, so
tests and evals can score call-engine decisions against ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.contracts import StateCheckLabel

COMPANY = "Amazon Customer Service"
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
    "Thank you for calling Amazon customer service. Your call may be monitored for quality. "
    "Please listen carefully, as our menu options have changed."
)

NODES: dict[str, Menu | Message] = {
    "main": Menu(
        prompt=(
            "For help with an order, press 1. For returns and refunds, press 2. "
            "To speak with an associate, press 0."
        ),
        options={"1": "orders", "2": "refunds", "0": "queue"},
    ),
    "refunds": Menu(
        prompt=(
            "Returns and refunds. To check the status of a refund, press 1. For a damaged or "
            "missing item, press 2. To speak with an associate, press 0. To return to the main "
            "menu, press 9."
        ),
        options={"1": "refund_status", "2": "queue", "0": "queue", "9": "main"},
    ),
    "orders": Message(
        text=(
            "You can track any order in the Your Orders section of the Amazon app. "
            "Please hold for the next available associate."
        ),
        next="queue",
    ),
    "refund_status": Message(
        text=(
            "Refunds usually appear on your original payment method within three to five "
            "business days. Returning to the returns and refunds menu."
        ),
        next="refunds",
    ),
}

INVALID_OPTION = "Sorry, that is not a valid option."
NO_INPUT_GOODBYE = "We did not receive a selection. Goodbye."

QUEUE_INTRO = (
    "All of our associates are currently assisting other customers. Please stay on the line."
)
HOLD_ANNOUNCEMENTS = (
    "Your call is important to us. Please continue to hold.",
    "Thank you for your patience. An associate will be with you shortly.",
    "Did you know you can start most returns in the Your Orders section of the Amazon app?",
)
CALLBACK_OFFER = (
    "To receive a callback instead of waiting, press 1. Otherwise, please stay on the line."
)
CALLBACK_ASK_NUMBER = "Please enter the ten digit number we should call, followed by the pound key."
CALLBACK_CONFIRM = "Thank you. We will call you back at the number you entered. Goodbye."
CONNECTING = "Thank you for holding. Connecting you to an associate now."

# Built-in scripted rep, used when no rep number / bot-rep URL is configured.
# Mirrors the demo: a verification question (ask_user, or the user's pre-call info),
# a paid Prime upsell (guard), then the refund and a reference number.
REP_SCRIPT: tuple[RepLine, ...] = (
    RepLine("Thanks for calling Amazon, this is Alex. How can I help you today?"),
    RepLine(
        "I'm sorry to hear that. I can help. First, can I get the order number, please?",
        expects_turns=2,
    ),
    RepLine(
        "Thank you, you're verified. I see the order for the wireless headphones. While I have "
        "you, would you like to try Prime? It's free for thirty days, then fourteen ninety nine "
        "a month. Would you like to upgrade?"
    ),
    RepLine(
        "No problem. I've issued a full refund of forty nine ninety nine to your original "
        "payment method. Your reference number is A 7 X 2 9 1. Is there anything else I can "
        "help with?"
    ),
    RepLine("Thanks for calling Amazon. Have a great day. Goodbye.", hangup_after=True),
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
