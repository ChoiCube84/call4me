import { internalMutation, mutation, query } from "./_generated/server";
import { ConvexError, v } from "convex/values";
import { liveMessage } from "./schema";
import { requireViewer, requireWorker } from "./lib/auth";

const BACKLOG = 400;
export const publish = mutation({
  args: { token: v.string(), messages: v.array(liveMessage) },
  handler: async (ctx, args) => {
    requireWorker(args.token);
    if (args.messages.length > 50) throw new ConvexError("BATCH_TOO_LARGE");
    const sessions = new Set<string>();
    for (const message of args.messages) {
      if (JSON.stringify(message).length > 32_000) throw new ConvexError("MESSAGE_TOO_LARGE");
      if (await ctx.db.query("live_message").withIndex("by_external_id", q => q.eq("id", message.id)).unique()) continue;
      const call = await ctx.db.query("call_session").withIndex("by_external_id", q => q.eq("id", message.session_id)).unique();
      if (!call) throw new ConvexError("CALL_NOT_FOUND");
      const last = await ctx.db.query("live_message")
        .withIndex("by_session_sequence", q => q.eq("session_id", message.session_id)).order("desc").first();
      await ctx.db.insert("live_message", { ...message, sequence: (last?.sequence ?? 0) + 1 });
      sessions.add(message.session_id);
    }
    for (const session of sessions) {
      const rows = await ctx.db.query("live_message")
        .withIndex("by_session_sequence", q => q.eq("session_id", session)).order("desc").take(BACKLOG + 50);
      for (const row of rows.slice(BACKLOG)) await ctx.db.delete(row._id);
    }
    return null;
  },
});

// A read-only subscription. The browser access code never grants worker writes.
export const recent = query({
  args: { accessCode: v.string(), call_id: v.string() },
  handler: async (ctx, args) => {
    requireViewer(args.accessCode);
    const rows = await ctx.db.query("live_message")
      .withIndex("by_session_sequence", q => q.eq("session_id", args.call_id)).order("desc").take(BACKLOG);
    return rows.reverse().map(({ _id, _creationTime, ...message }) => message);
  },
});

// Captions were ephemeral in LiveHub. Bound the new cloud replay cache to 24h.
export const expire = internalMutation({ args: {}, handler: async (ctx) => {
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  const rows = await ctx.db.query("live_message").withIndex("by_creation_time", q => q.lt("_creationTime", cutoff)).take(500);
  for (const row of rows) await ctx.db.delete(row._id);
  return rows.length;
}});
