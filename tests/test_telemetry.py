"""Telemetry: no-op without config, redaction on every exported log record."""

from __future__ import annotations

import logging

import pytest

from backend import telemetry
from backend.audit import log as audit
from backend.config import Settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry, "_meter", None)
    monkeypatch.setattr(telemetry, "_instruments", {})


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def root_handler():
    handler = ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.INFO)
    yield handler
    root.removeHandler(handler)
    root.setLevel(old)


def test_filter_redacts_message_args_extras_and_child_loggers(root_handler):
    audit.register_secret("s1", "4821")
    telemetry.install_redaction_filter()
    child = logging.getLogger("backend.some.module")
    child.info(
        "user said %s, card %s",
        "PIN 4821",
        "4111 1111 1111 1111",
        extra={"detail": {"pin": "4821"}, "note": "it was 4821"},
    )
    line = root_handler.lines[-1]
    assert "4821" not in line and "4111" not in line
    rec = root_handler.records[-1]
    assert rec.detail == {"pin": audit.REDACTED}
    assert "4821" not in rec.note


def test_filter_redacts_exception_text(root_handler):
    audit.register_secret("s1", "998877")
    telemetry.install_redaction_filter()
    try:
        raise ValueError("bad value 998877")
    except ValueError:
        logging.getLogger("x").exception("failed")
    assert "998877" not in root_handler.lines[-1]
    assert "ValueError" in root_handler.lines[-1]


def test_setup_is_noop_without_connection_string(monkeypatch):
    monkeypatch.setattr(telemetry, "get_settings", lambda: Settings(_env_file=None))
    called = []
    monkeypatch.setattr(telemetry, "_configure_azure_monitor", lambda c: called.append(c))
    assert telemetry.setup_telemetry(object()) is False
    assert called == []
    telemetry.record_metric(telemetry.GUARD_DENIALS, 1, reason="fee")  # no error


def test_setup_with_connection_string(monkeypatch, root_handler):
    pytest.importorskip("opentelemetry.metrics")
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        telemetry,
        "get_settings",
        lambda: Settings(_env_file=None, applicationinsights_connection_string=conn),
    )
    called, instrumented = [], []
    monkeypatch.setattr(telemetry, "_configure_azure_monitor", lambda c: called.append(c))
    monkeypatch.setattr(telemetry, "_instrument_app", lambda app: instrumented.append(app))
    app = object()
    assert telemetry.setup_telemetry(app) is True
    assert called == [conn] and instrumented == [app]
    assert any(isinstance(f, telemetry.RedactingFilter) for f in root_handler.filters)


def test_setup_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "get_settings",
        lambda: Settings(_env_file=None, applicationinsights_connection_string="x"),
    )

    def boom(_):
        raise RuntimeError("no network")

    monkeypatch.setattr(telemetry, "_configure_azure_monitor", boom)
    assert telemetry.setup_telemetry(object()) is False


def test_record_metric_counters_and_histograms():
    sdk = pytest.importorskip("opentelemetry.sdk.metrics")
    export = pytest.importorskip("opentelemetry.sdk.metrics.export")
    reader = export.InMemoryMetricReader()
    provider = sdk.MeterProvider(metric_readers=[reader])
    telemetry._use_meter(provider.get_meter("test"))

    telemetry.record_metric(telemetry.CALL_COMPLETED, 1, success=True, outcome="fee waived")
    telemetry.record_metric(telemetry.GUARD_DENIALS, 1, reason="paid upgrade")
    telemetry.record_metric(telemetry.MINUTES_SAVED, 34.0)
    telemetry.record_metric(telemetry.TIME_TO_GREET_MS, 1200, pin="4821")

    data = reader.get_metrics_data()
    metrics = {
        m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
    }
    assert metrics[telemetry.CALL_COMPLETED].data.data_points[0].value == 1
    assert metrics[telemetry.MINUTES_SAVED].data.data_points[0].sum == 34.0
    greet = metrics[telemetry.TIME_TO_GREET_MS].data.data_points[0]
    assert greet.attributes["pin"] == audit.REDACTED
