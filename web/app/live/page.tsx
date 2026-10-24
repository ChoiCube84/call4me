"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AskSheet } from "@/components/AskSheet";
import { CaptionList } from "@/components/CaptionList";
import { Flash } from "@/components/Flash";
import { StateBanner } from "@/components/StateBanner";
import { TakeoverComposer } from "@/components/TakeoverComposer";
import { TERMINAL_STATES, api } from "@/lib/api";
import { useWakeLock } from "@/lib/hooks";
import { useLiveCall } from "@/lib/live";
import { CAPTION_SIZES, loadCaptionSize, saveCaptionSize, stashEnded } from "@/lib/prefs";

export default function LivePage() {
  return (
    <Suspense fallback={<StateBanner state={null} holdSince={null} reconnecting={false} />}>
      <LiveScreen />
    </Suspense>
  );
}

function LiveScreen() {
  const id = useSearchParams().get("id");
  const router = useRouter();
  const { view, reconnecting, dismissAsk } = useLiveCall(id);
  const [typing, setTyping] = useState(false); // we asked to take over; TAKEOVER state follows
  const [confirmHangup, setConfirmHangup] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [size, setSize] = useState(24);
  const finished = Boolean(view.ended) || (view.state !== null && TERMINAL_STATES.has(view.state));
  const wake = useWakeLock(Boolean(id) && !finished);
  const takeoverOn = !finished && (typing || view.state === "TAKEOVER");

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the saved size once on mount
    setSize(loadCaptionSize());
  }, []);
  useEffect(() => {
    document.documentElement.style.setProperty("--caption-size", `${size}px`);
  }, [size]);

  // The call is over: go to the summary (keep the data for an instant render there).
  useEffect(() => {
    if (!id || !view.ended) return;
    stashEnded(id, view.ended);
    const t = setTimeout(() => router.push(`/summary/?id=${encodeURIComponent(id)}`), 1500);
    return () => clearTimeout(t);
  }, [id, view.ended, router]);

  // Hang-up confirmation times out so a stray tap doesn't stay armed.
  useEffect(() => {
    if (!confirmHangup) return;
    const t = setTimeout(() => setConfirmHangup(false), 4000);
    return () => clearTimeout(t);
  }, [confirmHangup]);

  if (!id) {
    return (
      <main className="safe-x safe-top flex min-h-dvh flex-col items-start justify-center gap-4">
        <p className="text-2xl font-bold">No call to show.</p>
        <Link href="/" className="min-h-14 rounded-2xl bg-accent px-6 py-3 text-xl font-bold text-black">
          Start a call
        </Link>
      </main>
    );
  }

  const act = async (fn: () => Promise<unknown>) => {
    setActionError(null);
    try {
      await fn();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "That didn't work. Try again.");
    }
  };

  const takeOver = () => {
    setTyping(true);
    if (view.ask) dismissAsk(view.ask.requestId);
    void act(() => api.takeover(id));
  };
  const handBack = () => {
    setTyping(false);
    void act(() => api.handback(id));
  };
  const resize = (dir: 1 | -1) => {
    const i = CAPTION_SIZES.indexOf(size);
    const next = CAPTION_SIZES[Math.min(CAPTION_SIZES.length - 1, Math.max(0, (i < 0 ? 1 : i) + dir))];
    setSize(next);
    saveCaptionSize(next);
  };

  const flashKey = view.ask
    ? `ask:${view.ask.requestId}`
    : view.takeoverSuggested
      ? `takeover:${view.takeoverSuggested}`
      : view.holdPrompt !== null
        ? `hold:${view.holdPrompt}`
        : null;

  return (
    <main className={`min-h-dvh ${takeoverOn ? "pb-72" : "pb-40"}`}>
      <StateBanner state={view.state} holdSince={view.holdSince} reconnecting={reconnecting} />
      <Flash alertKey={flashKey} />

      <div className="safe-x mx-auto flex max-w-2xl flex-col gap-4 pt-4">
        <div className="flex items-center justify-between gap-2">
          <Link href="/" className="min-h-12 py-3 text-base font-semibold text-muted underline">
            ← New call
          </Link>
          <div className="flex items-center gap-2" aria-label="Caption size">
            <button
              type="button"
              onClick={() => resize(-1)}
              aria-label="Smaller captions"
              className="min-h-12 min-w-12 rounded-xl border-2 border-line text-lg font-bold"
            >
              A−
            </button>
            <button
              type="button"
              onClick={() => resize(1)}
              aria-label="Bigger captions"
              className="min-h-12 min-w-12 rounded-xl border-2 border-line text-2xl font-bold"
            >
              A+
            </button>
          </div>
        </div>

        {view.summary && (
          <section className="rounded-2xl border-2 border-info bg-panel p-4" aria-live="polite">
            <h2 className="text-sm font-bold uppercase tracking-wide text-info">What&apos;s happening</h2>
            <p className="mt-1 text-xl">{view.summary}</p>
          </section>
        )}

        {view.takeoverSuggested && !takeoverOn && (
          <section role="alert" className="pulse-ring rounded-2xl border-4 border-accent bg-panel p-4">
            <h2 className="text-2xl font-extrabold text-accent">Please take over</h2>
            <p className="mt-1 text-xl">{view.takeoverSuggested}</p>
            <button
              type="button"
              onClick={takeOver}
              className="mt-3 min-h-14 w-full rounded-xl bg-accent text-xl font-extrabold text-black"
            >
              Take over and type
            </button>
          </section>
        )}

        {view.holdPrompt !== null && (
          <section role="alert" className="rounded-2xl border-4 border-warn bg-panel p-4">
            <h2 className="text-2xl font-extrabold text-warn">Still on hold</h2>
            <p className="mt-1 text-xl">
              On hold {view.holdPrompt} minutes. Keep waiting?
            </p>
            <div className="mt-3 flex gap-3">
              <button
                type="button"
                onClick={() => void act(() => api.keepHolding(id, true))}
                className="min-h-14 flex-1 rounded-xl bg-warn text-xl font-extrabold text-black"
              >
                Keep waiting
              </button>
              <button
                type="button"
                onClick={() => void act(() => api.keepHolding(id, false))}
                className="min-h-14 flex-1 rounded-xl border-2 border-line text-xl font-bold"
              >
                Stop
              </button>
            </div>
          </section>
        )}

        {view.denials.length > 0 && (
          <ul className="flex flex-wrap gap-2" aria-label="Blocked by the safety guard">
            {view.denials.slice(-3).map((d, i) => (
              <li key={`${d.at.getTime()}-${i}`} className="rounded-full border border-bad px-3 py-1 text-sm text-bad">
                Blocked: {d.reason}
              </li>
            ))}
          </ul>
        )}

        <CaptionList
          captions={view.captions}
          partials={view.partials}
          bottomInset={finished ? 0 : takeoverOn ? 270 : 110}
        />

        {finished && (
          <p className="rounded-2xl bg-panel-2 p-4 text-xl font-semibold" role="status">
            The call is over. Opening the summary…
          </p>
        )}
        {wake === "unsupported" && !finished && (
          <p className="text-sm text-muted">Tip: keep your screen on during the call.</p>
        )}
      </div>

      {actionError && (
        <p role="alert" className="safe-x fixed inset-x-0 top-24 z-40 bg-bad py-2 text-center text-lg font-bold text-black">
          {actionError}
        </p>
      )}

      {takeoverOn ? (
        <TakeoverComposer callId={id} onHandback={handBack} />
      ) : (
        !finished && (
          <nav className="safe-x safe-bottom fixed inset-x-0 bottom-0 z-30 flex gap-3 border-t border-line bg-black/95 pt-3">
            <button
              type="button"
              onClick={takeOver}
              className="min-h-16 flex-1 rounded-2xl bg-warn text-xl font-extrabold text-black"
            >
              Take over
            </button>
            <button
              type="button"
              onClick={() => {
                if (confirmHangup) {
                  setConfirmHangup(false);
                  void act(() => api.cancel(id));
                } else setConfirmHangup(true);
              }}
              className={`min-h-16 flex-1 rounded-2xl text-xl font-extrabold ${
                confirmHangup ? "bg-bad text-black" : "border-2 border-bad text-bad"
              }`}
            >
              {confirmHangup ? "Tap again to hang up" : "Hang up"}
            </button>
          </nav>
        )
      )}

      {view.ask && !takeoverOn && (
        <AskSheet ask={view.ask} onDone={dismissAsk} onSayItMyself={takeOver} />
      )}
    </main>
  );
}
