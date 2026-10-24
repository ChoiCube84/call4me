// REST client for the call4me backend (CLAUDE.md section 10).
// Request bodies may hold private text (answers, typed lines): never log them.

export type CallState =
  | "DIALING"
  | "MENU"
  | "HOLD"
  | "RECORDED_MESSAGE"
  | "CALLBACK_OFFERED"
  | "HUMAN_SUSPECTED"
  | "HUMAN_CONFIRMED"
  | "IN_CONVERSATION"
  | "WAITING_ON_USER"
  | "TAKEOVER"
  | "VOICEMAIL"
  | "FAILED"
  | "ENDED";

export const TERMINAL_STATES: ReadonlySet<CallState> = new Set(["VOICEMAIL", "FAILED", "ENDED"]);

export interface CallLimits {
  max_hold_minutes: number;
  max_spend_usd: number;
  allow_plan_changes: boolean;
  allow_cancellation: boolean;
  notes: string;
}

export interface CallSession {
  id: string;
  user_id: string;
  company: string;
  phone_number: string;
  goal: string;
  limits: CallLimits;
  disclosure_text: string;
  state: CallState;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  reference_number: string | null;
  minutes_saved: number | null;
}

export interface TimelineEvent {
  at: string;
  kind: string;
  from_state: string | null;
  to_state: string | null;
  confidence: number | null;
  detail: Record<string, unknown>;
}

export interface CallDetail {
  session: CallSession;
  timeline: TimelineEvent[];
}

export interface AuditRow {
  at: string;
  actor: string;
  action: string;
  subject: string | null;
  allowed: boolean;
  reason: string;
  diff: Record<string, unknown>;
  evidence: Record<string, unknown>;
}

export interface Company {
  name: string;
  phone_number: string;
  note?: string;
}

export interface StartCallBody {
  user_id: string;
  company: string;
  phone_number: string;
  goal: string;
  limits: CallLimits;
  disclosure_text: string;
  known_info: { label: string; value: string }[];
}

export interface EvalReport {
  meta?: Record<string, unknown>;
  metrics: {
    scenarios: number;
    passed: number;
    success_rate: number;
    minutes_saved_total: number;
    minutes_saved_avg: number;
    times_asked_user: number;
    errors: number;
    safety_violations: number;
    guard_denials: number;
    time_to_greet_ms_median?: number;
    [k: string]: unknown;
  };
  results: {
    id: string;
    title: string;
    passed: boolean;
    minutes_saved: number;
    asks: number;
    guard_denials: unknown[] | number;
    failures?: string[];
  }[];
}

// ---------------------------------------------------------------- base + token

/** "" = same origin (production: FastAPI serves this app). Dev: http://localhost:8000. */
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/+$/, "");

const TOKEN_KEY = "call4me.token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token.trim());
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: keep working without persistence */
  }
}

export function wsUrl(path: string): string {
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  if (API_BASE) return API_BASE.replace(/^http/, "ws") + path + q;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}${q}`;
}

// The access-code modal (components/AccessCode.tsx) registers itself here. A 401 asks
// it for a code once; concurrent 401s share the same prompt.
type TokenAsker = (reason: string) => Promise<boolean>;
let asker: TokenAsker | null = null;
let pending: Promise<boolean> | null = null;

export function registerTokenAsker(fn: TokenAsker | null): void {
  asker = fn;
}

export function requestToken(reason = "This server needs an access code."): Promise<boolean> {
  if (!asker) return Promise.resolve(false);
  if (!pending) {
    pending = asker(reason).finally(() => {
      pending = null;
    });
  }
  return pending;
}

// ---------------------------------------------------------------- errors

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** FastAPI errors in plain words: 422 lists come as detail[].msg. */
function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => String((d as { msg?: string }).msg || ""))
      .map((m) => m.replace(/^Value error, /, ""))
      .filter(Boolean);
    if (msgs.length) return msgs.join(" ");
  }
  if (typeof detail === "string") return detail;
  if (status === 0) return "Can't reach the server. Check your internet.";
  if (status === 401) return "Wrong access code.";
  return `Something went wrong (code ${status}). Try again.`;
}

// ---------------------------------------------------------------- fetch

async function request<T>(method: string, path: string, body?: unknown, retried = false): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, errorMessage(0, null));
  }

  if (res.status === 401 && !retried) {
    const ok = await requestToken(token ? "That access code didn't work." : undefined);
    if (ok) return request<T>(method, path, body, true);
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}

// ---------------------------------------------------------------- endpoints

export const api = {
  startCall: (body: StartCallBody) => request<CallSession>("POST", "/calls", body),
  listCalls: (limit = 20) => request<CallSession[]>("GET", `/calls?limit=${limit}`),
  getCall: (id: string) => request<CallDetail>("GET", `/calls/${encodeURIComponent(id)}`),
  audit: (id: string) => request<AuditRow[]>("GET", `/calls/${encodeURIComponent(id)}/audit`),
  takeover: (id: string) => request<unknown>("POST", `/calls/${encodeURIComponent(id)}/takeover`),
  handback: (id: string) => request<unknown>("POST", `/calls/${encodeURIComponent(id)}/handback`),
  cancel: (id: string) => request<unknown>("POST", `/calls/${encodeURIComponent(id)}/cancel`),
  type: (id: string, text: string) =>
    request<unknown>("POST", `/calls/${encodeURIComponent(id)}/type`, { text }),
  keepHolding: (id: string, keepWaiting: boolean) =>
    request<unknown>("POST", `/calls/${encodeURIComponent(id)}/hold`, { keep_waiting: keepWaiting }),
  answer: (requestId: string, answer: string) =>
    request<unknown>("POST", `/requests/${encodeURIComponent(requestId)}/answer`, { answer }),
  companies: (q: string) => request<Company[]>("GET", `/companies?q=${encodeURIComponent(q)}`),
  metrics: () => request<EvalReport>("GET", "/metrics"),
  health: () => request<{ status: string }>("GET", "/health"),
};

/** Backend datetimes may be naive (UTC without an offset). */
export function parseTime(v: string | null | undefined): Date | null {
  if (!v) return null;
  const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v);
  const d = new Date(hasZone ? v : v + "Z");
  return isNaN(d.getTime()) ? null : d;
}
