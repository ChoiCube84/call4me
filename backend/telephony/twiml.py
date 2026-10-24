"""Tiny TwiML builder (no Twilio SDK dependency).

    Response().say("Hi").gather(...).to_xml()

Every attribute and text value is XML-escaped.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

IVR_VOICE = "Polly.Joanna"
REP_VOICE = "Polly.Matthew"


def _attrs(attrs: dict[str, object]) -> str:
    parts = []
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f" {k}={quoteattr(str(v))}")
    return "".join(parts)


def _el(tag: str, body: str = "", **attrs: object) -> str:
    a = _attrs(attrs)
    return f"<{tag}{a}>{body}</{tag}>" if body else f"<{tag}{a}/>"


class Verbs:
    """Shared verb builders for <Response> and nested <Gather>."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def raw(self, xml: str) -> Verbs:
        self._parts.append(xml)
        return self

    def say(self, text: str, voice: str = IVR_VOICE) -> Verbs:
        return self.raw(_el("Say", escape(text), voice=voice))

    def play(self, url: str = "", loop: int | None = None, digits: str | None = None) -> Verbs:
        return self.raw(_el("Play", escape(url), loop=loop, digits=digits))

    def pause(self, seconds: int = 1) -> Verbs:
        return self.raw(_el("Pause", length=seconds))

    def body(self) -> str:
        return "".join(self._parts)


class Response(Verbs):
    def gather(self, inner: Verbs | None = None, **attrs: object) -> Response:
        self.raw(_el("Gather", inner.body() if inner else "", **attrs))
        return self

    def redirect(self, url: str) -> Response:
        self.raw(_el("Redirect", escape(url), method="POST"))
        return self

    def hangup(self) -> Response:
        self.raw("<Hangup/>")
        return self

    def dial(self, number: str, caller_id: str | None = None) -> Response:
        self.raw(_el("Dial", escape(number), callerId=caller_id))
        return self

    def conference(
        self, room: str, *, end_on_exit: bool = False, beep: bool = False, wait_url: str = ""
    ) -> Response:
        conf = _el(
            "Conference",
            escape(room),
            beep=beep,
            startConferenceOnEnter=True,
            endConferenceOnExit=end_on_exit,
            waitUrl=wait_url,
        )
        self.raw(_el("Dial", conf))
        return self

    def connect_stream(self, url: str, params: dict[str, str]) -> Response:
        """Bidirectional stream (we can send audio back). Blocks the call's TwiML."""
        self.raw(_el("Connect", _stream(url, params)))
        return self

    def start_stream(
        self, url: str, params: dict[str, str], name: str, track: str = "inbound_track"
    ) -> Response:
        """Unidirectional fork (listen only); TwiML continues to the next verb."""
        self.raw(_el("Start", _stream(url, params, name=name, track=track)))
        return self

    def stop_stream(self, name: str) -> Response:
        self.raw(_el("Stop", _el("Stream", name=name)))
        return self

    def to_xml(self) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{self.body()}</Response>'


def _stream(url: str, params: dict[str, str], **attrs: object) -> str:
    inner = "".join(_el("Parameter", name=k, value=v) for k, v in params.items())
    return _el("Stream", inner, url=url, **attrs)
