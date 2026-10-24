"use client";

import type { CallLimits } from "./api";

// Things worth remembering between calls. NEVER the "info the agent may share" values
// or ask_user answers: those live only in the inputs until they are sent.
export interface Prefs {
  name: string;
  company: string;
  phone: string;
  disclosure: string;
  limits: CallLimits;
}

export const DEFAULT_DISCLOSURE =
  "Hi, I'm an AI assistant calling on behalf of {name}. They are Deaf and are reading this call live.";

export const DEFAULT_LIMITS: CallLimits = {
  max_hold_minutes: 60,
  max_spend_usd: 0,
  allow_plan_changes: false,
  allow_cancellation: false,
  notes: "No new charges or subscriptions.",
};

const PREFS_KEY = "call4me.prefs";
const USER_KEY = "call4me.userId";
const SIZE_KEY = "call4me.captionSize";

function read<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode */
  }
}

export function loadPrefs(): Prefs {
  const p = read<Partial<Prefs>>(PREFS_KEY) || {};
  return {
    name: p.name || "",
    company: p.company || "",
    phone: p.phone || "",
    disclosure: p.disclosure || DEFAULT_DISCLOSURE,
    limits: { ...DEFAULT_LIMITS, ...(p.limits || {}) },
  };
}

export function savePrefs(p: Prefs): void {
  write(PREFS_KEY, p);
}

/** Anonymous id sent as `user_id`; created once per browser. */
export function userId(): string {
  let id = read<string>(USER_KEY);
  if (!id) {
    id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `u-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    write(USER_KEY, id);
  }
  return id;
}

export function fillDisclosure(template: string, name: string): string {
  const who = name.trim() || "my client";
  return template.replaceAll("{name}", who);
}

export const CAPTION_SIZES = [20, 24, 28, 34, 40, 48];

export function loadCaptionSize(): number {
  const v = read<number>(SIZE_KEY);
  return v && CAPTION_SIZES.includes(v) ? v : 24;
}

export function saveCaptionSize(px: number): void {
  write(SIZE_KEY, px);
}

const ENDED_PREFIX = "call4me.ended.";

export function stashEnded(callId: string, data: unknown): void {
  try {
    sessionStorage.setItem(ENDED_PREFIX + callId, JSON.stringify(data));
  } catch {
    /* ignore */
  }
}

export function readEnded<T>(callId: string): T | null {
  try {
    const raw = sessionStorage.getItem(ENDED_PREFIX + callId);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}
