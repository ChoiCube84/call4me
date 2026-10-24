"""CallEngine scenarios on fakes: menu, hold, human fast path, transfers, callbacks."""

from __future__ import annotations

from backend.contracts import AudioLabel, AudioLabelKind, CallState
from backend.engine import jev_checks
from backend.engine.call_engine import EngineConfig
from backend.engine.menu import MemoryMenuPathStore
from tests.engine.fakes import FakeTTS, Harness

S = CallState
MENU_TEXT = (
    "Thank you for calling. For orders, press 1. For billing, press 2. "
    "To speak with an agent, press 0."
)
REP_HELLO = "Hi, thanks for holding, my name is Sarah. How can I help you today?"


async def music(h: Harness, confidence: float = 0.95) -> None:
    await h.engine.on_audio_label(AudioLabel(label=AudioLabelKind.MUSIC, confidence=confidence))


async def to_hold(h: Harness) -> None:
    await h.engine.start()
    await h.hear(MENU_TEXT)
    await h.settle()
    await music(h)
    await h.settle()
    assert h.engine.state is S.HOLD


async def test_full_path_menu_hold_human_conversation() -> None:
    h = Harness()
    await h.engine.start()
    assert h.tts.cache  # greeting pre-generated before any human

    await h.hear(MENU_TEXT)
    assert h.engine.state is S.MENU
    await h.engine.tick()  # menu just ended: wait to settle before pressing
    assert h.telephony.digits == []
    await h.settle()
    assert h.telephony.digits == ["2"]  # billing serves "remove the late fee"

    await music(h)
    await h.settle()
    assert h.engine.state is S.HOLD
    assert h.stt.paused == 1  # STT paused on confirmed hold music

    await h.engine.on_audio_label(AudioLabel(label=AudioLabelKind.SPEECH, confidence=0.9))
    assert h.stt.resumed == 1
    await h.hear("Your call is important to us. Please stay on the line.")
    assert h.engine.state is S.RECORDED_MESSAGE
    await h.engine.on_audio_label(AudioLabel(label=AudioLabelKind.MUSIC, confidence=0.6))
    await h.settle(11)  # the announcement has left the 10 s transcript window
    assert h.engine.state is S.HOLD

    await h.hear(REP_HELLO)
    assert h.engine.state is S.HUMAN_SUSPECTED
    await h.flush()
    assert h.telephony.audio == [b"pre:" + h.session.disclosure_text.encode()]
    assert h.session.disclosure_given
    assert h.engine.time_to_greet_s is not None and h.engine.time_to_greet_s < 1.5
    assert h.events("greeting_played")[0]["source"] == "pregenerated"

    await h.hear("Sure, no problem. What can I do for you?")
    assert h.engine.state is S.IN_CONVERSATION
    assert h.conversations == ["Sure, no problem. What can I do for you?"]
    alerts = [m for m in h.notifier.sent if m.get("category") == "HUMAN_ANSWERED"]
    assert alerts and alerts[0]["data"]["haptic"] is True
    assert await h.menu_paths.load(h.session.company) == ["2"]

    await h.hear("Can I get the account number please?")
    assert h.remote_turns == ["Can I get the account number please?"]

    assert h.path == [
        "MENU",
        "HOLD",
        "RECORDED_MESSAGE",
        "HOLD",
        "HUMAN_SUSPECTED",
        "HUMAN_CONFIRMED",
        "IN_CONVERSATION",
    ]
    # Every transition carries a reason; Jev-driven ones carry a confidence.
    assert all(t.reason for t in h.recorder.transitions)
    assert h.recorder.transitions[0].confidence is not None
    # Running Jev decision count for the dashboard.
    counts = [e["count"] for e in h.events("jev_decision")]
    assert counts == list(range(1, len(counts) + 1)) and counts
    # Live Activity followed each state.
    assert [m["live_activity"] for m in h.notifier.sent if "live_activity" in m][-1] is (
        S.IN_CONVERSATION
    )


async def test_partial_menu_waits_for_settle() -> None:
    h = Harness()
    await h.hear(MENU_TEXT)
    await h.hear("For new accounts, press", final=False)
    await h.settle()
    assert h.telephony.digits == []  # still speaking


async def test_saved_menu_path_is_replayed_when_enabled() -> None:
    store = MemoryMenuPathStore()
    await store.save_success("Fake Support Line", ["0"])
    h = Harness(menu_paths=store, config=EngineConfig(replay_menu_paths=True))
    await h.engine.start()
    await h.hear(MENU_TEXT)
    await h.settle()
    assert h.telephony.digits == ["0"]
    assert h.events("menu_action")[0]["reason"] == "saved menu path"


async def test_menu_loop_presses_zero() -> None:
    h = Harness()
    await h.engine.start()
    for _ in range(4):
        await h.hear(MENU_TEXT)
        await h.settle()
    assert h.telephony.digits == ["2", "2", "2", "0"]
    assert "repeated" in h.events("menu_action")[-1]["reason"]


async def test_low_confidence_menu_choice_presses_zero() -> None:
    h = Harness()
    h.jev.script(jev_checks.STATE_CHECK, "menu", 0.9)
    h.jev.script(jev_checks.MENU_CHOICE, "1", 0.3)
    await h.hear(MENU_TEXT)
    await h.settle()
    assert h.telephony.digits == ["0"]


async def test_low_confidence_state_check_does_not_move() -> None:
    h = Harness()
    h.jev.script(jev_checks.STATE_CHECK, "menu", 0.4)
    await h.hear("Hmm.")
    assert h.engine.state is S.DIALING


async def test_human_below_threshold_is_not_greeted() -> None:
    h = Harness()
    await to_hold(h)
    h.jev.script(jev_checks.STATE_CHECK, "human", 0.5)
    await h.hear("Uh, hello?")
    assert h.engine.state is S.HOLD
    assert h.telephony.audio == []


async def test_two_moderate_human_checks_in_a_row_are_greeted() -> None:
    h = Harness()
    await to_hold(h)
    h.jev.script(jev_checks.STATE_CHECK, "human", 0.5)
    await h.hear("This is Alex in billing.")
    assert h.engine.state is S.HOLD
    h.jev.script(jev_checks.STATE_CHECK, "human", 0.47)
    await h.hear("How can I help you today?")
    assert h.engine.state is S.HUMAN_SUSPECTED


async def test_false_human_returns_to_hold_and_stops_greeting() -> None:
    h = Harness()
    await to_hold(h)
    await h.hear(REP_HELLO)
    assert h.engine.state is S.HUMAN_SUSPECTED
    await h.flush()  # the recording carries on after our greeting
    await h.hear("Your call is important to us. Please continue to hold.")
    assert h.engine.state is S.HOLD
    assert h.path[-2:] == ["HUMAN_SUSPECTED", "HOLD"]


async def test_no_reply_to_greeting_times_out_to_hold() -> None:
    h = Harness()
    await to_hold(h)
    await h.hear(REP_HELLO)
    await h.flush()
    await h.settle(5)
    assert h.engine.state is S.HUMAN_SUSPECTED
    await h.settle(5)
    assert h.engine.state is S.HOLD
    assert h.recorder.transitions[-1].reason == "no reply to greeting"


async def test_greeting_falls_back_to_live_tts_when_not_pregenerated() -> None:
    h = Harness(tts=FakeTTS(cache_works=False))
    await to_hold(h)
    await h.hear(REP_HELLO)
    await h.flush()
    assert h.telephony.audio == [b"live:" + h.session.disclosure_text.encode()]
    assert h.events("greeting_played")[0]["source"] == "live"


async def test_transfer_returns_to_hold_then_new_rep_gets_disclosure_again() -> None:
    h = Harness()
    await to_hold(h)
    await h.hear(REP_HELLO)
    await h.flush()
    await h.hear("Sure, what can I do for you?")
    assert h.engine.state is S.IN_CONVERSATION
    await h.hear("Please hold while I transfer you to billing.")
    assert h.engine.state is S.HOLD
    assert h.recorder.transitions[-1].reason == "transfer announced"
    await h.hear("Billing department, this is Tom speaking, how may I help?")
    await h.flush()
    assert h.engine.state is S.HUMAN_SUSPECTED
    assert len(h.telephony.audio) == 2  # greeting (with disclosure) played again


async def test_transfer_recording_while_waiting_goes_to_hold() -> None:
    h = Harness()
    await to_hold(h)
    await h.hear("Please hold while we transfer you to the next available agent.")
    assert h.engine.state is S.HOLD


async def test_waiting_on_user_round_trip_and_illegal_command() -> None:
    h = Harness()
    await to_hold(h)
    assert not await h.engine.user_answered()  # HOLD -> IN_CONVERSATION is illegal
    assert h.recorder.transitions[-1].allowed is False
    await h.hear(REP_HELLO)
    await h.flush()
    await h.hear("Sure, go ahead.")
    assert await h.engine.waiting_on_user("ask_user: PIN")
    assert h.engine.state is S.WAITING_ON_USER
    assert await h.engine.user_answered()
    assert h.engine.state is S.IN_CONVERSATION


async def test_takeover_stops_speech_and_handback_resumes_conversation() -> None:
    h = Harness()
    await to_hold(h)
    await h.hear(REP_HELLO)
    await h.flush()
    await h.hear("Sure, go ahead.")
    cleared = h.telephony.cleared
    assert await h.engine.takeover()
    assert h.engine.state is S.TAKEOVER
    assert h.telephony.cleared > cleared and h.telephony.takeovers == 1
    await h.hear("And your account number?")
    assert h.remote_turns == []  # agent is silent during takeover
    assert await h.engine.handback()
    assert h.engine.state is S.IN_CONVERSATION
    assert h.telephony.takeovers_ended == 1
    assert len(h.conversations) == 2


async def test_takeover_during_hold_hands_back_to_hold() -> None:
    h = Harness()
    await to_hold(h)
    assert await h.engine.takeover()
    await h.settle()
    assert h.engine.state is S.TAKEOVER  # no Jev-driven moves while the user has the line
    assert await h.engine.handback()
    assert h.engine.state is S.HOLD


async def test_silence_recheck_runs_a_check_even_when_idle() -> None:
    h = Harness(config=EngineConfig())
    await to_hold(h)
    before = len(h.jev.calls)
    h.clock.advance(31)
    await h.engine.tick()
    reasons = [e["reason"] for e in h.events("jev_decision")]
    assert "silence > 30 s re-check" in reasons
    assert len(h.jev.calls) > before


async def test_max_hold_notifies_user_and_stop_ends_call() -> None:
    h = Harness()
    await to_hold(h)
    for _ in range(31):
        await music(h)
        await h.settle(60)
    alerts = [m for m in h.notifier.sent if m.get("category") == "HOLD_CONTINUE"]
    assert len(alerts) == 1
    await h.engine.continue_hold(False)
    assert h.engine.state is S.ENDED
    assert h.telephony.hung_up and h.stt.stopped
    assert h.engine.outcome == "user_cancelled"


async def test_max_hold_keep_waiting_extends() -> None:
    h = Harness()
    await to_hold(h)
    for _ in range(31):
        await music(h)
        await h.settle(60)
    await h.engine.continue_hold(True)
    assert h.engine.state is S.HOLD
    for _ in range(14):
        await music(h)
        await h.settle(60)
    assert len([m for m in h.notifier.sent if m.get("category") == "HOLD_CONTINUE"]) == 1


async def test_callback_offer_accepted_with_agent_number() -> None:
    h = Harness(callback_number="+1 (555) 123-4567")
    await to_hold(h)
    await h.hear(
        "To receive a call back when an agent is available, press 1. To keep holding, press 2."
    )
    assert h.engine.state is S.CALLBACK_OFFERED
    assert h.telephony.digits[-1] == "1"
    assert h.engine.callback_accepted
    await h.hear("Please enter the phone number where we can reach you, followed by pound.")
    assert h.telephony.digits[-1] == "5551234567#"  # the agent's number, never the user's
    await h.hear("Thank you. We will call you back. Goodbye.")
    assert h.engine.state is S.ENDED
    assert h.engine.outcome == "callback_scheduled"
    assert any(m.get("title") == "Callback requested" for m in h.notifier.sent)


async def test_callback_offer_declined_without_agent_number() -> None:
    h = Harness(callback_number="")
    await to_hold(h)
    await h.hear(
        "To receive a call back when an agent is available, press 1. To keep holding, press 2."
    )
    assert h.telephony.digits[-1] == "2"
    assert h.engine.state is S.HOLD
    assert not h.engine.callback_accepted


async def test_voicemail_hangs_up_and_notifies() -> None:
    h = Harness()
    await h.hear("You have reached our voicemail. Please leave a message after the tone.")
    assert h.engine.state is S.VOICEMAIL
    assert h.telephony.hung_up
    assert any(m.get("title") == "Reached voicemail" for m in h.notifier.sent)


async def test_remote_hangup_and_line_status() -> None:
    h = Harness()
    await to_hold(h)
    await h.engine.on_call_status("completed")
    assert h.engine.state is S.ENDED and h.engine.outcome == "remote_hangup"
    assert not h.telephony.hung_up  # already gone; don't hang up twice
    await h.hear(REP_HELLO)  # ignored after the end
    assert h.engine.state is S.ENDED

    h2 = Harness()
    await h2.engine.on_call_status("busy")
    assert h2.engine.state is S.FAILED


async def test_agent_speech_is_ignored() -> None:
    from backend.contracts import Speaker, Transcript

    h = Harness()
    await h.engine.on_transcript(
        Transcript(session_id=h.session.id, text=REP_HELLO, is_final=True, speaker=Speaker.AGENT)
    )
    assert h.engine.state is S.DIALING and h.jev.calls == []


async def test_run_loop_stops_at_terminal_state() -> None:
    h = Harness(config=EngineConfig(check_interval_s=0))
    await h.hear("Please leave a message after the tone.")
    await h.engine.run()  # returns immediately: already VOICEMAIL
    assert h.engine.done


async def test_db_recorder_end_to_end_audit_and_live_channel(db) -> None:
    from sqlalchemy import select

    from backend.db.models import AuditLogRow, CallEventRow, CallSessionRow
    from backend.db.session import sessionmaker
    from backend.engine.states import DbRecorder
    from backend.notify.live import hub

    h = Harness()
    h.engine.deps.recorder = DbRecorder()
    s = h.session
    async with sessionmaker()() as dbs:
        dbs.add(
            CallSessionRow(
                id=s.id,
                user_id=s.user_id,
                company=s.company,
                phone_number=s.phone_number,
                goal=s.goal,
                limits_json={},
                disclosure_text=s.disclosure_text,
                state=s.state,
            )
        )
        await dbs.commit()
    q = hub.subscribe(s.id)
    try:
        await to_hold(h)
    finally:
        hub.unsubscribe(s.id, q)

    async with sessionmaker()() as dbs:
        actions = [a.action for a in (await dbs.scalars(select(AuditLogRow))).all()]
        kinds = [e.kind for e in (await dbs.scalars(select(CallEventRow))).all()]
        row = await dbs.get(CallSessionRow, s.id)
    assert {"state.transition", "jev.decision", "menu.press"} <= set(actions)
    assert {"transition", "jev_decision", "menu_action"} <= set(kinds)
    assert row.state == "HOLD"
    live = []
    while not q.empty():
        live.append(q.get_nowait())
    assert [m.data["to"] for m in live if m.type == "state"] == ["MENU", "HOLD"]
    assert any(m.type == "event" and m.data["kind"] == "jev_decision" for m in live)


async def test_rep_talking_over_greeting_is_not_judged_as_reply() -> None:
    """STT splits "This is Alex. How can I help?"; the second half lands during our
    greeting and must not be scored as a recording ignoring us."""
    h = Harness()
    await to_hold(h)
    await h.hear(REP_HELLO)
    assert h.engine.state is S.HUMAN_SUSPECTED
    await h.hear("How can I help you today?")  # greeting still playing
    assert h.engine.state is S.HUMAN_SUSPECTED
    assert not [c for c in h.jev.calls if c[0] == jev_checks.HUMAN_CONFIRMATION]
    await h.flush()
    await h.hear("Sure, I can look into that for you.")
    assert h.engine.state is S.IN_CONVERSATION


async def test_live_person_check_catches_rep_saying_hello_on_hold() -> None:
    h = Harness()
    await to_hold(h)
    h.jev.script(jev_checks.LIVE_PERSON, "live_person", 0.63)
    h.jev.script(jev_checks.STATE_CHECK, "hold_music", 0.79)  # the 8-way check misses it
    await h.hear("Hello? Are you there?")
    assert h.engine.state is S.HUMAN_SUSPECTED


async def test_live_person_check_ignores_hold_announcement() -> None:
    h = Harness()
    await to_hold(h)
    h.jev.script(jev_checks.LIVE_PERSON, "automated", 0.99)
    h.jev.script(jev_checks.STATE_CHECK, "hold_music", 0.8)
    await h.hear("Your call is important to us. Please continue to hold.")
    assert h.engine.state is S.HOLD
