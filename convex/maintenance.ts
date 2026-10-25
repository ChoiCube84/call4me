import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";

export const deleteFinishedCallData = internalAction({
  args: {},
  handler: async ctx => {
    const key = process.env.CARTESIA_API_KEY;
    if (!key) return;
    const pending = await ctx.runQuery(internal.managed.pendingDelete, {});
    for (const call of pending) {
      try {
        const response = await fetch(`https://api.cartesia.ai/agents/calls/${call.provider_call_id}`, {
          method: "DELETE", headers: { Authorization: `Bearer ${key}`, "Cartesia-Version": "2026-08-14" },
        });
        if (response.ok || response.status === 404) await ctx.runMutation(internal.managed.markProviderDeleted, {
          session_id: call.session_id,
        });
      } catch { /* retry at the next interval */ }
    }
  },
});

// Backup for a lost completion webhook, matching the previous local worker's
// status poll. The live call and its outcome remain visible even if Cartesia
// does not deliver the final event.
export const pollActiveCalls = internalAction({
  args: {},
  handler: async ctx => {
    const key = process.env.CARTESIA_API_KEY;
    if (!key) return;
    const active = await ctx.runQuery(internal.managed.activeProviderCalls, {});
    for (const item of active) {
      try {
        const response = await fetch(`https://api.cartesia.ai/agents/calls/${item.provider_call_id}`, {
          headers: { Authorization: `Bearer ${key}`, "Cartesia-Version": "2026-08-14" },
        });
        if (!response.ok) continue;
        const call = await response.json();
        if (call.status !== "completed" && call.status !== "failed") continue;
        const reason = String(call.end_reason || "").slice(0, 100).replaceAll("_", " ") || "The call ended.";
        const summary = String(call.summary || "").slice(0, 1000)
          .replace(/\b[\w.+-]+@[\w-]+\.[\w.-]+\b/g, "[REDACTED]")
          .replace(/(?<!\d)\d(?:[ -]?\d){3,}(?!\d)/g, "[REDACTED]");
        await ctx.runMutation(internal.managed.finish, {
          session_id: item.session_id, failed: call.status === "failed", reason, summary,
        });
      } catch { /* try again at the next interval */ }
    }
  },
});
