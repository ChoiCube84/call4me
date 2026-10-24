"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { TopBar } from "@/components/TopBar";
import { type CallSession, api, parseTime } from "@/lib/api";
import type { EndedInfo } from "@/lib/live";
import { readEnded } from "@/lib/prefs";

export default function SummaryPage() {
  return (
    <Suspense fallback={<TopBar />}>
      <SummaryScreen />
    </Suspense>
  );
}

function SummaryScreen() {
  const id = useSearchParams().get("id");
  const [ended, setEnded] = useState<EndedInfo | null>(null);
  const [session, setSession] = useState<CallSession | null>(null);
  const [savedSummary, setSavedSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!id) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- instant render from the live screen's data
    setEnded(readEnded<EndedInfo>(id));
    let alive = true;
    api
      .getCall(id)
      .then((d) => {
        if (!alive) return;
        setSession(d.session);
        // The plain-language summary is kept on the call's "call.ended" timeline event.
        const end = [...d.timeline].reverse().find((e) => e.kind === "call.ended");
        const text = end?.detail?.summary;
        if (typeof text === "string" && text.trim()) setSavedSummary(text.trim());
      })
      .catch((err) => alive && setError(err instanceof Error ? err.message : "Could not load the call."));
    return () => {
      alive = false;
    };
  }, [id]);

  // The server is the source of truth; the live screen's copy fills in while it loads.
  const reference = session?.reference_number ?? ended?.reference_number ?? null;
  const outcome = session?.outcome ?? ended?.outcome ?? null;
  const minutes = session?.minutes_saved ?? ended?.minutes_saved ?? null;
  const summary = ended?.summary ?? savedSummary;
  const started = parseTime(session?.started_at);
  const failed = (session?.state ?? ended?.state) === "FAILED";

  const copy = async () => {
    if (!reference) return;
    try {
      await navigator.clipboard.writeText(reference);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked: the number is on screen, large */
    }
  };

  return (
    <main className="min-h-dvh pb-10">
      <TopBar />
      <div className="safe-x mx-auto flex max-w-xl flex-col gap-5 pt-6">
        <div>
          <p className="text-base font-bold uppercase tracking-wide text-muted">
            {session?.company ?? "Your call"}
            {started ? ` · ${started.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : ""}
          </p>
          <h1 className={`mt-1 text-4xl font-extrabold ${failed ? "text-bad" : "text-ok"}`}>
            {failed ? "The call didn't go through" : "Call finished"}
          </h1>
        </div>

        {summary && <p className="text-2xl leading-snug">{summary}</p>}
        {outcome && outcome !== summary && (
          <section className="rounded-2xl bg-panel p-4">
            <h2 className="text-sm font-bold uppercase tracking-wide text-muted">Outcome</h2>
            <p className="mt-1 text-xl">{outcome}</p>
          </section>
        )}

        {reference && (
          <section className="rounded-2xl border-4 border-accent bg-panel p-5">
            <h2 className="text-sm font-bold uppercase tracking-wide text-accent">Reference number</h2>
            <p className="mt-2 break-all font-mono text-5xl font-extrabold tracking-wider" aria-live="polite">
              {reference}
            </p>
            <button
              type="button"
              onClick={() => void copy()}
              className="mt-4 min-h-14 w-full rounded-xl bg-accent text-xl font-bold text-black"
            >
              {copied ? "Copied ✓" : "Copy number"}
            </button>
          </section>
        )}

        {minutes !== null && minutes !== undefined && (
          <section className="rounded-2xl bg-panel p-4">
            <h2 className="text-sm font-bold uppercase tracking-wide text-muted">Time saved</h2>
            <p className="mt-1 text-3xl font-extrabold">
              {minutes} {minutes === 1 ? "minute" : "minutes"}
            </p>
            <p className="text-base text-muted">You didn&apos;t have to wait on the phone.</p>
          </section>
        )}

        {session?.goal && (
          <p className="text-lg text-muted">
            <span className="font-bold text-fg">You asked: </span>
            {session.goal}
          </p>
        )}

        {error && !ended && (
          <p role="alert" className="rounded-xl border-2 border-bad p-4 text-lg text-bad">
            {error}
          </p>
        )}
        {!id && <p className="text-xl">No call to show.</p>}

        <Link
          href="/"
          className="mt-2 flex min-h-16 items-center justify-center rounded-2xl bg-accent text-2xl font-extrabold text-black"
        >
          Start another call
        </Link>
        {id && (
          <Link href={`/dashboard/?id=${encodeURIComponent(id)}`} className="text-center text-base text-muted underline">
            See the full call log
          </Link>
        )}
      </div>
    </main>
  );
}
