"use client";

import type { CallState } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import { formatDuration, phaseOf } from "@/lib/live";

/** Big, color-coded "where is the call" banner. Color is never the only signal: the
 * label always says it in words. */
export function StateBanner({
  state,
  holdSince,
  reconnecting,
}: {
  state: CallState | null;
  holdSince: Date | null;
  reconnecting: boolean;
}) {
  const now = useNow(Boolean(holdSince));
  const phase = phaseOf(state);
  return (
    <div className="sticky top-0 z-30">
      <div className={`safe-top ${phase.tone}`}>
        <div
          className="safe-x flex min-h-16 items-center justify-between gap-3 py-3"
          role="status"
          aria-live="polite"
        >
          <span className="text-2xl font-extrabold tracking-tight">{phase.label}</span>
          {holdSince && (
            <span className="rounded-full bg-black/80 px-3 py-1 font-mono text-xl font-bold text-white">
              {formatDuration(now - holdSince.getTime())}
            </span>
          )}
        </div>
      </div>
      {reconnecting && (
        <div className="safe-x bg-warn py-2 text-center text-base font-bold text-black" role="status">
          Connection lost. Reconnecting…
        </div>
      )}
    </div>
  );
}
