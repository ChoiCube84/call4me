"""Jev checks, menu parsing/navigation, saved routes, and timers."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from backend.contracts import AudioLabel, AudioLabelKind, CallState, DecisionClient, JevDecision
from backend.engine import jev_checks
from backend.engine.menu import (
    DbMenuPathStore,
    MemoryMenuPathStore,
    MenuNavigator,
    parse_menu_options,
)
from backend.engine.timers import CallTimers, TimerEvent
from tests.engine.fakes import FakeClock

# --- state check heuristics (mock Jev) ----------------------------------------


@pytest.mark.parametrize(
    ("text", "label", "expected"),
    [
        ("For billing, press 2. For orders, press 1.", None, "menu"),
        ("Your call is important to us. Please continue to hold.", None, "recorded_message"),
        ("", AudioLabelKind.MUSIC, "hold_music"),
        ("", AudioLabelKind.SILENCE, "silence"),
        ("Hi, my name is Sarah, how can I help you today?", None, "human"),
        ("Please leave a message after the tone.", None, "voicemail"),
        ("Press 1 and we will call you back when an agent is free.", None, "callback_offer"),
        ("Thank you for calling. Goodbye.", None, "call_ended"),
    ],
)
async def test_mock_state_check(text, label, expected) -> None:
    audio = AudioLabel(label=label, confidence=0.9) if label else None
    d = await jev_checks.classify_state(
        jev_checks.MockDecisionClient(),
        transcript=text,
        audio_label=audio,
        current_state=CallState.HOLD,
    )
    assert d.choice == expected
    assert d.question == jev_checks.STATE_CHECK


class _Slow(DecisionClient):
    async def choose(self, question, options, context) -> JevDecision:
        await asyncio.sleep(10)
        raise AssertionError


class _Broken(DecisionClient):
    async def choose(self, question, options, context) -> JevDecision:
        raise ConnectionError("down")


class _Bogus(DecisionClient):
    async def choose(self, question, options, context) -> JevDecision:
        return JevDecision(question=question, choice="dance", probability=0.99)


@pytest.mark.parametrize("client", [_Slow(), _Broken(), _Bogus()])
async def test_jev_failure_falls_back_with_discounted_confidence(client) -> None:
    d = await jev_checks.classify_state(
        client,
        transcript="Hi, my name is Sarah, how can I help?",
        audio_label=None,
        current_state=CallState.HOLD,
        timeout_s=0.01,
    )
    assert d.choice == "human"
    assert "fallback" in d.alternatives
    # Fallbacks never clear the 0.8 human fast-path bar on their own.
    assert d.probability < 0.8


async def test_real_jev_client_maps_typesafe_response(monkeypatch) -> None:
    seen = {}

    class Choice:
        def __init__(self, *, instructions, criteria):
            seen["instructions"], seen["criteria"] = instructions, criteria

    class FakeClient:
        def __init__(self, **kw):
            seen["init"] = kw

        async def system_one(self, *, state, questions):
            seen["state"] = state
            [name] = questions
            answer = types.SimpleNamespace(
                choice="hold_music", confidence=0.87, probabilities={"hold_music": 0.87}
            )
            return types.SimpleNamespace(choices={name: answer})

    fake = types.ModuleType("typesafe_sdk")
    fake.Choice = Choice
    fake.AsyncTypeSafeClient = FakeClient
    monkeypatch.setitem(sys.modules, "typesafe_sdk", fake)

    client = jev_checks.JevDecisionClient(api_key="k")
    d = await jev_checks.classify_state(
        client,
        transcript="",
        audio_label=AudioLabel(label=AudioLabelKind.MUSIC, confidence=0.9),
        current_state=CallState.HOLD,
    )
    assert (d.choice, d.probability) == ("hold_music", 0.87)
    assert seen["init"] == {"api_key": "k", "model": "jev-latest"}
    assert set(seen["criteria"]) == set(jev_checks.STATE_OPTIONS)
    assert seen["criteria"]["voicemail"]  # options carry descriptions
    assert "option_descriptions" not in seen["state"]
    assert seen["state"]["audio_label"] == "music"


def test_get_decision_client_respects_use_mocks(monkeypatch) -> None:
    from backend.config import get_settings

    monkeypatch.setenv("USE_MOCKS", "true")
    get_settings.cache_clear()
    try:
        assert isinstance(jev_checks.get_decision_client(), jev_checks.MockDecisionClient)
    finally:
        get_settings.cache_clear()


async def test_confirm_human_heuristics() -> None:
    mock = jev_checks.MockDecisionClient()
    yes = await jev_checks.confirm_human(mock, greeting="Hi", response="Sure, how can I help?")
    no = await jev_checks.confirm_human(
        mock, greeting="Hi", response="Your call is important to us. Please hold."
    )
    assert yes.choice == "human" and yes.probability >= 0.6
    assert no.choice == "recording" and no.probability >= 0.6


# --- menu parsing --------------------------------------------------------------


def test_parse_label_then_key_and_say() -> None:
    opts = parse_menu_options(
        "Thank you for calling. For orders, press 1. For billing, press 2. "
        "To speak with an agent, press 0. Or say representative."
    )
    assert opts == {
        "1": "orders",
        "2": "billing",
        "0": "speak with an agent",
        "say:representative": "representative",
    }


def test_parse_key_then_label_and_number_words() -> None:
    opts = parse_menu_options(
        "Press one for orders, press 2 for billing questions, press 9 to repeat these options."
    )
    assert opts == {"1": "orders", "2": "billing questions", "9": "repeat these options"}


def test_parse_nothing_from_recording() -> None:
    assert parse_menu_options("Your call is important to us.") == {}


# --- navigator -----------------------------------------------------------------

MENU = {"1": "orders", "2": "billing", "0": "speak with an agent"}


async def test_navigator_picks_goal_option() -> None:
    nav = MenuNavigator(jev_checks.MockDecisionClient(), goal="Remove a late fee on my billing")
    action = await nav.decide(MENU, "menu")
    assert (action.kind, action.value) == ("dtmf", "2")
    assert nav.steps == ["2"]


async def test_navigator_low_confidence_falls_back_to_agent() -> None:
    jev = jev_checks.MockDecisionClient()
    jev.script(jev_checks.MENU_CHOICE, "1", 0.4)
    nav = MenuNavigator(jev, goal="something odd")
    action = await nav.decide(MENU, "menu")
    assert action.value == "0" and "low confidence" in action.reason


async def test_navigator_low_confidence_without_zero_says_representative() -> None:
    jev = jev_checks.MockDecisionClient()
    jev.script(jev_checks.MENU_CHOICE, "1", 0.3)
    nav = MenuNavigator(jev, goal="x")
    action = await nav.decide({"1": "orders", "say:billing": "billing"}, "menu")
    assert (action.kind, action.value) == ("say", "representative")


async def test_navigator_menu_loop_escalates_press_0_then_representative() -> None:
    nav = MenuNavigator(jev_checks.MockDecisionClient(), goal="billing fee")
    actions = [await nav.decide(MENU, "menu") for _ in range(4)]
    assert [a.value for a in actions[:3]] == ["2", "2", "2"]
    assert (actions[3].kind, actions[3].value) == ("dtmf", "0")
    assert "repeated" in actions[3].reason
    more = [await nav.decide(MENU, "menu") for _ in range(4)]
    assert (more[3].kind, more[3].value) == ("say", "representative")


async def test_navigator_replays_saved_path() -> None:
    jev = jev_checks.MockDecisionClient()
    nav = MenuNavigator(jev, goal="billing", saved_path=["1", "3"])
    a1 = await nav.decide(MENU, "menu")
    a2 = await nav.decide({"3": "late fees", "4": "payments"}, "sub menu")
    assert (a1.value, a1.reason) == ("1", "saved menu path")
    assert a2.value == "3"
    assert jev.calls == []  # no Jev call needed for a known route


async def test_navigator_open_prompt_says_representative() -> None:
    nav = MenuNavigator(jev_checks.MockDecisionClient(), goal="x")
    action = await nav.decide({}, "In a few words, tell me what you're calling about.")
    assert (action.kind, action.value) == ("say", "representative")
    assert await nav.decide({}, "please wait") is None


async def test_memory_menu_path_store() -> None:
    store = MemoryMenuPathStore()
    assert await store.load("Acme") is None
    await store.save_success("Acme", ["2", "1"])
    await store.save_success("Acme", ["2", "1"])
    assert await store.load("Acme") == ["2", "1"]
    assert store.paths["Acme"][1] == 2


async def test_db_menu_path_store(db) -> None:
    store = DbMenuPathStore()
    assert await store.load("Acme") is None
    await store.save_success("Acme", ["9"])
    await store.save_success("Acme", ["2", "1"])
    await store.save_success("Acme", ["2", "1"])
    assert await store.load("Acme") == ["2", "1"]


# --- timers --------------------------------------------------------------------


def test_silence_recheck_fires_once_per_silent_stretch() -> None:
    clock = FakeClock()
    t = CallTimers(max_hold_s=3600, clock=clock)
    clock.advance(29)
    assert t.due() == []
    clock.advance(2)
    assert t.due() == [TimerEvent.SILENCE_RECHECK]
    clock.advance(30)
    assert t.due() == []
    t.heard_sound()
    clock.advance(31)
    assert t.due() == [TimerEvent.SILENCE_RECHECK]


def test_max_hold_accumulates_across_hold_stretches_and_extends() -> None:
    clock = FakeClock()
    t = CallTimers(max_hold_s=100, clock=clock)
    t.hold_started()
    clock.advance(60)
    t.hold_stopped()
    clock.advance(500)  # not on hold: doesn't count
    t.heard_sound()
    t.hold_started()
    clock.advance(41)
    t.heard_sound()
    assert t.due() == [TimerEvent.MAX_HOLD]
    assert t.due() == []
    t.extend_hold(50)
    clock.advance(51)
    t.heard_sound()
    assert t.due() == [TimerEvent.MAX_HOLD]
