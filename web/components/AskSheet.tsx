"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import type { AskRequest } from "@/lib/live";

/** The agent needs something only the user knows. Full screen, impossible to miss.
 * Privacy (CLAUDE.md rule 2): the answer is read from the input when sent, the input
 * is cleared at once, and the value is never kept in state, storage, or the console. */
export function AskSheet({
  ask,
  onDone,
  onSayItMyself,
}: {
  ask: AskRequest;
  onDone: (requestId: string) => void;
  onSayItMyself: () => void;
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [sending, setSending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const now = useNow(true);
  const high = ask.sensitivity === "high";
  const left = Math.max(0, Math.ceil(ask.timeoutS - (now - ask.at.getTime()) / 1000));

  useEffect(() => {
    if (!high) inputRef.current?.focus();
  }, [ask.requestId, high]);

  const send = async () => {
    const el = inputRef.current;
    const value = el?.value.trim() || "";
    if (!el || !value || sending) return;
    el.value = ""; // gone from the screen before it leaves the phone
    setSending(true);
    setNote(null);
    try {
      await api.answer(ask.requestId, value);
      onDone(ask.requestId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNote("This question was already answered or it timed out.");
        setTimeout(() => onDone(ask.requestId), 2500);
      } else {
        setNote(`${err instanceof Error ? err.message : "Could not send."} Type it again.`);
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="ask-title"
      aria-describedby="ask-question"
      className="fixed inset-0 z-50 flex flex-col overflow-y-auto bg-black"
    >
      <div className="safe-top bg-accent text-black">
        <div className="safe-x flex min-h-16 items-center justify-between gap-3 py-3" aria-live="assertive">
          <span id="ask-title" className="text-2xl font-extrabold">
            {high ? "Private info needed" : "Question for you"}
          </span>
          <span className="rounded-full bg-black px-3 py-1 font-mono text-lg font-bold text-accent">
            {left > 0 ? `${left}s` : "waiting"}
          </span>
        </div>
      </div>

      <div className="safe-x safe-bottom flex flex-1 flex-col gap-5 py-6">
        <p className="text-sm font-bold uppercase tracking-wide text-muted">The company asks:</p>
        <p id="ask-question" className="text-3xl font-bold leading-tight">
          {ask.question}
        </p>

        {high ? (
          <>
            <p className="rounded-2xl border-2 border-warn bg-panel p-4 text-xl">
              This is private (like a full card or Social Security number). call4me won&apos;t say it.
              Type it to the rep yourself.
            </p>
            <button
              type="button"
              onClick={onSayItMyself}
              className="pulse-ring min-h-16 rounded-2xl bg-accent px-6 text-2xl font-extrabold text-black"
            >
              Say it myself
            </button>
          </>
        ) : (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <label htmlFor="ask-answer" className="text-lg font-semibold">
              Your answer (call4me says it once, then forgets it)
            </label>
            <textarea
              id="ask-answer"
              ref={inputRef}
              rows={2}
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="send"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              className="w-full rounded-2xl border-4 border-accent bg-panel px-4 py-3 text-2xl"
            />
            <button
              type="submit"
              disabled={sending}
              className="min-h-16 rounded-2xl bg-accent px-6 text-2xl font-extrabold text-black disabled:opacity-60"
            >
              {sending ? "Sending…" : "Send answer"}
            </button>
            <button
              type="button"
              onClick={onSayItMyself}
              className="min-h-12 self-start rounded-xl border-2 border-line px-4 text-lg font-semibold"
            >
              I&apos;ll type it to the rep myself
            </button>
          </form>
        )}

        {note && (
          <p role="alert" className="rounded-xl bg-panel-2 p-3 text-lg font-semibold text-warn">
            {note}
          </p>
        )}
        {left === 0 && (
          <p className="text-lg text-muted">
            call4me asked the company to hold. You can still answer.
          </p>
        )}
      </div>
    </div>
  );
}
