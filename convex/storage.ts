import { mutation, query } from "./_generated/server";
import type { MutationCtx, QueryCtx } from "./_generated/server";
import { ConvexError, v, type Infer } from "convex/values";
import { records } from "./schema";
import { requireWorker } from "./lib/auth";

const table = v.union(v.literal("call_session"), v.literal("call_event"),
  v.literal("user_request"), v.literal("audit_log"), v.literal("menu_path"), v.literal("device"));
type Table = Infer<typeof table>;
const auth = { token: v.string() };

// All writes have exact validators. There is no generic arbitrary-document endpoint.
const write = v.union(
  ...Object.entries(records).map(([name, record]) => v.object({
    table: v.literal(name as Table), value: record, expected: v.union(record, v.null()),
  })),
);

async function find(ctx: QueryCtx | MutationCtx, name: Table, key: string) {
  if (name === "device") return ctx.db.query("device").withIndex("by_external_id", q => q.eq("user_id", key)).unique();
  return ctx.db.query(name).withIndex("by_external_id", q => q.eq("id", key)).unique();
}
function plain<T extends { _id: unknown; _creationTime: number }>(doc: T) {
  const { _id, _creationTime, ...value } = doc;
  return value;
}
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, val]) => `${JSON.stringify(key)}:${stable(val)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export const ready = query({ args: auth, handler: (_ctx, args) => {
  requireWorker(args.token); return { storage: "convex" };
}});
export const get = query({ args: { ...auth, table, key: v.string() }, handler: async (ctx, args) => {
  requireWorker(args.token);
  const doc = await find(ctx, args.table, args.key);
  return doc ? plain(doc) : null;
}});

export const commit = mutation({ args: { ...auth, writes: v.array(write) }, handler: async (ctx, args) => {
  requireWorker(args.token);
  if (args.writes.length > 100) throw new ConvexError("BATCH_TOO_LARGE");
  for (const item of args.writes) {
    // The mapped union above can't correlate table/value in TypeScript. Runtime
    // validation below binds them before insertion (and rejects extra fields).
    const value = item.value as Record<string, unknown>;
    const fields = records[item.table].fields;
    if (Object.keys(value).some(key => !(key in fields)) ||
        Object.keys(fields).some(key => !(key in value))) throw new ConvexError("INVALID_RECORD");
    const key = (item.table === "device" ? value.user_id : value.id) as string;
    const existing = await find(ctx, item.table, key);
    if (item.expected === null) {
      if (existing) {
        if (stable(plain(existing)) !== stable(value)) throw new ConvexError("CONFLICT");
        continue; // Safe replay after a lost HTTP response.
      }
      if (item.table === "call_event" || item.table === "user_request") {
        if (!await find(ctx, "call_session", value.session_id as string)) throw new ConvexError("CALL_NOT_FOUND");
      }
      await ctx.db.insert(item.table, item.value);
    } else {
      if (item.table === "audit_log" || item.table === "call_event") throw new ConvexError("APPEND_ONLY");
      if (!existing) throw new ConvexError("CONFLICT");
      const before = item.expected as Record<string, unknown>;
      if ((item.table === "device" ? before.user_id : before.id) !== key) throw new ConvexError("INVALID_RECORD");
      const current = plain(existing) as Record<string, unknown>;
      const patch: Record<string, unknown> = {};
      for (const [field, val] of Object.entries(value)) {
        if (stable(val) === stable(before[field])) continue;
        if (stable(current[field]) !== stable(before[field])) throw new ConvexError("CONFLICT");
        patch[field] = val;
      }
      await ctx.db.patch(existing._id, patch);
    }
  }
  return null;
}});

export const listCalls = query({
  args: { ...auth, user_id: v.union(v.string(), v.null()), limit: v.number() },
  handler: async (ctx, args) => {
    requireWorker(args.token);
    const limit = Math.max(1, Math.min(100, Math.floor(args.limit)));
    const q = args.user_id
      ? ctx.db.query("call_session").withIndex("by_user_started", q => q.eq("user_id", args.user_id!))
      : ctx.db.query("call_session").withIndex("by_started");
    return (await q.order("desc").take(limit)).map(plain);
  },
});
export const events = query({ args: { ...auth, call_id: v.string() }, handler: async (ctx, args) => {
  requireWorker(args.token);
  return (await ctx.db.query("call_event").withIndex("by_session_at", q => q.eq("session_id", args.call_id)).collect()).map(plain);
}});
export const audit = query({ args: { ...auth, call_id: v.string() }, handler: async (ctx, args) => {
  requireWorker(args.token);
  const requests = await ctx.db.query("user_request").withIndex("by_session", q => q.eq("session_id", args.call_id)).collect();
  const subjects = [args.call_id, ...requests.map(r => r.id)];
  const entries = (await Promise.all(subjects.map(subject => ctx.db.query("audit_log")
    .withIndex("by_subject_at", q => q.eq("subject", subject)).collect()))).flat();
  return entries.sort((a, b) => a.at.localeCompare(b.at)).map(plain);
}});
export const menu = query({ args: { ...auth, company: v.string() }, handler: async (ctx, args) => {
  requireWorker(args.token);
  const row = await ctx.db.query("menu_path").withIndex("by_company_success", q => q.eq("company", args.company)).order("desc").first();
  return row?.steps_json ?? null;
}});
export const saveMenu = mutation({ args: { ...auth, company: v.string(), steps: v.array(v.string()) }, handler: async (ctx, args) => {
  requireWorker(args.token);
  const rows = await ctx.db.query("menu_path").withIndex("by_company_success", q => q.eq("company", args.company)).collect();
  const row = rows.find(row => stable(row.steps_json) === stable(args.steps));
  const now = new Date().toISOString();
  if (row) await ctx.db.patch(row._id, { success_count: row.success_count + 1, last_used_at: now });
  else await ctx.db.insert("menu_path", {
    id: crypto.randomUUID().replaceAll("-", ""), company: args.company, steps_json: args.steps,
    success_count: 1, last_used_at: now,
  });
  return null;
}});
