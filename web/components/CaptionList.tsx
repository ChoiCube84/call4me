"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { type Caption, SPEAKER_LABEL, type Speaker } from "@/lib/live";

const SPEAKER_STYLE: Record<Speaker, { label: string; bubble: string }> = {
  remote: { label: "text-info", bubble: "bg-panel border-l-4 border-info" },
  agent: { label: "text-ok", bubble: "bg-panel border-l-4 border-ok" },
  user: { label: "text-accent", bubble: "bg-panel-2 border-l-4 border-accent" },
};

/** Live captions of both sides. Auto-scrolls while you are at the bottom; if you
 * scroll up to reread, it stays put and offers a "Jump to latest" button. */
export function CaptionList({
  captions,
  partials,
  emptyText = "Captions will show here when the call starts.",
  scrollParent = "window",
  bottomInset = 0,
}: {
  captions: Caption[];
  partials: Partial<Record<Speaker, string>>;
  emptyText?: string;
  /** "window" on the phone screens; "self" inside a dashboard panel. */
  scrollParent?: "window" | "self";
  /** Height (px) of fixed controls covering the bottom; auto-scroll stops above them. */
  bottomInset?: number;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const partialList = (Object.entries(partials) as [Speaker, string][]).filter(([, t]) => t);

  useEffect(() => {
    const target: HTMLElement | Window | null = scrollParent === "self" ? boxRef.current : window;
    if (!target) return;
    // Only a real gesture unpins: our own auto-scroll fires scroll events too, and during
    // a burst of captions they can arrive after the page has grown again.
    let lastGesture = 0;
    const onGesture = () => {
      lastGesture = Date.now();
    };
    const onScroll = () => {
      let gap: number;
      if (scrollParent === "self" && boxRef.current) {
        const el = boxRef.current;
        gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      } else {
        gap = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
      }
      if (gap < 120) setPinned(true);
      else if (Date.now() - lastGesture < 1000) setPinned(false);
    };
    const gestures = ["touchmove", "wheel", "keydown"] as const;
    target.addEventListener("scroll", onScroll, { passive: true });
    gestures.forEach((g) => target.addEventListener(g, onGesture, { passive: true }));
    return () => {
      target.removeEventListener("scroll", onScroll);
      gestures.forEach((g) => target.removeEventListener(g, onGesture));
    };
  }, [scrollParent]);

  useLayoutEffect(() => {
    if (pinned) endRef.current?.scrollIntoView({ block: "end" });
  }, [captions.length, partialList.length, pinned, bottomInset]);

  const jump = () => {
    endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
    setPinned(true);
  };

  return (
    <div
      ref={boxRef}
      className={scrollParent === "self" ? "relative h-full overflow-y-auto" : "relative"}
    >
      <ol className="flex flex-col gap-3" aria-live="polite" aria-relevant="additions" aria-label="Call captions">
        {captions.length === 0 && partialList.length === 0 && (
          <li className="rounded-2xl border-2 border-dashed border-line p-5 text-lg text-muted">{emptyText}</li>
        )}
        {captions.map((c) => (
          <li key={c.id} className={`rounded-2xl px-4 py-3 ${SPEAKER_STYLE[c.speaker]?.bubble ?? "bg-panel"}`}>
            <div className={`text-sm font-bold uppercase tracking-wide ${SPEAKER_STYLE[c.speaker]?.label ?? ""}`}>
              {SPEAKER_LABEL[c.speaker] ?? c.speaker}
            </div>
            <p className="caption-text mt-1 break-words">{c.text}</p>
          </li>
        ))}
        {partialList.map(([speaker, text]) => (
          <li
            key={`partial-${speaker}`}
            className={`rounded-2xl px-4 py-3 opacity-80 ${SPEAKER_STYLE[speaker]?.bubble ?? "bg-panel"}`}
          >
            <div className={`text-sm font-bold uppercase tracking-wide ${SPEAKER_STYLE[speaker]?.label ?? ""}`}>
              {SPEAKER_LABEL[speaker]} · speaking
            </div>
            <p className="caption-text mt-1 break-words italic">{text}</p>
          </li>
        ))}
      </ol>
      <div ref={endRef} className="h-1" style={{ scrollMarginBottom: bottomInset + 16 }} />
      {!pinned && (
        <button
          type="button"
          onClick={jump}
          style={{ bottom: scrollParent === "self" ? 12 : bottomInset + 24 }}
          className={`${scrollParent === "self" ? "sticky" : "fixed"} left-1/2 z-20 min-h-12 -translate-x-1/2 rounded-full bg-accent px-5 text-lg font-bold text-black shadow-lg`}
        >
          ↓ Jump to latest
        </button>
      )}
    </div>
  );
}
