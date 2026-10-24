"use client";

import Link from "next/link";
import { openAccessCode } from "./AccessCode";

export function TopBar({ title = "call4me", right }: { title?: string; right?: React.ReactNode }) {
  return (
    <header className="safe-top border-b border-line bg-black">
      <div className="safe-x flex min-h-14 items-center justify-between gap-3 py-2">
        <Link href="/" className="text-2xl font-extrabold tracking-tight">
          {title}
        </Link>
        <div className="flex items-center gap-2">
          {right}
          <button
            type="button"
            onClick={openAccessCode}
            aria-label="Settings: access code"
            className="flex min-h-12 min-w-12 items-center justify-center rounded-xl border-2 border-line text-2xl"
          >
            ⚙︎
          </button>
        </div>
      </div>
    </header>
  );
}
