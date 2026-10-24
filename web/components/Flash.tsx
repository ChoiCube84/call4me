"use client";

import { useEffect, useState } from "react";
import { vibrate } from "@/lib/hooks";

// Alerts already shown, so a reconnect (which replays the call) doesn't flash again.
const seen = new Set<string>();

/** Full-screen yellow/black flash plus vibration when `alertKey` is new. Visual and
 * haptic only: never a sound-only cue (CLAUDE.md rule 7). */
export function Flash({ alertKey }: { alertKey: string | null }) {
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    if (!alertKey || seen.has(alertKey)) return;
    seen.add(alertKey);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- start the one-shot flash
    setActive(alertKey);
    vibrate();
    const t = setTimeout(() => setActive(null), 2100);
    return () => clearTimeout(t);
  }, [alertKey]);

  if (!active) return null;
  return <div aria-hidden className="alert-flash pointer-events-none fixed inset-0 z-[70]" />;
}
