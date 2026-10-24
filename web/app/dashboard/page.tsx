"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { CaptionList } from "@/components/CaptionList";
import { StateBanner } from "@/components/StateBanner";
import { TopBar } from "@/components/TopBar";
import {
  type AuditRow,
  type CallSession,
  type EvalReport,
  TERMINAL_STATES,
  type TimelineEvent,
  ApiError,
  api,
  parseTime,
} from "@/lib/api";
import { useLiveCall } from "@/lib/live";

export default function DashboardPage() {
  return (
    <Suspense fallback={<TopBar title="call4me · judges" />}>
      <Dashboard />
    </Suspense>
  );
}

/** Poll `fn` every `ms` while `active`; the latest result (or error) is returned. */
function usePoll<T>(fn: (() => Promise<T>) | null, ms: number, active = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!fn) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clear when the source goes away
      setData(null);
      return;
    }
    let alive = true;
    const run = () =>
      fn()
        .then((d) => {
          if (!alive) return;
          setData(d);
          setError(null);
        })
        .catch((err: unknown) => {
          if (alive) setError(err instanceof ApiError || err instanceof Error ? err.message : "Error");
        });
    void run();
    if (!active) return () => void (alive = false);
    const t = setInterval(run, ms);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [fn, ms, active]);
  return { data, error };
}

const time = (v: string | null | undefined) =>
  parseTime(v)?.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) ?? "";

function Dashboard() {
  const router = useRouter();
  const selected = useSearchParams().get("id");
  const select = (id: string) => router.replace(`/dashboard/?id=${encodeURIComponent(id)}`);

  const listCalls = useCallback(() => api.listCalls(20), []);
  const metricsFn = useCallback(() => api.metrics(), []);
  const calls = usePoll<CallSession[]>(listCalls, 5000);
  const metrics = usePoll<EvalReport>(metricsFn, 60000);

  return (
    <main className="min-h-dvh">
      <TopBar title="call4me · judges" />
      <div className="safe-x grid gap-6 py-5 lg:grid-cols-[minmax(320px,420px)_1fr]">
        <aside className="flex flex-col gap-5">
          <section className="rounded-2xl border border-line bg-panel p-4">
            <h1 className="text-xl font-extrabold">What call4me is</h1>
            <p className="mt-1 text-base text-muted">
              An AI that makes customer service phone calls for Deaf and hard-of-hearing people. It navigates
              the menu, waits on hold, and talks to the rep. The user reads live captions and types only when
              needed. A guard checks every action; private answers are spoken once and redacted.
            </p>
          </section>
          <MetricsCard report={metrics.data} error={metrics.error} />
          <section className="rounded-2xl border border-line bg-panel p-4">
            <h2 className="text-lg font-bold">Recent calls</h2>
            {calls.error && <p className="mt-2 text-bad">{calls.error}</p>}
            <ul className="mt-3 flex flex-col gap-2">
              {(calls.data ?? []).map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    onClick={() => select(c.id)}
                    className={`w-full rounded-xl border-2 px-3 py-2 text-left ${
                      c.id === selected ? "border-accent bg-panel-2" : "border-line hover:bg-panel-2"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-bold">{c.company}</span>
                      <StateChip state={c.state} />
                    </div>
                    <div className="truncate text-sm text-muted">{c.goal}</div>
                    <div className="text-xs text-muted">{time(c.started_at)}</div>
                  </button>
                </li>
              ))}
              {calls.data && calls.data.length === 0 && <li className="text-muted">No calls yet.</li>}
            </ul>
          </section>
        </aside>

        <section className="min-w-0">
          {selected ? (
            <CallPanel key={selected} id={selected} />
          ) : (
            <div className="rounded-2xl border-2 border-dashed border-line p-8 text-xl text-muted">
              Pick a call on the left to watch it live, with its timeline and audit log.
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function StateChip({ state }: { state: string }) {
  const terminal = TERMINAL_STATES.has(state as CallSession["state"]);
  const tone =
    state === "FAILED" ? "bg-bad text-black" : terminal ? "bg-panel-2 text-muted" : "bg-ok text-black";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${tone}`}>{state}</span>;
}

function MetricsCard({ report, error }: { report: EvalReport | null; error: string | null }) {
  if (!report) {
    return (
      <section className="rounded-2xl border border-line bg-panel p-4">
        <h2 className="text-lg font-bold">Eval results</h2>
        <p className="mt-2 text-muted">{error ?? "Loading…"}</p>
      </section>
    );
  }
  const m = report.metrics;
  const tiles: [string, string][] = [
    ["Success", `${m.passed}/${m.scenarios} (${Math.round((m.success_rate ?? 0) * 100)}%)`],
    ["Minutes saved", `${m.minutes_saved_total} (${m.minutes_saved_avg} avg)`],
    ["Asked the user", String(m.times_asked_user)],
    ["Safety violations", String(m.safety_violations)],
    ["Guard denials", String(m.guard_denials)],
    [
      "Time to greet (median)",
      typeof m.time_to_greet_ms_median === "number" ? `${m.time_to_greet_ms_median.toFixed(0)} ms` : "–",
    ],
  ];
  return (
    <section className="rounded-2xl border border-line bg-panel p-4">
      <h2 className="text-lg font-bold">Eval results ({m.scenarios} scripted calls)</h2>
      <dl className="mt-3 grid grid-cols-2 gap-2">
        {tiles.map(([k, v]) => (
          <div key={k} className="rounded-xl bg-black p-3">
            <dt className="text-xs font-bold uppercase tracking-wide text-muted">{k}</dt>
            <dd
              className={`mt-1 text-lg font-extrabold ${
                k === "Safety violations" ? (m.safety_violations === 0 ? "text-ok" : "text-bad") : ""
              }`}
            >
              {v}
            </dd>
          </div>
        ))}
      </dl>
      <table className="mt-4 w-full text-left text-sm">
        <thead className="text-muted">
          <tr>
            <th className="py-1 font-semibold">Scenario</th>
            <th className="py-1 font-semibold">Result</th>
            <th className="py-1 text-right font-semibold">Saved</th>
          </tr>
        </thead>
        <tbody>
          {report.results.map((r) => (
            <tr key={r.id} className="border-t border-line">
              <td className="py-1.5 pr-2">{r.title || r.id}</td>
              <td className={`py-1.5 font-bold ${r.passed ? "text-ok" : "text-bad"}`}>{r.passed ? "PASS" : "FAIL"}</td>
              <td className="py-1.5 text-right">{r.minutes_saved} min</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function CallPanel({ id }: { id: string }) {
  const { view, reconnecting } = useLiveCall(id);
  const live = !view.ended && !(view.state && TERMINAL_STATES.has(view.state));
  const detailFn = useCallback(() => api.getCall(id), [id]);
  const auditFn = useCallback(() => api.audit(id), [id]);
  const detail = usePoll(detailFn, 3000, live);
  const audit = usePoll<AuditRow[]>(auditFn, 3000, live);
  const session = detail.data?.session;

  return (
    <div className="flex flex-col gap-4">
      <div className="overflow-hidden rounded-2xl border border-line">
        <StateBanner state={view.state ?? session?.state ?? null} holdSince={view.holdSince} reconnecting={reconnecting} />
        <div className="bg-panel p-4">
          <h2 className="text-2xl font-extrabold">{session?.company ?? "Call"}</h2>
          <p className="text-lg text-muted">{session?.goal}</p>
          {session?.reference_number && (
            <p className="mt-2 text-lg">
              Reference: <span className="font-mono font-bold text-accent">{session.reference_number}</span>
            </p>
          )}
          {view.summary && <p className="mt-3 rounded-xl border-2 border-info p-3 text-lg">{view.summary}</p>}
          {view.ask && (
            <p className="mt-3 rounded-xl border-2 border-accent p-3 text-lg">
              <span className="font-bold text-accent">Asking the user ({view.ask.sensitivity}): </span>
              {view.ask.question}
            </p>
          )}
          {view.takeoverSuggested && (
            <p className="mt-3 rounded-xl border-2 border-warn p-3 text-lg text-warn">
              Takeover suggested: {view.takeoverSuggested}
            </p>
          )}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="flex h-[60vh] flex-col rounded-2xl border border-line bg-black p-3">
          <h3 className="mb-2 text-lg font-bold">Live captions</h3>
          <div className="min-h-0 flex-1">
            <CaptionList captions={view.captions} partials={view.partials} scrollParent="self" emptyText="No captions yet." />
          </div>
        </section>
        <Timeline events={detail.data?.timeline ?? []} />
      </div>

      <AuditLog rows={audit.data ?? []} error={audit.error} />
    </div>
  );
}

function describe(e: TimelineEvent): string {
  if (e.kind === "transition") return `${e.from_state ?? "?"} → ${e.to_state ?? "?"}${e.detail.reason ? ` · ${e.detail.reason}` : ""}`;
  const d = { ...e.detail };
  delete d.at;
  const text = Object.entries(d)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
  return text.length > 220 ? text.slice(0, 217) + "…" : text;
}

function Timeline({ events }: { events: TimelineEvent[] }) {
  // Newest first: judges watching a live call see what just happened without scrolling.
  const shown = events.filter((e) => e.kind !== "caption").slice(-150).reverse();
  return (
    <section className="flex h-[60vh] flex-col rounded-2xl border border-line bg-panel p-3">
      <h3 className="mb-2 text-lg font-bold">
        Timeline <span className="text-base font-normal text-muted">(newest first)</span>
      </h3>
      <ol className="min-h-0 flex-1 overflow-y-auto text-sm">
        {shown.map((e, i) => (
          <li
            key={`${e.at}-${i}`}
            className={`border-t border-line py-1.5 ${e.kind === "guard_denied" ? "text-bad" : ""}`}
          >
            <span className="font-mono text-muted">{time(e.at)}</span>{" "}
            <span className="font-bold">{e.kind}</span>
            {typeof e.confidence === "number" && <span className="text-muted"> ({e.confidence.toFixed(2)})</span>}
            <div className="break-words text-muted">{describe(e)}</div>
          </li>
        ))}
        {shown.length === 0 && <li className="text-muted">No events yet.</li>}
      </ol>
    </section>
  );
}

function AuditLog({ rows, error }: { rows: AuditRow[]; error: string | null }) {
  const denials = rows.filter((r) => !r.allowed).length;
  return (
    <section className="rounded-2xl border border-line bg-panel p-3">
      <h3 className="mb-1 text-lg font-bold">
        Audit log <span className="text-base font-normal text-muted">(append-only · newest first · {rows.length} entries · {denials} denied)</span>
      </h3>
      {error && <p className="text-bad">{error}</p>}
      <div className="max-h-[50vh] overflow-auto">
        <table className="w-full text-left text-sm">
          <thead className="sticky top-0 bg-panel text-muted">
            <tr>
              <th className="py-1 pr-2 font-semibold">Time</th>
              <th className="py-1 pr-2 font-semibold">Actor</th>
              <th className="py-1 pr-2 font-semibold">Action</th>
              <th className="py-1 font-semibold">Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(-300).reverse().map((r, i) => (
              <tr key={`${r.at}-${i}`} className={`border-t border-line ${r.allowed ? "" : "bg-[#3a0d0d] text-bad"}`}>
                <td className="py-1 pr-2 font-mono whitespace-nowrap">{time(r.at)}</td>
                <td className="py-1 pr-2">{r.actor}</td>
                <td className="py-1 pr-2 font-semibold">
                  {r.allowed ? "" : "DENIED · "}
                  {r.action}
                </td>
                <td className="py-1 break-words">{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
