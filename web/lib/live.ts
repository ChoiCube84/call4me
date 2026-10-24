"use client";

// `WS /calls/{id}/live`: on every (re)connect the server replays the call's recent
// messages, then streams new ones. So each connect starts from an empty view and
// rebuilds it; nothing has to be de-duplicated.

import { useEffect, useReducer, useRef, useState } from "react";
import { type CallState, TERMINAL_STATES, parseTime, requestToken, wsUrl } from "./api";

export type Speaker = "remote" | "agent" | "user";

export interface Caption {
  id: number;
  speaker: Speaker;
  text: string;
  at: Date;
}

export interface AskRequest {
  requestId: string;
  question: string;
  sensitivity: "low" | "medium" | "high";
  timeoutS: number;
  at: Date;
}

export interface EndedInfo {
  summary?: string;
  outcome?: string;
  reference_number?: string | null;
  minutes_saved?: number | null;
  state?: string;
}

export interface Denial {
  reason: string;
  at: Date;
}

export interface LiveView {
  state: CallState | null;
  holdSince: Date | null;
  captions: Caption[];
  partials: Partial<Record<Speaker, string>>;
  summary: string | null;
  ask: AskRequest | null;
  takeoverSuggested: string | null;
  holdPrompt: number | null; // minutes on hold, when asked "keep waiting?"
  denials: Denial[];
  ended: EndedInfo | null;
  messages: number;
}

const EMPTY: LiveView = {
  state: null,
  holdSince: null,
  captions: [],
  partials: {},
  summary: null,
  ask: null,
  takeoverSuggested: null,
  holdPrompt: null,
  denials: [],
  ended: null,
  messages: 0,
};

const MAX_CAPTIONS = 500;
const HOLD_STATES: ReadonlySet<string> = new Set(["HOLD", "RECORDED_MESSAGE"]);

interface LiveMessage {
  type: "caption" | "state" | "summary" | "ask_user" | "event" | "ended";
  session_id: string;
  at: string;
  data: Record<string, unknown>;
}

type Action = { kind: "reset" } | { kind: "msg"; msg: LiveMessage };

let captionSeq = 0;

function reduce(view: LiveView, action: Action): LiveView {
  if (action.kind === "reset") return EMPTY;
  const { msg } = action;
  const d = msg.data || {};
  const at = parseTime(msg.at) || new Date();
  const next: LiveView = { ...view, messages: view.messages + 1 };

  switch (msg.type) {
    case "caption": {
      const speaker = (String(d.speaker || "remote") as Speaker) || "remote";
      const text = String(d.text || "").trim();
      if (!text) return next;
      if (d.final === false) {
        next.partials = { ...view.partials, [speaker]: text };
        return next;
      }
      const partials = { ...view.partials };
      delete partials[speaker];
      next.partials = partials;
      next.captions = [...view.captions, { id: ++captionSeq, speaker, text, at }].slice(-MAX_CAPTIONS);
      return next;
    }
    case "state": {
      const to = String(d.to || "") as CallState;
      if (!to) return next;
      next.state = to;
      if (HOLD_STATES.has(to)) next.holdSince = view.holdSince ?? at;
      else next.holdSince = null;
      if (to === "TAKEOVER") next.takeoverSuggested = null;
      if (TERMINAL_STATES.has(to)) {
        next.ask = null;
        next.holdPrompt = null;
      }
      return next;
    }
    case "summary": {
      if (d.text) next.summary = String(d.text);
      return next;
    }
    case "ask_user": {
      next.ask = {
        requestId: String(d.request_id || ""),
        question: String(d.question || ""),
        sensitivity: (["low", "medium", "high"].includes(String(d.sensitivity))
          ? d.sensitivity
          : "medium") as AskRequest["sensitivity"],
        timeoutS: Number(d.timeout_s) || 60,
        at,
      };
      return next;
    }
    case "event": {
      const kind = String(d.kind || "");
      if (kind === "ask_user_answered" && view.ask?.requestId === d.request_id) next.ask = null;
      else if (kind === "takeover_suggested") next.takeoverSuggested = String(d.reason || "Please take over.");
      else if (kind === "takeover" || kind === "handback") next.takeoverSuggested = null;
      else if (kind === "max_hold") next.holdPrompt = Number(d.hold_minutes) || 0;
      else if (kind === "hold_continue") next.holdPrompt = null;
      else if (kind === "guard_denied" && d.reason)
        next.denials = [...view.denials, { reason: String(d.reason), at }].slice(-20);
      else if (kind === "greeting_played" && d.text) {
        // The pre-generated greeting (the AI disclosure) is spoken by the engine, not
        // the agent, so it arrives as an event: show it as call4me's first line.
        const text = String(d.text).trim();
        next.captions = [...view.captions, { id: ++captionSeq, speaker: "agent" as Speaker, text, at }].slice(-MAX_CAPTIONS);
      }
      return next;
    }
    case "ended": {
      next.ended = d as EndedInfo;
      next.ask = null;
      next.holdPrompt = null;
      next.takeoverSuggested = null;
      if (typeof d.state === "string" && d.state) next.state = d.state as CallState;
      return next;
    }
    default:
      return next;
  }
}

export interface LiveConnection {
  view: LiveView;
  connected: boolean;
  /** True after the first connection dropped and we are trying again. */
  reconnecting: boolean;
  /** Clear the open question locally (after answering, or a 409). */
  dismissAsk: (requestId: string) => void;
}

const BACKOFF_MS = [1000, 2000, 5000, 10000];

export function useLiveCall(callId: string | null): LiveConnection {
  const [view, dispatch] = useReducer(reduce, EMPTY);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const endedRef = useRef(false);

  useEffect(() => {
    if (!callId) return;
    let stopped = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let failedBeforeOpen = 0;
    endedRef.current = false;

    const connect = () => {
      if (stopped) return;
      let opened = false;
      const ws = new WebSocket(wsUrl(`/calls/${encodeURIComponent(callId)}/live`));
      socket = ws;
      ws.onopen = () => {
        opened = true;
        attempt = 0;
        failedBeforeOpen = 0;
        dispatch({ kind: "reset" }); // the server replays the backlog next
        setConnected(true);
        setReconnecting(false);
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(String(e.data)) as LiveMessage;
          if (msg.type === "ended") endedRef.current = true;
          dispatch({ kind: "msg", msg });
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = async () => {
        if (socket === ws) socket = null;
        setConnected(false);
        if (stopped || endedRef.current) return;
        setReconnecting(true);
        if (!opened) {
          // Refused before opening twice in a row: most likely a missing/wrong token.
          failedBeforeOpen += 1;
          if (failedBeforeOpen === 2) {
            await requestToken("This server needs an access code to show the call.");
          }
        }
        const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
        attempt += 1;
        timer = setTimeout(connect, delay);
      };
    };

    // A phone that wakes up should reconnect at once rather than wait for the backoff.
    const onVisible = () => {
      if (document.visibilityState === "visible" && !socket && !stopped && !endedRef.current) {
        if (timer) clearTimeout(timer);
        attempt = 0;
        connect();
      }
    };

    connect();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisible);
      if (timer) clearTimeout(timer);
      socket?.close();
      dispatch({ kind: "reset" });
      setConnected(false);
      setReconnecting(false);
    };
  }, [callId]);

  const shown = view.ask && view.ask.requestId === dismissed ? { ...view, ask: null } : view;
  return { view: shown, connected, reconnecting, dismissAsk: setDismissed };
}

// ---------------------------------------------------------------- plain words

export const SPEAKER_LABEL: Record<Speaker, string> = {
  remote: "Company",
  agent: "call4me",
  user: "You",
};

export interface Phase {
  label: string;
  /** Tailwind classes for the banner. */
  tone: string;
}

export function phaseOf(state: CallState | null): Phase {
  switch (state) {
    case null:
    case "DIALING":
      return { label: "Calling…", tone: "bg-info text-black" };
    case "MENU":
      return { label: "Phone menu", tone: "bg-info text-black" };
    case "HOLD":
    case "RECORDED_MESSAGE":
      return { label: "On hold", tone: "bg-[#b794f6] text-black" };
    case "CALLBACK_OFFERED":
      return { label: "Callback offered", tone: "bg-[#b794f6] text-black" };
    case "HUMAN_SUSPECTED":
    case "HUMAN_CONFIRMED":
      return { label: "A person answered", tone: "bg-ok text-black" };
    case "IN_CONVERSATION":
      return { label: "Talking to the company", tone: "bg-ok text-black" };
    case "WAITING_ON_USER":
      return { label: "Needs your answer", tone: "bg-accent text-black" };
    case "TAKEOVER":
      return { label: "You are typing", tone: "bg-warn text-black" };
    case "VOICEMAIL":
      return { label: "Voicemail. Call ended", tone: "bg-panel-2 text-fg" };
    case "FAILED":
      return { label: "Call failed", tone: "bg-bad text-black" };
    case "ENDED":
      return { label: "Call ended", tone: "bg-panel-2 text-fg" };
    default:
      return { label: String(state), tone: "bg-panel-2 text-fg" };
  }
}

export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}
