// Synthetic cloud test: authenticated subscriptions receive updates and reconnect.
// Run after deploying the functions: node scripts/check_convex_subscription.mjs
import { ConvexClient, ConvexHttpClient } from "convex/browser";
import { api } from "../convex/_generated/api.js";
import { randomUUID } from "node:crypto";
process.loadEnvFile(".env");
const url = process.env.CONVEX_URL;
const token = process.env.CONVEX_BACKEND_TOKEN;
const accessCode = process.env.API_TOKEN;
const http = new ConvexHttpClient(url);
const id = randomUUID().replaceAll("-", "");
const now = new Date().toISOString();
await http.mutation(api.storage.commit, { token, writes: [{ table: "call_session", expected: null, value: {
  id, user_id: "convex-smoke-test", company: "Synthetic subscription test", phone_number: "+15550100100",
  goal: "Verify replay", limits_json: {}, disclosure_text: "Synthetic AI test", state: "ENDED", started_at: now,
  ended_at: now, outcome: "Subscription test", reference_number: null, minutes_saved: 0,
} }] });
async function readUntil(expected) {
  const client = new ConvexClient(url, { webSocketConstructor: WebSocket });
  try {
    return await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Subscription timed out")), 15000);
      client.onUpdate(api.live.recent, { accessCode, call_id: id }, messages => {
        if (messages.length >= expected) { clearTimeout(timeout); resolve(messages); }
      }, error => { clearTimeout(timeout); reject(error); });
    });
  } finally { await client.close(); }
}
const first = readUntil(1);
await http.mutation(api.live.publish, { token, messages: [{ id: randomUUID(), session_id: id, at: now,
  type: "caption", data: { speaker: "agent", text: "Synthetic replay works.", final: true } }] });
const live = await first;
const reconnect = await readUntil(1);
if (JSON.stringify(live) !== JSON.stringify(reconnect)) throw new Error("Reconnect replay differed");
console.log(JSON.stringify({ status: "passed", live_updates: live.length, replay_on_reconnect: reconnect.length }));
