"""Loads the real product components for evals and tests, when they exist.

Other areas are built in parallel; until their code is merged these loaders
raise `ComponentMissing` and callers fall back (evals) or skip (tests). Every
assumption about another area's interface lives in this one file so it is easy
to check at merge time:

- conversation-agent: `backend.agent.guard.check(action, session) -> GuardResult`
  (sync or async).
- the system under test as a whole: a factory `make_eval_driver(session) -> CallDriver`
  (see `evals.driver.CallDriver`). Looked up from `$CALL4ME_EVAL_DRIVER`
  ("module:function"), else `backend.agent.conversation:make_eval_driver`.
"""

from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Literal

from backend.contracts import AgentAction, CallSession, GuardResult
from evals import reference_guard
from evals.driver import BaselineDriver, CallDriver

GuardFn = Callable[[AgentAction, CallSession], GuardResult | Awaitable[GuardResult]]
DriverFactory = Callable[[CallSession], CallDriver]

DEFAULT_DRIVER_PATH = "backend.agent.conversation:make_eval_driver"


class ComponentMissing(RuntimeError):
    """A product component isn't merged/implemented yet."""


def _load_attr(path: str) -> object:
    module_name, _, attr = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ComponentMissing(f"{module_name} not importable: {exc}") from exc
    obj = getattr(module, attr, None)
    if obj is None:
        raise ComponentMissing(f"{path} not implemented yet")
    return obj


def real_guard() -> GuardFn:
    fn = _load_attr("backend.agent.guard:check")
    if not callable(fn):
        raise ComponentMissing("backend.agent.guard.check is not callable")
    return fn  # type: ignore[return-value]


def resolve_guard(
    mode: Literal["auto", "real", "reference", "none"] = "auto",
) -> tuple[GuardFn | None, str]:
    """Pick the guard the harness applies to every action. Returns (fn, label)."""
    if mode == "none":
        return None, "none"
    if mode == "reference":
        return reference_guard.check, "reference (evals stand-in)"
    try:
        return real_guard(), "backend.agent.guard.check"
    except ComponentMissing:
        if mode == "real":
            raise
        return reference_guard.check, "reference (evals stand-in; real guard not merged)"


def _accepts_runtime(fn: GuardFn) -> bool:
    try:
        return len(inspect.signature(fn).parameters) >= 3
    except (TypeError, ValueError):
        return False


async def apply_guard(
    fn: GuardFn, action: AgentAction, session: CallSession, runtime: object | None = None
) -> GuardResult:
    # The real guard reads per-call runtime (one-shot relay, what was said); pass it when supported.
    result = (
        fn(action, session, runtime)
        if runtime is not None and _accepts_runtime(fn)
        else fn(action, session)
    )  # type: ignore[call-arg]
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, GuardResult):
        raise TypeError(f"guard returned {type(result).__name__}, expected GuardResult")
    return result


def product_driver_factory(path: str | None = None) -> DriverFactory:
    path = path or os.environ.get("CALL4ME_EVAL_DRIVER") or DEFAULT_DRIVER_PATH
    factory = _load_attr(path)
    if not callable(factory):
        raise ComponentMissing(f"{path} is not callable")
    return factory  # type: ignore[return-value]


def resolve_driver(spec: str = "baseline") -> tuple[DriverFactory, str]:
    """`baseline`, `product` (default product path), or an explicit "module:function"."""
    if spec == "baseline":
        return (lambda _session: BaselineDriver()), "baseline (reference policy, not the product)"
    path = None if spec == "product" else spec
    factory = product_driver_factory(path)
    return factory, path or os.environ.get("CALL4ME_EVAL_DRIVER") or DEFAULT_DRIVER_PATH
