"""Claude client behind a small interface, with a mock twin for USE_MOCKS=true.

Business logic only sees `LLMClient`; the Anthropic SDK is imported lazily in
`AnthropicLLMClient` so tests and mock runs need no network or API key.
"""

from __future__ import annotations

import abc
import itertools
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.config import get_settings

DEFAULT_MODEL = "claude-opus-5-5"
# Phone turns are latency-critical (section 16). Opus 5.5 always thinks; effort is the dial.
DEFAULT_EFFORT = "low"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    # Content to append back verbatim as the assistant turn (keeps thinking blocks intact).
    content: list[Any]
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str = "end_turn"


class LLMClient(abc.ABC):
    @abc.abstractmethod
    async def respond(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        """One Claude turn with tools available."""

    @abc.abstractmethod
    async def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        """One plain text completion (greeting line, summaries)."""


# --------------------------------------------------------------------------- #
# Real client
# --------------------------------------------------------------------------- #


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 4096,
        timeout_s: float = 20.0,
    ) -> None:
        import anthropic  # lazy: only needed for real calls

        self.model = model or os.environ.get("CALL4ME_CLAUDE_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("CALL4ME_CLAUDE_EFFORT", DEFAULT_EFFORT)
        self.max_tokens = max_tokens
        key = api_key or get_settings().anthropic_api_key or None
        self._client = anthropic.AsyncAnthropic(api_key=key, timeout=timeout_s, max_retries=1)

    async def _create(self, **kwargs: Any) -> Any:
        return await self._client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            output_config={"effort": self.effort},
            # Server-side fallback on a refusal so the call does not stall mid-conversation.
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
            **kwargs,
        )

    async def respond(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        resp = await self._create(
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto"},
        )
        calls: list[ToolCall] = []
        texts: list[str] = []
        for block in resp.content:
            if block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
            elif block.type == "text":
                texts.append(block.text)
        return LLMResponse(
            content=list(resp.content),
            tool_calls=calls,
            text=" ".join(texts).strip(),
            stop_reason=resp.stop_reason or "end_turn",
        )

    async def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        resp = await self._create(system=system, messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason == "refusal":
            return ""
        return " ".join(b.text for b in resp.content if b.type == "text").strip()


# --------------------------------------------------------------------------- #
# Mock twin: a rule-based rep conversation, deterministic, no network
# --------------------------------------------------------------------------- #

Rule = Callable[[str], list[ToolCall] | None]


_ids = itertools.count(1)


def _tool(name: str, **kwargs: Any) -> ToolCall:
    return ToolCall(id=f"toolu_mock_{next(_ids)}", name=name, input=kwargs)


def _last_rep_text(messages: list[dict[str, Any]]) -> str | None:
    """The newest rep utterance, or None if the last user turn is only tool results."""
    if not messages or messages[-1]["role"] != "user":
        return None
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return " ".join(texts) if texts else None


class MockLLMClient(LLMClient):
    """Plays the conversation agent with simple rules, or with a fixed script.

    `script` (list of tool-call lists) overrides the rules, one entry per rep turn.
    """

    def __init__(self, script: list[list[ToolCall]] | None = None) -> None:
        self.script = list(script) if script is not None else None
        self.calls: list[dict[str, Any]] = []
        self._reference: str | None = None
        self._confirmed = False

    async def respond(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": [m.copy() for m in messages]})
        rep = _last_rep_text(messages)
        if rep is None:  # tool results came back: end this turn
            return LLMResponse(content=[{"type": "text", "text": "ok"}], text="ok")
        calls = self.script.pop(0) if self.script else self._rules(rep)
        content: list[Any] = [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return LLMResponse(
            content=content or [{"type": "text", "text": "..."}],
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
        )

    def _rules(self, rep: str) -> list[ToolCall]:
        low = rep.lower()
        ref = re.search(
            r"\b(?:reference|confirmation)(?: number)?(?: is)?[:\s]+([A-Z0-9-]{4,})", rep, re.I
        )
        if ref:
            self._reference = ref.group(1).upper()
            return [_tool("say", text=f"Just to confirm, the reference is {self._reference}?")]
        if self._reference and re.search(r"\b(yes|correct|that's right|right)\b", low):
            return [
                _tool("say", text="Thank you so much for your help today."),
                _tool(
                    "end_call", summary="The request was handled.", reference_number=self._reference
                ),
            ]
        if re.search(r"social security|card number|password", low):
            return [_tool("request_takeover", reason="The rep needs high-sensitivity info.")]
        if re.search(r"\b(pin|passcode|account number|date of birth|zip code)\b", low):
            what = re.search(
                r"\b(pin|passcode|account number|date of birth|zip code)\b", low
            ).group(1)
            return [
                _tool(
                    "ask_user",
                    question=f"The rep is asking for your {what}. What is it?",
                    sensitivity="medium",
                    relay_template=f"Sure, the {what} is {{answer}}.",
                )
            ]
        if re.search(r"upgrade|premium|new plan|protection plan|add-on", low):
            return [_tool("say", text="No thank you, I'd just like help with my request today.")]
        if re.search(r"\bai\b|robot|real person", low) and re.search(r"don't|won't|can't", low):
            return [_tool("request_takeover", reason="The rep will not talk to an AI assistant.")]
        return [_tool("say", text="I'm calling to get help with the request on this account.")]

    async def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        return ""  # callers fall back to deterministic text


def get_llm_client() -> LLMClient:
    return MockLLMClient() if get_settings().use_mocks else AnthropicLLMClient()
