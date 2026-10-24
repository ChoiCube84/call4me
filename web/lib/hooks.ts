"use client";

import { useEffect, useState } from "react";

type WakeLockStatus = "unsupported" | "on" | "off";

interface WakeLockSentinelLike {
  released: boolean;
  release: () => Promise<void>;
  addEventListener: (type: "release", fn: () => void) => void;
}

/** Keep the screen on while `active` (Screen Wake Lock API). Re-acquired when the page
 * becomes visible again, since the browser drops the lock when it is hidden. */
export function useWakeLock(active: boolean): WakeLockStatus {
  const [status, setStatus] = useState<WakeLockStatus>("off");

  useEffect(() => {
    const wl = (navigator as Navigator & {
      wakeLock?: { request: (t: "screen") => Promise<WakeLockSentinelLike> };
    }).wakeLock;
    if (!wl) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time capability flag
      setStatus("unsupported");
      return;
    }
    if (!active) return;
    let sentinel: WakeLockSentinelLike | null = null;
    let cancelled = false;

    const acquire = async () => {
      if (cancelled || document.visibilityState !== "visible") return;
      if (sentinel && !sentinel.released) return;
      try {
        sentinel = await wl.request("screen");
        if (cancelled) {
          await sentinel.release();
          return;
        }
        setStatus("on");
        sentinel.addEventListener("release", () => setStatus("off"));
      } catch {
        setStatus("off"); // e.g. battery saver; not fatal
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void acquire();
    };

    void acquire();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      void sentinel?.release().catch(() => {});
      setStatus("off");
    };
  }, [active]);

  return status;
}

/** A clock that ticks every `ms` while `active` (for hold timers and countdowns). */
export function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return now;
}

/** Visual + haptic alert. Never sound-only: these users are Deaf / hard of hearing. */
export function vibrate(pattern: number[] = [200, 100, 200, 100, 400]): void {
  try {
    navigator.vibrate?.(pattern);
  } catch {
    /* iOS Safari has no vibration API; the full-screen flash still shows */
  }
}
