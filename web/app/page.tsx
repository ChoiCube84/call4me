"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { TopBar } from "@/components/TopBar";
import { type CallLimits, type Company, api } from "@/lib/api";
import {
  DEFAULT_DISCLOSURE,
  DEFAULT_LIMITS,
  type Prefs,
  fillDisclosure,
  loadPrefs,
  savePrefs,
  userId,
} from "@/lib/prefs";

const MAX_INFO = 10;
const field =
  "w-full rounded-xl border-2 border-line bg-panel px-4 py-3 text-xl placeholder:text-[#8a919c] focus:border-accent";
const label = "mb-2 block text-lg font-bold";

interface InfoRow {
  key: number;
  label: string;
}

export default function StartPage() {
  const router = useRouter();
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [goal, setGoal] = useState("");
  const [suggestions, setSuggestions] = useState<Company[]>([]);
  const [showLimits, setShowLimits] = useState(false);
  const [info, setInfo] = useState<InfoRow[]>([]);
  const infoValues = useRef(new Map<number, HTMLInputElement | null>());
  const nextKey = useRef(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // localStorage only exists in the browser, not in the static prerender.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load saved prefs once on mount
    setPrefs(loadPrefs());
  }, []);

  const update = (patch: Partial<Prefs>) => setPrefs((p) => (p ? { ...p, ...patch } : p));
  const updateLimits = (patch: Partial<CallLimits>) =>
    setPrefs((p) => (p ? { ...p, limits: { ...p.limits, ...patch } } : p));

  // Company suggestions from the directory.
  const company = prefs?.company ?? "";
  useEffect(() => {
    const q = company.trim();
    if (q.length < 2) return;
    const t = setTimeout(() => {
      api
        .companies(q)
        .then((list) => setSuggestions(list.filter((c) => c.name !== q).slice(0, 5)))
        .catch(() => setSuggestions([]));
    }, 250);
    return () => clearTimeout(t);
  }, [company]);

  if (!prefs) {
    return (
      <main className="min-h-dvh">
        <TopBar />
      </main>
    );
  }

  const start = async () => {
    setError(null);
    if (!prefs.company.trim() || !prefs.phone.trim() || !goal.trim()) {
      setError("Fill in the company, the phone number, and what you need.");
      return;
    }
    // Read the info values straight from the inputs; they are never kept anywhere.
    const knownInfo = info
      .map((row) => ({ label: row.label.trim(), value: infoValues.current.get(row.key)?.value.trim() || "" }))
      .filter((r) => r.label && r.value);
    setBusy(true);
    try {
      savePrefs(prefs);
      const session = await api.startCall({
        user_id: userId(),
        company: prefs.company.trim(),
        phone_number: prefs.phone.trim(),
        goal: goal.trim(),
        limits: prefs.limits,
        disclosure_text: fillDisclosure(prefs.disclosure, prefs.name),
        known_info: knownInfo,
      });
      infoValues.current.forEach((el) => {
        if (el) el.value = "";
      });
      router.push(`/live/?id=${encodeURIComponent(session.id)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the call.");
      setBusy(false);
    }
  };

  return (
    <main className="min-h-dvh pb-10">
      <TopBar />
      <form
        className="safe-x mx-auto flex max-w-xl flex-col gap-6 pt-6"
        onSubmit={(e) => {
          e.preventDefault();
          void start();
        }}
      >
        <div>
          <h1 className="text-3xl font-extrabold leading-tight">We make the call. You read it.</h1>
          <p className="mt-2 text-lg text-muted">
            call4me calls the company, waits on hold, and talks to the person. It asks you only when it
            needs something only you know.
          </p>
        </div>

        <div className="relative">
          <label htmlFor="company" className={label}>
            Company
          </label>
          <input
            id="company"
            className={field}
            value={prefs.company}
            autoComplete="organization"
            placeholder="e.g. Amazon"
            onChange={(e) => {
              update({ company: e.target.value });
              if (e.target.value.trim().length < 2) setSuggestions([]);
            }}
            onBlur={() => setTimeout(() => setSuggestions([]), 150)}
          />
          {suggestions.length > 0 && (
            <ul className="absolute inset-x-0 top-full z-20 mt-1 overflow-hidden rounded-xl border-2 border-accent bg-panel">
              {suggestions.map((c) => (
                <li key={c.name}>
                  <button
                    type="button"
                    className="min-h-14 w-full px-4 py-2 text-left text-lg hover:bg-panel-2 focus:bg-panel-2"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      update({ company: c.name, phone: c.phone_number });
                      setSuggestions([]);
                    }}
                  >
                    <span className="font-bold">{c.name}</span>
                    <span className="block text-base text-muted">{c.phone_number}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <label htmlFor="phone" className={label}>
            Their phone number
          </label>
          <input
            id="phone"
            className={field}
            type="tel"
            inputMode="tel"
            autoComplete="off"
            placeholder="(415) 555-1234"
            value={prefs.phone}
            onChange={(e) => update({ phone: e.target.value })}
          />
        </div>

        <div>
          <label htmlFor="goal" className={label}>
            What do you need?
          </label>
          <textarea
            id="goal"
            className={`${field} min-h-32`}
            placeholder="e.g. Remove the $40 late fee from my account."
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
        </div>

        <section className="rounded-2xl border-2 border-line p-4">
          <h2 className="text-lg font-bold">Info call4me may share</h2>
          <p className="mt-1 text-base text-muted">
            Order number, email on the account… Used for this call only, never saved. Never add a full
            card number, Social Security number, or password: you type those yourself if needed.
          </p>
          <div className="mt-3 flex flex-col gap-3">
            {info.map((row) => (
              <div key={row.key} className="flex flex-col gap-2 rounded-xl bg-panel p-3">
                <input
                  aria-label="What it is"
                  className={field}
                  placeholder="What it is (e.g. Order number)"
                  value={row.label}
                  maxLength={40}
                  onChange={(e) =>
                    setInfo((rows) => rows.map((r) => (r.key === row.key ? { ...r, label: e.target.value } : r)))
                  }
                />
                <div className="flex gap-2">
                  <input
                    aria-label="Value"
                    className={field}
                    placeholder="Value"
                    autoComplete="off"
                    maxLength={120}
                    ref={(el) => {
                      infoValues.current.set(row.key, el);
                    }}
                  />
                  <button
                    type="button"
                    aria-label="Remove"
                    className="min-h-12 min-w-12 rounded-xl border-2 border-line text-xl"
                    onClick={() => {
                      infoValues.current.delete(row.key);
                      setInfo((rows) => rows.filter((r) => r.key !== row.key));
                    }}
                  >
                    ✕
                  </button>
                </div>
              </div>
            ))}
          </div>
          {info.length < MAX_INFO && (
            <button
              type="button"
              className="mt-3 min-h-12 rounded-xl border-2 border-line px-4 text-lg font-semibold"
              onClick={() => setInfo((rows) => [...rows, { key: nextKey.current++, label: "" }])}
            >
              + Add info
            </button>
          )}
        </section>

        <section className="rounded-2xl border-2 border-line">
          <button
            type="button"
            aria-expanded={showLimits}
            onClick={() => setShowLimits((v) => !v)}
            className="flex min-h-14 w-full items-center justify-between px-4 text-left text-lg font-bold"
          >
            <span>Rules and your name</span>
            <span aria-hidden>{showLimits ? "▲" : "▼"}</span>
          </button>
          {showLimits && (
            <div className="flex flex-col gap-5 px-4 pb-5">
              <div>
                <label htmlFor="name" className={label}>
                  Your name
                </label>
                <input
                  id="name"
                  className={field}
                  autoComplete="name"
                  value={prefs.name}
                  onChange={(e) => update({ name: e.target.value })}
                />
              </div>
              <div>
                <label htmlFor="disclosure" className={label}>
                  How call4me introduces itself
                </label>
                <textarea
                  id="disclosure"
                  className={`${field} min-h-24`}
                  value={prefs.disclosure}
                  onChange={(e) => update({ disclosure: e.target.value })}
                />
                <p className="mt-1 text-base text-muted">
                  It always says it is an AI. {"{name}"} becomes your name.{" "}
                  {prefs.disclosure !== DEFAULT_DISCLOSURE && (
                    <button
                      type="button"
                      className="underline"
                      onClick={() => update({ disclosure: DEFAULT_DISCLOSURE })}
                    >
                      Reset
                    </button>
                  )}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="max-hold" className={label}>
                    Max hold (min)
                  </label>
                  <input
                    id="max-hold"
                    className={field}
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={240}
                    value={prefs.limits.max_hold_minutes}
                    onChange={(e) => updateLimits({ max_hold_minutes: Math.max(1, Number(e.target.value) || 1) })}
                  />
                </div>
                <div>
                  <label htmlFor="max-spend" className={label}>
                    Max spend ($)
                  </label>
                  <input
                    id="max-spend"
                    className={field}
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="0.01"
                    value={prefs.limits.max_spend_usd}
                    onChange={(e) => updateLimits({ max_spend_usd: Math.max(0, Number(e.target.value) || 0) })}
                  />
                </div>
              </div>
              {(
                [
                  ["allow_plan_changes", "OK to change my plan"],
                  ["allow_cancellation", "OK to cancel something"],
                ] as const
              ).map(([key, text]) => (
                <label key={key} className="flex min-h-12 items-center gap-3 text-lg">
                  <input
                    type="checkbox"
                    className="h-7 w-7 accent-[#ffd60a]"
                    checked={prefs.limits[key]}
                    onChange={(e) => updateLimits({ [key]: e.target.checked })}
                  />
                  {text}
                </label>
              ))}
              <div>
                <label htmlFor="notes" className={label}>
                  Other rules
                </label>
                <input
                  id="notes"
                  className={field}
                  value={prefs.limits.notes}
                  placeholder="e.g. No new subscriptions"
                  onChange={(e) => updateLimits({ notes: e.target.value })}
                />
              </div>
              <button
                type="button"
                className="self-start text-base underline"
                onClick={() => update({ limits: DEFAULT_LIMITS })}
              >
                Reset rules
              </button>
            </div>
          )}
        </section>

        {error && (
          <p role="alert" className="rounded-xl border-2 border-bad bg-panel p-4 text-lg font-semibold text-bad">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="safe-bottom sticky bottom-0 min-h-16 rounded-2xl bg-accent px-6 text-2xl font-extrabold text-black shadow-[0_-12px_24px_#000] disabled:opacity-60"
        >
          {busy ? "Starting…" : "Start call"}
        </button>
      </form>
    </main>
  );
}
