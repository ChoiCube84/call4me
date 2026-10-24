import { convexTest } from "convex-test";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import schema from "../../convex/schema";
import { api, internal } from "../../convex/_generated/api";

const modules = import.meta.glob("../../convex/**/*.ts");
const token = "worker-test-only";
const accessCode = "viewer-test-only";
const call = {
  id: "call-1", user_id: "demo", company: "Synthetic support", phone_number: "+15550100100",
  goal: "Refund", limits_json: {}, disclosure_text: "I am an AI", state: "DIALING",
  started_at: "2026-10-24T00:00:00+00:00", ended_at: null, outcome: null,
  reference_number: null, minutes_saved: null,
};
const request = {
  id: "req-1", session_id: call.id, question: "PIN?", sensitivity: "medium" as const,
  status: "pending" as const, created_at: call.started_at, answered_at: null,
};
const audit = {
  id: "audit-1", at: call.started_at, actor: "test", action: "call.start", subject: call.id,
  allowed: true, reason: "test", diff_json: {}, evidence_json: {},
};
function setup() { return convexTest(schema, modules); }
async function seed(t: ReturnType<typeof setup>) {
  await t.mutation(api.storage.commit, { token, writes: [{ table: "call_session", value: call, expected: null }] });
}
beforeEach(() => {
  vi.stubEnv("CALL4ME_BACKEND_TOKEN", token);
  vi.stubEnv("CALL4ME_VIEWER_TOKEN", accessCode);
});
afterEach(() => { vi.unstubAllEnvs(); vi.useRealTimers(); });

describe("storage authorization and transactions", () => {
  it("fails closed when credentials are absent, wrong, or viewer-only", async () => {
    const t = setup();
    for (const wrong of ["", "wrong", accessCode]) {
      await expect(t.query(api.storage.ready, { token: wrong })).rejects.toThrow("UNAUTHORIZED");
      await expect(t.mutation(api.storage.commit, { token: wrong, writes: [] })).rejects.toThrow("UNAUTHORIZED");
    }
    vi.stubEnv("CALL4ME_BACKEND_TOKEN", "");
    await expect(t.query(api.storage.ready, { token })).rejects.toThrow("UNAUTHORIZED");
  });
  it("stores sessions, requests, events, and audit together, with stable public IDs", async () => {
    const t = setup(); await seed(t);
    await t.mutation(api.storage.commit, { token, writes: [
      { table: "user_request", value: request, expected: null },
      { table: "audit_log", value: { ...audit, subject: request.id }, expected: null },
    ] });
    const rows = await t.query(api.storage.listCalls, { token, user_id: "demo", limit: 20 });
    expect(rows).toEqual([call]);
    expect(await t.query(api.storage.listCalls, { token, user_id: "other", limit: 20 })).toEqual([]);
    expect(await t.query(api.storage.audit, { token, call_id: call.id })).toHaveLength(1);
  });
  it("rejects answer columns and orphaned requests", async () => {
    const t = setup();
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "user_request", value: request, expected: null },
    ] })).rejects.toThrow("CALL_NOT_FOUND");
    await seed(t);
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "user_request", value: { ...request, answer: "never-store" } as typeof request, expected: null },
    ] })).rejects.toThrow();
  });
  it("makes audit append-only and rolls back the entire batch on failure", async () => {
    const t = setup(); await seed(t);
    await t.mutation(api.storage.commit, { token, writes: [{ table: "audit_log", value: audit, expected: null }] });
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "call_session", value: { ...call, state: "HOLD" }, expected: call },
      { table: "audit_log", value: { ...audit, reason: "changed" }, expected: audit },
    ] })).rejects.toThrow("APPEND_ONLY");
    expect(await t.query(api.storage.get, { token, table: "call_session", key: call.id })).toEqual(call);
  });
  it("retries inserts idempotently but rejects an ID collision", async () => {
    const t = setup(); await seed(t); await seed(t);
    expect(await t.query(api.storage.listCalls, { token, user_id: null, limit: 100 })).toHaveLength(1);
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "call_session", value: { ...call, goal: "different" }, expected: null },
    ] })).rejects.toThrow("CONFLICT");
  });
  it("preserves disjoint concurrent changes and rejects stale writes to the same field", async () => {
    const t = setup(); await seed(t);
    await t.mutation(api.storage.commit, { token, writes: [
      { table: "call_session", value: { ...call, state: "HOLD" }, expected: call },
    ] });
    await t.mutation(api.storage.commit, { token, writes: [
      { table: "call_session", value: { ...call, outcome: "done" }, expected: call },
    ] });
    expect(await t.query(api.storage.get, { token, table: "call_session", key: call.id }))
      .toMatchObject({ state: "HOLD", outcome: "done" });
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "call_session", value: { ...call, state: "ENDED" }, expected: call },
    ] })).rejects.toThrow("CONFLICT");
  });
  it("does not let a timeout overwrite an answered request", async () => {
    const t = setup(); await seed(t);
    await t.mutation(api.storage.commit, { token, writes: [{ table: "user_request", value: request, expected: null }] });
    await t.mutation(api.storage.commit, { token, writes: [
      { table: "user_request", value: { ...request, status: "answered" }, expected: request },
    ] });
    await expect(t.mutation(api.storage.commit, { token, writes: [
      { table: "user_request", value: { ...request, status: "timed_out" }, expected: request },
    ] })).rejects.toThrow("CONFLICT");
  });
  it("updates device tokens without erasing the other token", async () => {
    const t = setup();
    const device = { user_id: "u", apns_token: "push", live_activity_token: null };
    await t.mutation(api.storage.commit, { token, writes: [{ table: "device", value: device, expected: null }] });
    await t.mutation(api.storage.commit, { token, writes: [{ table: "device", value: { ...device, live_activity_token: "live" }, expected: device }] });
    expect(await t.query(api.storage.get, { token, table: "device", key: "u" })).toEqual({ ...device, live_activity_token: "live" });
  });
  it("counts successful menu routes atomically", async () => {
    const t = setup();
    await t.mutation(api.storage.saveMenu, { token, company: "test", steps: ["1", "0"] });
    await t.mutation(api.storage.saveMenu, { token, company: "test", steps: ["1", "0"] });
    expect(await t.query(api.storage.menu, { token, company: "test" })).toEqual(["1", "0"]);
    expect(await t.run(ctx => ctx.db.query("menu_path").collect())).toMatchObject([{ success_count: 2 }]);
  });
});

describe("live replay", () => {
  const message = { id: "m1", session_id: call.id, at: call.started_at, type: "caption" as const, data: { text: "[REDACTED]", final: true } };
  it("authenticates reads, deduplicates writes, and preserves insertion order", async () => {
    const t = setup(); await seed(t);
    await t.mutation(api.live.publish, { token, messages: [message, message, { ...message, id: "m2" }] });
    await expect(t.query(api.live.recent, { accessCode: "", call_id: call.id })).rejects.toThrow("UNAUTHORIZED");
    const result = await t.query(api.live.recent, { accessCode, call_id: call.id });
    expect(result.map(m => m.sequence)).toEqual([1, 2]);
    expect(result.map(m => m.id)).toEqual(["m1", "m2"]);
  });
  it("keeps only the latest 400 messages", async () => {
    const t = setup(); await seed(t);
    for (let batch = 0; batch < 9; batch++) {
      await t.mutation(api.live.publish, { token, messages: Array.from({ length: 50 }, (_, i) => ({ ...message, id: `m-${batch * 50 + i}` })) });
    }
    const result = await t.query(api.live.recent, { accessCode, call_id: call.id });
    expect(result).toHaveLength(400);
    expect(result[0].sequence).toBe(51);
    expect(result.at(-1)?.sequence).toBe(450);
  });
  it("expires replay after 24 hours while keeping call records", async () => {
    vi.useFakeTimers();
    const t = setup(); await seed(t);
    await t.mutation(api.live.publish, { token, messages: [message] });
    vi.setSystemTime(Date.now() + 25 * 60 * 60 * 1000);
    expect(await t.mutation(internal.live.expire, {})).toBe(1);
    expect(await t.query(api.live.recent, { accessCode, call_id: call.id })).toEqual([]);
    expect(await t.query(api.storage.get, { token, table: "call_session", key: call.id })).toEqual(call);
  });
});
