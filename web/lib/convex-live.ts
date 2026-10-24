"use client";

import { ConvexClient } from "convex/browser";
import { makeFunctionReference } from "convex/server";
import { ConvexError } from "convex/values";
import { getToken, requestToken } from "./api";

export const CONVEX_URL = process.env.NEXT_PUBLIC_CONVEX_URL || "";
export interface ReplayMessage {
  id: string;
  sequence: number;
  type: "caption" | "state" | "summary" | "ask_user" | "event" | "ended";
  session_id: string;
  at: string;
  data: Record<string, unknown>;
}
const recent = makeFunctionReference<"query", { accessCode: string; call_id: string }, ReplayMessage[]>("live:recent");

/** Convex owns reconnection. Resume by sequence; never replay duplicate captions. */
export function subscribeToCall(
  callId: string,
  onMessage: (message: ReplayMessage) => void,
  onConnection: (connected: boolean, reconnecting: boolean) => void,
): () => void {
  const client = new ConvexClient(CONVEX_URL);
  let stopped = false;
  let accepted = false;
  let seen = 0;
  let unsubscribe: (() => void) | undefined;
  let prompting = false;
  const connection = client.subscribeToConnectionState(state => {
    if (!stopped) onConnection(accepted && state.isWebSocketConnected, accepted && !state.isWebSocketConnected);
  });
  const subscribe = () => {
    unsubscribe?.();
    accepted = false;
    unsubscribe = client.onUpdate(recent, { accessCode: getToken(), call_id: callId }, messages => {
      if (stopped) return;
      accepted = true;
      onConnection(true, false);
      for (const message of messages) {
        if (message.sequence > seen) {
          onMessage(message);
          seen = message.sequence;
        }
      }
    }, async error => {
      if (stopped) return;
      accepted = false;
      onConnection(false, false);
      if (error instanceof ConvexError && error.data === "UNAUTHORIZED" && !prompting) {
        prompting = true;
        const ok = await requestToken("This server needs an access code to show the call.");
        prompting = false;
        if (ok && !stopped) subscribe();
      }
    });
  };
  subscribe();
  return () => {
    stopped = true;
    unsubscribe?.();
    connection();
    void client.close();
  };
}
