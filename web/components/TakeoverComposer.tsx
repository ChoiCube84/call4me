"use client";

import { useRef, useState } from "react";
import { api } from "@/lib/api";

const QUICK = ["Yes", "No", "Can you repeat that?", "One moment please", "Thank you"];

/** Type-to-talk: what you type is spoken to the rep, word for word. The text box is
 * cleared as soon as a line is sent (it may hold private info typed to the rep). */
export function TakeoverComposer({
  callId,
  onHandback,
}: {
  callId: string;
  onHandback: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const say = async (text: string) => {
    const line = text.trim();
    if (!line) return;
    setBusy(true);
    setError(null);
    try {
      await api.type(callId, line);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send.");
    } finally {
      setBusy(false);
      ref.current?.focus();
    }
  };

  const sendTyped = () => {
    const el = ref.current;
    if (!el) return;
    const text = el.value;
    el.value = "";
    void say(text);
  };

  return (
    <section
      aria-label="Type to talk"
      className="safe-x safe-bottom fixed inset-x-0 bottom-0 z-40 border-t-4 border-warn bg-panel pt-3"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-lg font-extrabold text-warn">You are talking. call4me is quiet.</span>
        <button
          type="button"
          onClick={onHandback}
          className="min-h-12 rounded-xl border-2 border-ok px-3 text-base font-bold text-ok"
        >
          Hand back to call4me
        </button>
      </div>
      <div className="mb-2 flex gap-2 overflow-x-auto pb-1">
        {QUICK.map((q) => (
          <button
            key={q}
            type="button"
            disabled={busy}
            onClick={() => void say(q)}
            className="min-h-12 shrink-0 rounded-full border-2 border-line bg-black px-4 text-lg font-semibold"
          >
            {q}
          </button>
        ))}
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          sendTyped();
        }}
      >
        <label htmlFor="type-to-talk" className="sr-only">
          Type what to say to the rep
        </label>
        <textarea
          id="type-to-talk"
          ref={ref}
          rows={2}
          autoFocus
          enterKeyHint="send"
          placeholder="Type what to say…"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendTyped();
            }
          }}
          className="min-h-16 flex-1 rounded-2xl border-2 border-warn bg-black px-4 py-3 text-xl"
        />
        <button
          type="submit"
          disabled={busy}
          className="min-h-16 min-w-24 rounded-2xl bg-warn px-4 text-xl font-extrabold text-black disabled:opacity-60"
        >
          Say it
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-2 text-lg font-semibold text-bad">
          {error}
        </p>
      )}
    </section>
  );
}
