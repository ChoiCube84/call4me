"use client";

import { useEffect, useRef, useState } from "react";
import { getToken, registerTokenAsker, setToken } from "@/lib/api";

interface Pending {
  reason: string;
  resolve: (ok: boolean) => void;
}

/** Asks for the server's access code (API_TOKEN) when the API answers 401.
 * Mounted once in the root layout. The code is a shared server password, not user
 * data, so it is remembered in localStorage. */
export function AccessCodeHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    registerTokenAsker(
      (reason) =>
        new Promise<boolean>((resolve) => {
          setPending({ reason, resolve });
        }),
    );
    const open = () =>
      setPending({ reason: "Change the access code for this server.", resolve: () => {} });
    window.addEventListener("call4me:access-code", open);
    return () => {
      registerTokenAsker(null);
      window.removeEventListener("call4me:access-code", open);
    };
  }, []);

  useEffect(() => {
    if (pending) inputRef.current?.focus();
  }, [pending]);

  if (!pending) return null;

  const finish = (ok: boolean) => {
    if (ok) {
      const v = inputRef.current?.value.trim() || "";
      if (!v) return;
      setToken(v);
    }
    pending.resolve(ok);
    setPending(null);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="access-title"
      className="fixed inset-0 z-[60] flex items-end justify-center bg-black/80 sm:items-center"
    >
      <form
        className="safe-bottom w-full max-w-md rounded-t-3xl border-2 border-accent bg-panel p-6 sm:rounded-3xl"
        onSubmit={(e) => {
          e.preventDefault();
          finish(true);
        }}
      >
        <h2 id="access-title" className="text-2xl font-bold">
          Access code
        </h2>
        <p className="mt-2 text-muted">{pending.reason}</p>
        <label htmlFor="access-input" className="mt-5 block font-semibold">
          Type the access code
        </label>
        <input
          id="access-input"
          ref={inputRef}
          type="password"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          defaultValue={getToken()}
          className="mt-2 w-full rounded-xl border-2 border-line bg-black px-4 py-3 text-xl"
        />
        <div className="mt-6 flex gap-3">
          <button
            type="submit"
            className="min-h-14 flex-1 rounded-xl bg-accent px-4 text-lg font-bold text-black"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => finish(false)}
            className="min-h-14 rounded-xl border-2 border-line px-5 text-lg font-semibold"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

export function openAccessCode(): void {
  window.dispatchEvent(new Event("call4me:access-code"));
}
