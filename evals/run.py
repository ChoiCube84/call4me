"""Run the eval suite: `uv run python -m evals.run`

Options:
  --driver baseline|product|module:function   system under test (default: baseline)
  --guard auto|real|reference|none            guard applied by the harness (default: auto)
  --only id[,id...]                           run a subset
  --out PATH                                  results JSON (default: evals/results/latest.json)

The baseline driver is a rule-based reference policy for testing the harness;
its numbers are not product numbers, and the report says so. Times are
simulated call time (virtual clock), except time-to-greet, which is measured
wall-clock processing of the driver + guard (excludes STT/TTS network time).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from evals.product import ComponentMissing, resolve_driver, resolve_guard  # noqa: E402
from evals.scenario import Scenario  # noqa: E402
from evals.scenarios import BY_ID, SCENARIOS  # noqa: E402
from evals.scoring import (  # noqa: E402
    ScenarioResult,
    SuiteMetrics,
    aggregate,
    find_leaks,
    score,
    to_jsonable,
)
from evals.sim import SimulatedCall, make_session  # noqa: E402

DEFAULT_OUT = Path(__file__).parent / "results" / "latest.json"


async def run_suite(
    scenarios: tuple[Scenario, ...] | list[Scenario] = SCENARIOS,
    *,
    driver: str = "baseline",
    guard: str = "auto",
) -> tuple[list[ScenarioResult], SuiteMetrics, dict]:
    factory, driver_label = resolve_driver(driver)
    guard_fn, guard_label = resolve_guard(guard)  # type: ignore[arg-type]
    results = []
    for sc in scenarios:
        session = make_session(sc)
        sim = SimulatedCall(
            sc, factory(session), guard=guard_fn, guard_label=guard_label, session=session
        )
        results.append(score(sc, await sim.run()))
    meta = {"driver": driver_label, "guard": guard_label, "clock": "simulated"}
    return results, aggregate(results), meta


def _print(results: list[ScenarioResult], m: SuiteMetrics, meta: dict) -> None:
    print(f"driver: {meta['driver']}\nguard:  {meta['guard']}\n")
    print(
        f"{'scenario':<24} {'result':<6} {'saved':>6} {'asks':>4} {'deny':>4} {'err':>3} "
        f"{'greet ms':>9}  notes"
    )
    for r in results:
        ttg = f"{r.time_to_greet_ms:.2f}" if r.time_to_greet_ms is not None else "-"
        notes = "; ".join(r.failures + r.violations + r.errors)
        print(
            f"{r.id:<24} {'PASS' if r.passed else 'FAIL':<6} {r.minutes_saved:>6.1f} "
            f"{r.asks:>4} {len(r.guard_denials):>4} {len(r.errors):>3} {ttg:>9}  {notes}"
        )
    ttg_med = m.time_to_greet_ms_median
    print(
        f"\nsuccess rate        {m.passed}/{m.scenarios} ({m.success_rate:.0%})"
        f"\nminutes saved       {m.minutes_saved_total} total, "
        f"{m.minutes_saved_avg} avg per success (simulated)"
        f"\ntimes asked user    {m.times_asked_user}"
        f"\nerrors              {m.errors}"
        f"\nsafety violations   {m.safety_violations}"
        f"\nguard denials       {m.guard_denials}"
        f"\ntime-to-greet       median {ttg_med} ms, max {m.time_to_greet_ms_max} ms"
        f" (driver+guard processing only)"
    )


def main(argv: list[str] | None = None) -> int:
    # Only when run as a CLI: importing this module (tests) must not change the env.
    os.environ.setdefault("USE_MOCKS", "true")
    p = argparse.ArgumentParser(prog="evals.run")
    p.add_argument("--driver", default="baseline")
    p.add_argument("--guard", default="auto", choices=["auto", "real", "reference", "none"])
    p.add_argument("--only", default="")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)

    scenarios = [BY_ID[s] for s in args.only.split(",") if s] if args.only else list(SCENARIOS)
    try:
        results, metrics, meta = asyncio.run(
            run_suite(scenarios, driver=args.driver, guard=args.guard)
        )
    except ComponentMissing as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    report = to_jsonable(results, metrics, **meta)
    blob = json.dumps(report, default=str)
    # Last line of defense: never write a private value to disk.
    leaks = sorted({f"{sc.id}:{k}" for sc in scenarios for k in find_leaks(blob, sc)})
    if leaks:
        print(f"refusing to write results: private values found {leaks}", file=sys.stderr)
        return 3
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str))
    _print(results, metrics, meta)
    print(f"\nresults: {args.out}")
    return 0 if metrics.passed == metrics.scenarios else 1


if __name__ == "__main__":
    raise SystemExit(main())
