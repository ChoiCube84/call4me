"""Application Insights telemetry (logs, traces, metrics).

`setup_telemetry(app)` turns on Azure Monitor OpenTelemetry when
APPLICATIONINSIGHTS_CONNECTION_STRING is set and is a no-op otherwise, so dev
and tests never need the `azure` extra. `record_metric()` is always safe to
call; without telemetry configured it does nothing.

Privacy (CLAUDE.md rule 2): every log record leaving the process goes through
`RedactingFilter`, which runs the message, args, exception text and extra
attributes through `backend.audit.log.redact`. The filter is attached to the
root handlers (including the Azure Monitor exporter handler), so it applies to
records from every logger, not just ones logged directly on root.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.audit.log import redact
from backend.config import get_settings

log = logging.getLogger(__name__)

# Metric names used across the app (CLAUDE.md section 14).
CALL_COMPLETED = "call4me.call.completed"  # counter; attrs: success=bool, outcome=...
MINUTES_SAVED = "call4me.call.minutes_saved"  # histogram (minutes)
GUARD_DENIALS = "call4me.guard.denials"  # counter; attrs: reason=...
TIME_TO_GREET_MS = "call4me.time_to_greet_ms"  # histogram (ms after human detected)
ASK_USER_COUNT = "call4me.ask_user.count"  # counter

# Everything else defaults to a histogram.
_COUNTERS = frozenset({CALL_COMPLETED, GUARD_DENIALS, ASK_USER_COUNT})
_UNITS = {MINUTES_SAVED: "min", TIME_TO_GREET_MS: "ms"}

# LogRecord attributes that aren't user-supplied extras.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}

_FILTERED_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")

_enabled = False
_meter: Any = None
_instruments: dict[str, Any] = {}
_filter: RedactingFilter | None = None


class RedactingFilter(logging.Filter):
    """Redact a record in place before any handler formats or exports it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format args: fall back to the raw msg
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        for key, value in list(vars(record).items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str | dict | list | tuple):
                setattr(record, key, redact(value))
        return True


def install_redaction_filter(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach the redaction filter to every handler on `logger` (default: root)."""
    global _filter
    if _filter is None:
        _filter = RedactingFilter()
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if _filter not in handler.filters:
            handler.addFilter(_filter)
    return _filter


def _install_everywhere() -> None:
    # uvicorn's loggers don't propagate to root, so cover their handlers too.
    for name in _FILTERED_LOGGERS:
        install_redaction_filter(logging.getLogger(name))


def _configure_azure_monitor(connection_string: str) -> None:
    # Imported lazily: only installed with the `azure` extra.
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=connection_string,
        # We instrument our app instance explicitly below (it already exists).
        instrumentation_options={"fastapi": {"enabled": False}},
    )


def _instrument_app(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        log.warning("opentelemetry-instrumentation-fastapi missing; request traces off")
        return
    FastAPIInstrumentor.instrument_app(app)


def _use_meter(meter: Any) -> None:
    """Point record_metric at a meter (setup and tests)."""
    global _meter, _enabled
    _meter = meter
    _instruments.clear()
    _enabled = meter is not None


def setup_telemetry(app: Any) -> bool:
    """Enable Application Insights if configured. Returns True when enabled."""
    if _enabled:
        return True
    conn = get_settings().applicationinsights_connection_string
    if not conn:
        _install_everywhere()  # console logs get redacted too
        log.info("APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry disabled")
        return False
    try:
        _configure_azure_monitor(conn)
        _instrument_app(app)
        from opentelemetry import metrics

        _use_meter(metrics.get_meter("call4me"))
    except Exception as exc:  # noqa: BLE001 - telemetry must never stop the app booting
        log.warning("telemetry setup failed: %s", type(exc).__name__)
        return False
    finally:
        # Azure Monitor adds its exporter handler to root; filter it (and any others).
        _install_everywhere()
    log.info("Application Insights telemetry enabled")
    return True


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in redact(attrs).items():
        if isinstance(value, bool | int | float | str):
            out[key] = value
        elif value is not None:
            out[key] = str(value)
    return out


def record_metric(name: str, value: float, **attrs: Any) -> None:
    """Record one metric value. Counters add `value`; others go to a histogram."""
    if not _enabled or _meter is None:
        return
    try:
        inst = _instruments.get(name)
        if inst is None:
            unit = _UNITS.get(name, "1")
            if name in _COUNTERS:
                inst = _meter.create_counter(name, unit=unit)
            else:
                inst = _meter.create_histogram(name, unit=unit)
            _instruments[name] = inst
        clean = _clean_attrs(attrs)
        if name in _COUNTERS:
            inst.add(value, attributes=clean)
        else:
            inst.record(value, attributes=clean)
    except Exception as exc:  # noqa: BLE001 - metrics are best effort
        log.warning("record_metric %s failed: %s", name, type(exc).__name__)
