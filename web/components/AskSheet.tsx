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
  const [viewport, setViewport] = useState<{ top: number; height: number } | null>(null);
  const now = useNow(true);
  const high = ask.sensitivity === "high";
  const compact = !high && viewport !== null && viewport.height < 600;
  const left = Math.max(0, Math.ceil(ask.timeoutS - (now - ask.at.getTime()) / 1000));

  useEffect(() => {
    if (high) return;
    const visible = window.visualViewport;
    // The keyboard can shrink and pan the visible area without resizing fixed elements.
    const update = () => setViewport({ top: visible?.offsetTop ?? 0, height: visible?.height ?? window.innerHeight });
    const frame = requestAnimationFrame(update);
    visible?.addEventListener("resize", update);
    visible?.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    return () => {
      cancelAnimationFrame(frame);
      visible?.removeEventListener("resize", update);
      visible?.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [high]);

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

  const typeItMyself = (
    <button
      type="button"
      onClick={onSayItMyself}
      className={`min-h-12 self-start rounded-xl border-2 border-line px-4 font-semibold ${compact ? "mt-3 text-base" : "text-lg"}`}
    >
      I&apos;ll type it to the rep myself
    </button>
  );

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="ask-title"
      aria-describedby="ask-question"
      className={`fixed inset-0 z-50 flex flex-col bg-black ${high ? "overflow-y-auto" : "overflow-hidden"}`}
      style={!high && viewport ? { top: viewport.top, height: viewport.height, bottom: "auto" } : undefined}
    >
      <div className="safe-top shrink-0 bg-accent text-black">
        <div className={`safe-x flex items-center justify-between gap-3 ${compact ? "min-h-12 py-2" : "min-h-16 py-3"}`} aria-live="assertive">
          <span id="ask-title" className="text-2xl font-extrabold">
            {high ? "Private info needed" : "Question for you"}
          </span>
          <span className="rounded-full bg-black px-3 py-1 font-mono text-lg font-bold text-accent">
            {left > 0 ? `${left}s` : "waiting"}
          </span>
        </div>
      </div>

      <div className={`safe-x safe-bottom flex flex-1 flex-col ${high ? "gap-5 py-6" : compact ? "min-h-0 gap-2 pt-2" : "min-h-0 gap-5 pt-6"}`}>
        <div className={high ? "contents" : "min-h-0 flex-1 overflow-y-auto overscroll-contain"}>
          <p className="text-sm font-bold uppercase tracking-wide text-muted">The company asks:</p>
          <p id="ask-question" className={`font-bold leading-tight ${compact ? "mt-1 text-2xl" : "mt-3 text-3xl"}`}>
            {ask.question}
          </p>
          {note && (
            <p role="alert" className="mt-3 rounded-xl bg-panel-2 p-3 text-lg font-semibold text-warn">
              {note}
            </p>
          )}
          {left === 0 && (
            <p className="mt-3 text-lg text-muted">
              call4me asked the company to hold. You can still answer.
            </p>
          )}
          {compact && typeItMyself}
        </div>

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
            className={`shrink-0 ${compact ? "grid grid-cols-[minmax(0,1fr)_7rem] gap-2" : "flex flex-col gap-4"}`}
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <label htmlFor="ask-answer" className={`font-semibold ${compact ? "col-span-2 text-sm" : "text-lg"}`}>
              Your answer (shared once, then forgotten)
            </label>
            <textarea
              id="ask-answer"
              ref={inputRef}
              rows={compact ? 1 : 2}
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
              className={`w-full resize-none rounded-2xl border-4 border-accent bg-panel px-4 text-2xl ${compact ? "py-2" : "py-3"}`}
            />
            <button
              type="submit"
              disabled={sending}
              className={`${compact ? "min-h-12 px-2 text-xl" : "min-h-16 px-6 text-2xl"} rounded-2xl bg-accent font-extrabold text-black disabled:opacity-60`}
            >
              {sending ? "Sending…" : "Send answer"}
            </button>
            {!compact && typeItMyself}
          </form>
        )}
      </div>
    </div>
  );
}
