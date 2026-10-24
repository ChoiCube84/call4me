from __future__ import annotations

from backend.contracts import LiveMessage
from backend.notify.live import LiveHub


def _caption(session: str, text: str) -> LiveMessage:
    return LiveMessage(type="caption", session_id=session, data={"speaker": "remote", "text": text})


def test_reconnect_replays_the_call_so_far() -> None:
    hub = LiveHub()
    hub.publish(_caption("c1", "Thanks for calling."))
    hub.publish(_caption("c2", "Another call."))
    fresh = hub.subscribe("c1")
    assert fresh.empty()  # internal subscribers only get new messages
    again = hub.subscribe("c1", replay=True)
    assert [again.get_nowait().data["text"]] == ["Thanks for calling."]
    hub.publish(_caption("c1", "How can I help?"))
    assert again.get_nowait().data["text"] == "How can I help?"


def test_backlog_is_bounded(monkeypatch) -> None:
    from backend.notify import live

    monkeypatch.setattr(live, "BACKLOG_CALLS", 2)
    hub = LiveHub()
    for s in ("a", "b", "c"):
        hub.publish(_caption(s, s))
    assert hub.subscribe("a", replay=True).empty()  # oldest call dropped
    assert not hub.subscribe("c", replay=True).empty()
