/* call4me demo dashboard. Plain JS, no build step.
 *
 * Served by the backend at /dashboard, so every API call is a relative,
 * same-origin path ("../calls" from /dashboard/ → /calls). The WebSocket URL is
 * built from `location` for the same reason (ws:// locally, wss:// on Azure).
 *
 * Privacy (CLAUDE.md section 2): ask_user answers and pre-call info values are
 * read from the inputs, sent once, and the inputs are cleared. They are never
 * kept in localStorage, the URL, or the console. (Company, number, rules and
 * the AI introduction are remembered in localStorage for the next call.)
 */
"use strict";

// --------------------------------------------------------------------------
// Paths and small helpers
// --------------------------------------------------------------------------

// Resolve API paths against the site root, not /dashboard/, so the page works
// whether it is opened as /dashboard or /dashboard/ or /dashboard/index.html.
const API_ROOT = (() => {
  const p = location.pathname;
  const i = p.indexOf("/dashboard");
  return i >= 0 ? p.slice(0, i) : "";
})();
const api = (path) => API_ROOT + path;

function wsUrl(path) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // Browsers can't set headers on WebSockets, so the access code rides in the URL.
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${location.host}${API_ROOT}${path}${q}`;
}

// Access code: the server's API_TOKEN (servers without one ignore it). It is a
// shared server password, not user data, so it is remembered in localStorage.
const TOKEN_KEY = "call4me.token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function askForToken() {
  const entered = window.prompt("Enter the call4me access code:");
  if (!entered || !entered.trim()) return false;
  try { localStorage.setItem(TOKEN_KEY, entered.trim()); } catch { /* private mode */ }
  return true;
}

const $ = (id) => document.getElementById(id);

/** Create an element; `text` is always set via textContent (no HTML injection). */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

class HttpError extends Error {
  constructor(status, detail) {
    super(`HTTP ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function http(method, path, body, retried = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const token = getToken();
  if (token) opts.headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(api(path), opts);
  if (res.status === 401 && !retried && askForToken()) {
    return http(method, path, body, true);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : res.statusText;
    throw new HttpError(res.status, detail);
  }
  return data;
}

/** Backend datetimes may be naive (stored as UTC without an offset). Treat those as UTC. */
function parseTime(v) {
  if (!v) return null;
  if (v instanceof Date) return v;
  const s = String(v);
  const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(s);
  const d = new Date(hasZone ? s : s + "Z");
  return isNaN(d) ? null : d;
}

function fmtDuration(ms) {
  if (ms == null || ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function fmtClock(d) {
  d = parseTime(d);
  return d ? d.toLocaleTimeString([], { hour12: false }) : "";
}

function fmtNum(v, digits = 2) {
  return v == null || isNaN(v) ? "–" : Number(v).toFixed(digits);
}

function setMsg(node, text, kind) {
  node.textContent = text || "";
  node.className = "form-msg" + (kind ? " " + kind : "");
}

/** Compact one-line JSON for detail cells; drops keys already shown elsewhere. */
function compact(obj, skip = []) {
  if (!obj || typeof obj !== "object") return obj == null ? "" : String(obj);
  const out = {};
  for (const [k, v] of Object.entries(obj)) if (!skip.includes(k) && v !== null && v !== "") out[k] = v;
  const s = JSON.stringify(out);
  return s === "{}" ? "" : s;
}

/** Append text to `node`, wrapping every [REDACTED] in a highlight: the demo's privacy proof. */
function appendRedactAware(node, text) {
  const parts = String(text).split("[REDACTED]");
  parts.forEach((p, i) => {
    if (p) node.appendChild(document.createTextNode(p));
    if (i < parts.length - 1) node.appendChild(el("mark", "redacted", "[REDACTED]"));
  });
}

// --------------------------------------------------------------------------
// State machine (CLAUDE.md section 6)
// --------------------------------------------------------------------------

// Main path, in the order the demo walks through it. "⇄" pairs bounce back and forth.
const MAIN_PATH = [
  "DIALING", "→", "MENU", "→", "HOLD", "⇄", "RECORDED_MESSAGE", "→",
  "HUMAN_SUSPECTED", "→", "HUMAN_CONFIRMED", "→", "IN_CONVERSATION", "⇄", "WAITING_ON_USER",
  "|", "TAKEOVER", "ENDED",
];
const SIDE_STATES = ["CALLBACK_OFFERED", "VOICEMAIL", "FAILED"];
const HOLD_STATES = new Set(["HOLD", "RECORDED_MESSAGE"]);
const TERMINAL = new Set(["ENDED", "FAILED", "VOICEMAIL"]);
const ALERT_STATES = new Set(["WAITING_ON_USER", "TAKEOVER"]);

// Plain words for the banner (short UI text; English may be a second language).
const STATE_TEXT = {
  DIALING: ["Calling", "Dialing the company."],
  MENU: ["Phone menu", "Choosing menu options."],
  HOLD: ["On hold", "Waiting for a person. You can leave."],
  RECORDED_MESSAGE: ["Recorded message", "Still waiting for a person."],
  CALLBACK_OFFERED: ["Callback offered", "The company offered to call back."],
  HUMAN_SUSPECTED: ["Person answered?", "Greeting played. Checking it is a person."],
  HUMAN_CONFIRMED: ["Person answered", "A person is on the line."],
  IN_CONVERSATION: ["Talking with rep", "The agent is talking for you."],
  WAITING_ON_USER: ["Needs your input", "Please answer the question below."],
  TAKEOVER: ["You are talking", "Type below. The agent is quiet."],
  VOICEMAIL: ["Voicemail", "No one answered."],
  FAILED: ["Call failed", "The call could not continue."],
  ENDED: ["Call ended", "See the summary below."],
};

// --------------------------------------------------------------------------
// Per-call view state. Reset whenever we join a different call.
// --------------------------------------------------------------------------

let view = null;

function freshView(callId) {
  return {
    callId,
    session: null,
    state: null,
    visited: new Set(),
    startedAt: null,
    endedAt: null,
    holdMs: 0,             // finished hold segments
    holdSince: null,       // Date when the current hold segment began
    feed: [],              // events shown in the feed (see addEvent)
    replaying: false,      // true while GET /calls/{id} is in flight for a replay
    liveQueue: [],         // live messages that arrived during a replay
    seenAuditDenials: new Set(),
    partials: {},          // speaker -> caption node still being spoken
    counts: { jev: 0, menu: 0, asks: 0, deny: 0 },
    greetS: null,
    ended: false,
    asks: new Map(),       // request_id -> card node
  };
}

// --------------------------------------------------------------------------
// Rendering: banner, path, timers, stats
// --------------------------------------------------------------------------

function renderPath() {
  const ol = $("state-path");
  ol.textContent = "";
  for (const item of MAIN_PATH) {
    if (item === "→" || item === "⇄") { ol.appendChild(el("li", "sep", item)); continue; }
    if (item === "|") { ol.appendChild(el("li", "sep", "·")); continue; }
    ol.appendChild(stateChip(item, false));
  }
  ol.appendChild(el("li", "sep", "·"));
  for (const s of SIDE_STATES) ol.appendChild(stateChip(s, true));
}

function stateChip(s, side) {
  const li = el("li", "st" + (side ? " side" : ""), s.replace(/_/g, " "));
  li.dataset.state = s;
  if (view && view.visited.has(s)) li.classList.add("visited");
  if (view && view.state === s) {
    li.classList.add("current");
    if (ALERT_STATES.has(s)) li.classList.add("alert");
  }
  return li;
}

function renderBanner() {
  const b = $("banner");
  const s = view && view.state;
  b.dataset.state = s || "NONE";
  const [title, sub] = STATE_TEXT[s] || [s || "No call yet", view ? "" : "Start or join a call."];
  $("banner-state").textContent = title;
  if (!view || !view.lastSummary) $("banner-sub").textContent = sub;
  document.title = (s === "WAITING_ON_USER" ? "(!) Needs your input — " : "") + "call4me dashboard";
  renderPath();
  renderControls();
}

/** A visual-only alert (CLAUDE.md rule 7): flash the banner. Never a sound. */
function flashBanner() {
  const b = $("banner");
  b.classList.remove("flash");
  void b.offsetWidth; // restart the CSS animation
  b.classList.add("flash");
}

function tickTimers() {
  if (!view || !view.startedAt) {
    $("call-timer").textContent = "0:00";
    $("hold-timer").textContent = "0:00";
    return;
  }
  const now = view.endedAt || new Date();
  $("call-timer").textContent = fmtDuration(now - view.startedAt);
  const live = view.holdSince ? now - view.holdSince : 0;
  $("hold-timer").textContent = fmtDuration(view.holdMs + live);
}
setInterval(tickTimers, 500);

function renderStats() {
  const c = view ? view.counts : { jev: 0, menu: 0, asks: 0, deny: 0 };
  $("stat-jev").textContent = c.jev;
  $("stat-menu").textContent = c.menu;
  $("stat-asks").textContent = c.asks;
  $("stat-deny").textContent = c.deny;
  $("stat-greet").textContent = view && view.greetS != null ? `${fmtNum(view.greetS, 2)} s` : "–";
}

/** Apply a state change; `at` keeps the hold timer correct when replaying a timeline. */
function applyState(to, at, { quiet = false } = {}) {
  if (!to || !view) return;
  at = parseTime(at) || new Date();
  const from = view.state;
  // Hold time accumulates across HOLD ⇄ RECORDED_MESSAGE and pauses outside them.
  if (HOLD_STATES.has(to) && !view.holdSince) view.holdSince = at;
  if (!HOLD_STATES.has(to) && view.holdSince) {
    view.holdMs += Math.max(0, at - view.holdSince);
    view.holdSince = null;
  }
  view.state = to;
  view.visited.add(to);
  if (TERMINAL.has(to)) {
    view.endedAt = view.endedAt || at;
  }
  // Leaving WAITING_ON_USER means the question was answered, timed out, or replaced by takeover.
  // `quiet` is for replays: old transitions must not clear a question pending now.
  if (!quiet) {
    if (from === "WAITING_ON_USER" && to !== "WAITING_ON_USER") clearAsks();
    if (to === "WAITING_ON_USER" && from !== to) flashBanner();
  }
  renderBanner();
}

// --------------------------------------------------------------------------
// Captions
// --------------------------------------------------------------------------

const SPEAKER_LABEL = { remote: "Rep", agent: "Agent", user: "You" };

function captionsAtBottom() {
  const c = $("captions");
  return c.scrollHeight - c.scrollTop - c.clientHeight < 60;
}

function addCaption(data) {
  const speaker = String(data.speaker || "remote").toLowerCase();
  const text = data.text || "";
  // Missing `final` means final: agent captions are only published after speaking.
  const final = data.final !== undefined ? !!data.final
    : data.is_final !== undefined ? !!data.is_final : true;
  const box = $("captions");
  const stick = captionsAtBottom();

  // Partials for a speaker overwrite one line in place until the final arrives.
  let row = view.partials[speaker];
  if (!row) {
    row = el("div", "cap");
    row.appendChild(el("span", "who " + (SPEAKER_LABEL[speaker] ? speaker : "sys"), SPEAKER_LABEL[speaker] || speaker));
    row.appendChild(el("span", "txt"));
    box.appendChild(row);
  }
  const txt = row.querySelector(".txt");
  txt.textContent = "";
  appendRedactAware(txt, text);
  row.classList.toggle("partial", !final);
  if (final) delete view.partials[speaker];
  else view.partials[speaker] = row;

  while (box.children.length > 300) box.removeChild(box.firstChild);
  if (stick) box.scrollTop = box.scrollHeight;
}

function addSummaryLine(text) {
  const box = $("captions");
  const stick = captionsAtBottom();
  const row = el("div", "cap summary");
  row.appendChild(el("span", "who sys", "Summary"));
  const t = el("span", "txt");
  appendRedactAware(t, text);
  row.appendChild(t);
  box.appendChild(row);
  if (stick) box.scrollTop = box.scrollHeight;
  view.lastSummary = text;
  $("banner-sub").textContent = text;
}

// --------------------------------------------------------------------------
// Event feed
// --------------------------------------------------------------------------

/**
 * Normalize live `event`/`state` messages and timeline rows into one shape so
 * the feed and counters are built the same way whether the event arrived live
 * or was replayed after a (re)connect.
 */
function normEvent(kind, detail, confidence, at) {
  return { kind, detail: detail || {}, confidence: confidence ?? null, at: parseTime(at) || new Date() };
}

// Same event via WS and via GET /calls/{id} has different `at` values (publish
// time vs. event time), so matching uses content, not the timestamp.
function eventKey(ev) {
  return ev.kind + "|" + compact(ev.detail, ["at"]);
}

function describeEvent(ev) {
  const d = ev.detail;
  const conf = ev.confidence ?? d.probability ?? d.confidence ?? null;
  switch (ev.kind) {
    case "transition":
      return { cls: "state", k: "State", text: `${d.from || "–"} → ${d.to}${d.reason ? " · " + d.reason : ""}`, conf };
    case "jev_decision":
      return {
        cls: "", k: `Jev #${d.count ?? ""}`,
        text: `${d.question}: ${d.choice}${d.fallback ? " (fallback)" : ""}${d.reason ? " · " + d.reason : ""}`,
        conf,
      };
    case "menu_action": {
      const what = d.action === "dtmf" ? `pressed ${d.value}` : `said "${d.value}"`;
      return { cls: "", k: "Menu", text: `${what}${d.reason ? " · " + d.reason : ""}`, conf };
    }
    case "greeting_played": {
      const s = Number(d.time_to_greet_s);
      return {
        cls: s <= 1.5 ? "good" : "warn", k: "Greeting",
        text: `played ${fmtNum(s, 2)} s after the person spoke (target ≤ 1.5 s)${d.source ? " · " + d.source : ""}`,
      };
    }
    case "guard_denied":
      return { cls: "deny", k: "Guard blocked", text: [d.reason, d.attempt || d.action].filter(Boolean).join(" · ") };
    case "takeover_suggested":
      return { cls: "warn", k: "Take over?", text: d.reason || "The agent asks you to take over." };
    case "takeover":
      return { cls: "warn", k: "You", text: "took over the call" };
    case "handback":
      return { cls: "", k: "You", text: "handed the call back to the agent" };
    case "cancel":
      return { cls: "deny", k: "You", text: "cancelled the call" };
    case "callback_offer":
      return { cls: "", k: "Callback", text: `${d.decision} · ${d.reason || ""}` };
    case "callback_number":
      return { cls: "", k: "Callback", text: "gave the agent's own number" };
    case "max_hold":
      return { cls: "warn", k: "Long hold", text: `on hold ${d.hold_minutes} min · asked you to keep waiting` };
    case "hold_continue":
      return { cls: "", k: "Hold", text: d.keep_waiting ? "you chose to keep waiting" : "you stopped waiting" };
    case "ask_user":
      return { cls: "warn", k: "Asked you", text: `${d.question || ""}${d.sensitivity ? " (" + d.sensitivity + ")" : ""}` };
    default:
      return { cls: "", k: ev.kind, text: compact(d, ["kind"]), conf };
  }
}

/**
 * The feed is a list of events, each tagged by source:
 *  - "stored": also in GET /calls/{id} timeline (transitions, engine events,
 *    user actions). A replay replaces all of these with the stored copy.
 *  - "local": only known to this page (ask_user prompts, denials read from the
 *    audit log). A replay keeps these.
 * Replacing instead of de-duping matters: HOLD ⇄ RECORDED_MESSAGE can repeat
 * with identical content, and every repeat must stay visible and counted.
 */
function addEvent(ev, { live = false, src = "stored" } = {}) {
  ev.src = src;
  view.feed.push(ev);
  countEvent(ev);
  if (ev.kind === "takeover_suggested" && live) flashBanner();
  renderStats();
  insertEventRow(ev, live);
}

/** Counters come from the feed so a replay after reconnect rebuilds them exactly. */
function countEvent(ev) {
  if (ev.kind === "jev_decision") view.counts.jev += 1;
  if (ev.kind === "menu_action") view.counts.menu += 1;
  if (ev.kind === "guard_denied") view.counts.deny += 1;
  if (ev.kind === "ask_user") view.counts.asks += 1;
  if (ev.kind === "greeting_played" && ev.detail.time_to_greet_s != null) {
    view.greetS = Number(ev.detail.time_to_greet_s);
  }
}

function insertEventRow(ev, live) {
  const info = describeEvent(ev);
  const li = el("li", info.cls + (live ? " new" : ""));
  li.appendChild(el("span", "t", fmtClock(ev.at)));
  const body = el("span");
  body.appendChild(el("span", "k", info.k));
  appendRedactAware(body, info.text || "");
  if (info.conf != null && !isNaN(info.conf)) {
    body.appendChild(document.createTextNode(` ${Math.round(info.conf * 100)}%`));
    const bar = el("span", "conf");
    const fill = el("i");
    fill.style.width = `${Math.max(0, Math.min(1, info.conf)) * 100}%`;
    bar.appendChild(fill);
    body.appendChild(bar);
  }
  li.appendChild(body);

  // Keep the feed in time order even when local rows interleave with stored ones.
  const list = $("events");
  li.dataset.at = String(ev.at.getTime());
  let before = null;
  for (const other of list.children) {
    if (Number(other.dataset.at) > ev.at.getTime()) { before = other; break; }
  }
  const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  list.insertBefore(li, before);
  if (stick && !before) list.scrollTop = list.scrollHeight;
}

/**
 * Swap live copies of stored events for the timeline copy, and redraw. Some
 * live events are never written to the timeline (e.g. takeover_suggested is
 * only published), so a live event with no stored match is kept as local.
 */
function rebuildFeed(storedEvents) {
  const pending = new Map();
  for (const ev of storedEvents) {
    const k = eventKey(ev);
    pending.set(k, (pending.get(k) || 0) + 1);
  }
  const local = view.feed.filter((e) => {
    if (e.src === "local") return true;
    const k = eventKey(e);
    if (pending.get(k)) { pending.set(k, pending.get(k) - 1); return false; }
    e.src = "local";
    return true;
  });
  view.feed = [];
  view.counts = { jev: 0, menu: 0, asks: 0, deny: 0 };
  view.greetS = null;
  $("events").textContent = "";
  const all = local.concat(storedEvents.map((e) => Object.assign(e, { src: "stored" })));
  all.sort((a, b) => a.at - b.at);
  for (const ev of all) {
    view.feed.push(ev);
    countEvent(ev);
    insertEventRow(ev, false);
  }
  const list = $("events");
  list.scrollTop = list.scrollHeight;
  renderStats();
}

// --------------------------------------------------------------------------
// ask_user (build step 11 test page)
// --------------------------------------------------------------------------

function showAsk(data) {
  const id = data.request_id;
  if (!id || view.asks.has(id)) return;
  addEvent(normEvent("ask_user", { question: data.question, sensitivity: data.sensitivity }),
    { live: true, src: "local" });

  const card = el("div", "ask");
  const meta = el("div", "ask-meta");
  meta.appendChild(document.createTextNode("Needs your input"));
  if (data.sensitivity) {
    meta.appendChild(document.createTextNode(" · sensitivity: "));
    meta.appendChild(el("span", data.sensitivity === "high" ? "hi" : "", data.sensitivity));
  }
  const countdown = el("span");
  meta.appendChild(countdown);
  card.appendChild(meta);
  card.appendChild(el("div", "ask-q", data.question || "(no question text)"));

  const form = el("form");
  form.autocomplete = "off";
  const input = el("input");
  // Hidden by default: the dashboard is on a projector and the answer may be a PIN.
  input.type = "password";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Type your answer";
  input.maxLength = 500;
  const showLbl = el("label", "check show");
  const show = el("input");
  show.type = "checkbox";
  show.addEventListener("change", () => { input.type = show.checked ? "text" : "password"; });
  showLbl.appendChild(show);
  showLbl.appendChild(document.createTextNode("Show"));
  const btn = el("button", "warn", "Send");
  btn.type = "submit";
  form.append(input, showLbl, btn);
  card.appendChild(form);
  const msg = el("p", "form-msg");
  card.appendChild(msg);

  if (data.sensitivity === "high") {
    card.appendChild(el("p", "form-msg err",
      "High-sensitivity item: the agent will not relay this. Use Take over and type it yourself."));
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!input.value) return;
    btn.disabled = true;
    // Read and clear in one step: the answer never lives anywhere but this request body.
    const body = { answer: input.value };
    input.value = "";
    try {
      const res = await http("POST", `/requests/${encodeURIComponent(id)}/answer`, body);
      setMsg(msg, res && res.detail === "delivered" ? "Sent to the agent." : "Recorded.", "ok");
      setTimeout(() => removeAsk(id), 1200);
    } catch (err) {
      if (err.status === 409 || err.status === 404) {
        setMsg(msg, "This question is closed.", "err");
        setTimeout(() => removeAsk(id), 1500);
      } else {
        setMsg(msg, "Could not send. Try again.", "err");
        btn.disabled = false;
      }
    }
  });

  // Visible countdown so the user knows the rep is waiting (no sound alerts).
  const deadline = data.timeout_s ? Date.now() + data.timeout_s * 1000 : null;
  card._timer = setInterval(() => {
    if (!deadline) return;
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    countdown.textContent = ` · ${left}s before the agent asks the rep to hold`;
  }, 500);

  view.asks.set(id, card);
  $("asks").appendChild(card);
  flashBanner();
  input.focus();
}

function removeAsk(id) {
  if (!view) return;
  const card = view.asks.get(id);
  if (!card) return;
  clearInterval(card._timer);
  card.remove();
  view.asks.delete(id);
}

function clearAsks() {
  if (!view) return;
  for (const id of [...view.asks.keys()]) removeAsk(id);
}

// --------------------------------------------------------------------------
// End of call
// --------------------------------------------------------------------------

function renderSummary(data) {
  const s = view.session || {};
  const summary = data.summary ?? s.outcome;
  const ref = data.reference_number ?? s.reference_number;
  const saved = data.minutes_saved ?? s.minutes_saved;
  if (summary == null && ref == null && !view.ended) return;
  $("summary-card").hidden = false;
  const t = $("sum-text");
  t.textContent = "";
  appendRedactAware(t, summary || "No summary.");
  $("sum-ref").textContent = ref || "–";
  // Only show minutes saved when the backend computed it; never invent a number.
  $("sum-saved").textContent = saved != null ? `${fmtNum(saved, 0)} min` : "not reported";
  const start = view.startedAt;
  const end = view.endedAt || parseTime(s.ended_at);
  $("sum-len").textContent = start && end ? fmtDuration(end - start) : "–";
}

async function onEnded(data) {
  view.ended = true;
  if (!TERMINAL.has(view.state)) applyState("ENDED", new Date());
  clearAsks();
  renderSummary(data || {});
  // Pull the stored record: final session fields (minutes_saved) and the full audit trail.
  await loadCall({ replay: true });
  renderSummary(data || {});
  await loadAudit();
  stopAuditPoll();
  // Nothing more will be published for this call; drop the socket instead of holding it open.
  closeWs();
  setConn("idle", "Call ended");
  loadRecentCalls();
}

// --------------------------------------------------------------------------
// Controls: takeover / handback / cancel / type-to-talk
// --------------------------------------------------------------------------

function renderControls() {
  const active = !!view && !view.ended && !TERMINAL.has(view.state);
  const takeover = !!view && view.state === "TAKEOVER";
  $("btn-takeover").disabled = !active || takeover;
  $("btn-handback").disabled = !active || !takeover;
  $("btn-cancel").disabled = !active;
  $("type-text").disabled = !active;
  $("type-send").disabled = !active;
  document.querySelector(".controls").classList.toggle("takeover", takeover);
}

async function control(action, label) {
  if (!view) return;
  const msg = $("control-msg");
  try {
    await http("POST", `/calls/${encodeURIComponent(view.callId)}/${action}`);
    setMsg(msg, `${label} requested.`, "ok");
    if (action === "takeover") $("type-text").focus();
  } catch (err) {
    setMsg(msg, `${label} failed: ${err.message}`, "err");
  }
}

$("btn-takeover").addEventListener("click", () => control("takeover", "Takeover"));
$("btn-handback").addEventListener("click", () => control("handback", "Hand back"));
$("btn-cancel").addEventListener("click", () => {
  if (confirm("Cancel this call? The agent will hang up.")) control("cancel", "Cancel");
});

$("type-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("type-text");
  if (!view || !input.value.trim()) return;
  // Typed text may contain private info: send it, clear the box, and show only that it went.
  const body = { text: input.value };
  input.value = "";
  try {
    await http("POST", `/calls/${encodeURIComponent(view.callId)}/type`, body);
    setMsg($("control-msg"), `Sent ${body.text.length} characters to speak.`, "ok");
  } catch (err) {
    setMsg($("control-msg"), `Could not send: ${err.message}`, "err");
  }
});

// --------------------------------------------------------------------------
// Live WebSocket with reconnect
// --------------------------------------------------------------------------

let ws = null;
let wsRetry = 0;
let wsTimer = null;

function setConn(status, text) {
  const c = $("conn");
  c.dataset.status = status;
  c.textContent = text;
}

function closeWs() {
  clearTimeout(wsTimer);
  if (ws) {
    ws.onclose = null; // a deliberate close must not trigger a reconnect
    ws.close();
    ws = null;
  }
}

function connectWs() {
  if (!view) return;
  closeWs();
  const callId = view.callId;
  setConn("connecting", "Connecting…");
  const sock = new WebSocket(wsUrl(`/calls/${encodeURIComponent(callId)}/live`));
  ws = sock;

  sock.onopen = async () => {
    wsRetry = 0;
    setConn("open", "Live");
    // Messages published while we were disconnected are gone (the hub does not
    // buffer), so replay the stored timeline to fill the gap.
    await loadCall({ replay: true });
  };
  sock.onmessage = (e) => {
    if (!view || view.callId !== callId) return;
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handleLive(msg);
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    if (!view || view.callId !== callId) return;
    if (view.ended) { setConn("idle", "Call ended"); return; }
    // Backoff: 1 s, 2 s, 4 s … capped at 10 s, so a server restart recovers quickly.
    const delay = Math.min(10000, 1000 * 2 ** wsRetry++);
    setConn("closed", `Reconnecting in ${Math.round(delay / 1000)}s`);
    wsTimer = setTimeout(connectWs, delay);
  };
  sock.onerror = () => { /* onclose follows and handles the retry */ };
}

function handleLive(msg) {
  // During a replay the feed is about to be replaced; hold messages until it is done.
  if (view.replaying) { view.liveQueue.push(msg); return; }
  const d = msg.data || {};
  switch (msg.type) {
    case "caption":
      addCaption(d);
      break;
    case "summary":
      if (d.text) addSummaryLine(d.text);
      break;
    case "state":
      addEvent(normEvent("transition", { from: d.from, to: d.to, reason: d.reason }, d.confidence, msg.at), { live: true });
      applyState(d.to, msg.at);
      break;
    case "ask_user":
      showAsk(d);
      break;
    case "event": {
      const { kind, ...rest } = d;
      // Engine events put confidence in the detail (jev: probability); pass it through.
      addEvent(normEvent(kind || "event", rest, rest.confidence ?? rest.probability, rest.at || msg.at), { live: true });
      if (kind === "takeover" && view.state !== "TAKEOVER") setMsg($("control-msg"), "Takeover in progress.", "ok");
      break;
    }
    case "ended":
      onEnded(d);
      break;
    default:
      break;
  }
}

// --------------------------------------------------------------------------
// Loading a call: GET /calls/{id} (session + timeline)
// --------------------------------------------------------------------------

async function loadCall({ replay }) {
  if (!view) return;
  const callId = view.callId;
  if (replay) view.replaying = true;
  let data;
  try {
    data = await http("GET", `/calls/${encodeURIComponent(callId)}`);
  } catch (err) {
    $("timeline-msg").textContent = err.status === 404 ? "Call not found." : `Could not load call: ${err.message}`;
    if (replay) finishReplay([]);
    return;
  }
  if (!view || view.callId !== callId) return;
  const s = data.session || {};
  view.session = s;
  view.startedAt = parseTime(s.started_at) || view.startedAt;
  if (s.ended_at) view.endedAt = parseTime(s.ended_at);
  const timeline = data.timeline || [];
  renderTimeline(timeline);

  if (replay) {
    // Rebuild visited states and hold time from the stored transitions, in order.
    view.visited = new Set();
    view.holdMs = 0;
    view.holdSince = null;
    view.state = null;
    const stored = [];
    for (const e of timeline) {
      const detail = e.detail || {};
      if (e.kind === "transition" && e.to_state) {
        applyState(e.to_state, e.at, { quiet: true });
        stored.push(normEvent("transition",
          { from: e.from_state, to: e.to_state, reason: detail.reason }, e.confidence, e.at));
      } else {
        stored.push(normEvent(e.kind, detail, e.confidence ?? detail.probability, e.at));
      }
    }
    // The session row is the source of truth for the current state.
    if (s.state && s.state !== view.state) applyState(s.state, view.endedAt || new Date(), { quiet: true });
    if (view.endedAt && view.holdSince) {
      view.holdMs += Math.max(0, view.endedAt - view.holdSince);
      view.holdSince = null;
    }
    rebuildFeed(stored);
    finishReplay(stored);
  }
  if (TERMINAL.has(s.state)) {
    view.ended = true;
    renderSummary({});
  }
  renderBanner();
  tickTimers();
}

/**
 * Apply live messages that arrived while the replay fetch was in flight. Ones
 * already in the timeline snapshot are skipped (matched by content, one for one).
 */
function finishReplay(stored) {
  const pending = new Map();
  for (const ev of stored) {
    const k = eventKey(ev);
    pending.set(k, (pending.get(k) || 0) + 1);
  }
  const queue = view.liveQueue;
  view.liveQueue = [];
  view.replaying = false;
  for (const msg of queue) {
    const k = liveEventKey(msg);
    if (k && pending.get(k)) {
      pending.set(k, pending.get(k) - 1);
      if (msg.type === "state") applyState(msg.data.to, msg.at, { quiet: true });
      continue;
    }
    handleLive(msg);
  }
}

/** The feed key a live message would produce, or null if it is not a stored event. */
function liveEventKey(msg) {
  const d = msg.data || {};
  if (msg.type === "state") return eventKey(normEvent("transition", { from: d.from, to: d.to, reason: d.reason }));
  if (msg.type === "event") {
    const { kind, ...rest } = d;
    return eventKey(normEvent(kind || "event", rest));
  }
  return null;
}

function renderTimeline(rows) {
  const tb = document.querySelector("#timeline-table tbody");
  tb.textContent = "";
  for (const e of rows) {
    const tr = el("tr");
    const detail = e.detail || {};
    if (e.kind === "guard_denied") tr.className = "deny";
    tr.appendChild(el("td", "mono", fmtClock(e.at)));
    tr.appendChild(el("td", "", e.kind));
    const st = e.from_state && e.to_state && e.from_state !== e.to_state
      ? `${e.from_state} → ${e.to_state}` : (e.to_state || e.from_state || "");
    tr.appendChild(el("td", "", st));
    tr.appendChild(el("td", "num", e.confidence != null ? fmtNum(e.confidence, 2) : ""));
    const td = el("td", "evidence");
    appendRedactAware(td, compact(detail));
    tr.appendChild(td);
    tb.appendChild(tr);
  }
  $("timeline-msg").textContent = rows.length ? "" : "No timeline events yet.";
}

// --------------------------------------------------------------------------
// Audit log: GET /calls/{id}/audit
// --------------------------------------------------------------------------

let auditRows = [];
let auditTimer = null;

async function loadAudit() {
  if (!view) return;
  const callId = view.callId;
  try {
    auditRows = (await http("GET", `/calls/${encodeURIComponent(callId)}/audit`)) || [];
  } catch (err) {
    auditRows = [];
    $("audit-msg").textContent = err.status === 404
      ? "Audit endpoint not available (GET /calls/{id}/audit)."
      : `Could not load audit log: ${err.message}`;
    renderAudit();
    return false;
  }
  if (!view || view.callId !== callId) return false;
  $("audit-msg").textContent = auditRows.length ? "" : "No audit entries yet.";
  renderAudit();
  // There is no live "guard denied" message today, so surface denials from the
  // audit trail in the event feed too: the demo's "guard blocks" moment needs it.
  for (const a of auditRows) {
    if (a.allowed !== false || a.actor !== "guard") continue;
    const key = `${a.at}|${a.action}|${a.reason}`;
    if (view.seenAuditDenials.has(key)) continue;
    view.seenAuditDenials.add(key);
    // If a live guard_denied event already showed this denial, don't count it twice.
    const at = parseTime(a.at);
    const shown = view.feed.some((e) => e.kind === "guard_denied" && e.detail.reason === a.reason
      && at && Math.abs(e.at - at) < 10000);
    if (shown) continue;
    const ev = a.evidence || {};
    addEvent(normEvent("guard_denied",
      { reason: a.reason, attempt: ev.attempt || a.action, actor: a.actor }, null, a.at),
      { live: true, src: "local" });
  }
  return true;
}

function renderAudit() {
  const tb = document.querySelector("#audit-table tbody");
  tb.textContent = "";
  const denialsOnly = $("audit-denials-only").checked;
  for (const a of auditRows) {
    if (denialsOnly && a.allowed !== false) continue;
    const tr = el("tr", a.allowed === false ? "deny" : "");
    tr.appendChild(el("td", "mono", fmtClock(a.at)));
    tr.appendChild(el("td", "", a.actor));
    tr.appendChild(el("td", "", a.action));
    tr.appendChild(el("td", "", a.allowed === false ? "DENIED" : "yes"));
    const reason = el("td");
    appendRedactAware(reason, a.reason || "");
    tr.appendChild(reason);
    const ev = el("td", "evidence");
    appendRedactAware(ev, a.evidence ? JSON.stringify(a.evidence) : "");
    tr.appendChild(ev);
    tb.appendChild(tr);
  }
}
$("audit-denials-only").addEventListener("change", renderAudit);

// Poll the audit log while a call is live (every 4 s) so guard denials show up
// within seconds; stop once the call ends or if the endpoint is missing.
function startAuditPoll() {
  stopAuditPoll();
  auditTimer = setInterval(async () => {
    if (!view || view.ended) { stopAuditPoll(); return; }
    const ok = await loadAudit();
    if (ok === false) stopAuditPoll();
  }, 4000);
}
function stopAuditPoll() {
  clearInterval(auditTimer);
  auditTimer = null;
}

$("refresh-records").addEventListener("click", async () => {
  if (!view) return;
  await loadCall({ replay: false });
  await loadAudit();
});

// --------------------------------------------------------------------------
// Joining a call
// --------------------------------------------------------------------------

function resetLiveUi() {
  $("captions").textContent = "";
  $("events").textContent = "";
  $("asks").textContent = "";
  $("summary-card").hidden = true;
  $("control-msg").textContent = "";
  document.querySelector("#timeline-table tbody").textContent = "";
  document.querySelector("#audit-table tbody").textContent = "";
  auditRows = [];
}

async function joinCall(callId) {
  callId = String(callId || "").trim();
  if (!callId) return;
  closeWs();
  stopAuditPoll();
  clearAsks();
  view = freshView(callId);
  resetLiveUi();
  renderStats();
  renderBanner();
  // Keep the id in the URL so a page reload re-joins the same call.
  history.replaceState(null, "", `#call=${encodeURIComponent(callId)}`);
  for (const li of document.querySelectorAll("#recent-calls li")) {
    li.classList.toggle("active", li.dataset.id === callId);
  }
  await loadCall({ replay: true });
  if (!view || view.callId !== callId) return;
  await loadAudit();
  if (view.ended) {
    setConn("idle", "Call ended");
  } else {
    connectWs();
    startAuditPoll();
  }
}

$("join-form").addEventListener("submit", (e) => {
  e.preventDefault();
  joinCall($("join-id").value);
  $("join-id").value = "";
});

// --------------------------------------------------------------------------
// Recent calls: GET /calls
// --------------------------------------------------------------------------

async function loadRecentCalls() {
  const ul = $("recent-calls");
  let calls;
  try {
    calls = await http("GET", "/calls");
  } catch (err) {
    ul.textContent = "";
    ul.appendChild(el("li", "muted", err.status === 404 || err.status === 405
      ? "List endpoint not available yet." : "Could not load calls."));
    return;
  }
  ul.textContent = "";
  if (!calls || !calls.length) {
    ul.appendChild(el("li", "muted", "No calls yet."));
    return;
  }
  for (const c of calls.slice(0, 25)) {
    const li = el("li");
    li.dataset.id = c.id;
    if (view && view.callId === c.id) li.classList.add("active");
    const top = el("div", "r-top");
    top.appendChild(el("span", "", c.company));
    top.appendChild(el("span", "muted", c.state));
    li.appendChild(top);
    li.appendChild(el("div", "r-goal", `${fmtClock(c.started_at)} · ${c.goal}`));
    li.title = c.id;
    li.addEventListener("click", () => joinCall(c.id));
    ul.appendChild(li);
  }
}
$("refresh-calls").addEventListener("click", loadRecentCalls);
setInterval(loadRecentCalls, 15000);

// --------------------------------------------------------------------------
// Start-call form: GET /companies?q= and POST /calls
// --------------------------------------------------------------------------

let companies = [];
let companyTimer = null;

async function searchCompanies(q) {
  try {
    companies = (await http("GET", `/companies?q=${encodeURIComponent(q)}`)) || [];
  } catch {
    companies = [];
  }
  const dl = $("company-list");
  dl.textContent = "";
  for (const c of companies) {
    const opt = el("option");
    opt.value = c.name;
    opt.label = c.note ? `${c.name} — ${c.note}` : c.name;
    dl.appendChild(opt);
  }
  fillPhoneFromCompany();
}

/** Picking a directory company fills its number (the fake line's number comes from settings). */
function fillPhoneFromCompany() {
  const name = $("company").value.trim();
  const match = companies.find((c) => c.name === name);
  if (match && match.phone_number) $("phone").value = match.phone_number;
}

$("company").addEventListener("input", () => {
  clearTimeout(companyTimer);
  companyTimer = setTimeout(() => searchCompanies($("company").value.trim()), 200);
  fillPhoneFromCompany();
});

const DEFAULT_DISCLOSURE =
  "Hi, I'm an AI assistant calling on behalf of my client, who is Deaf. " +
  "They are following this call by text and can join if needed.";

// Settings worth keeping between calls (never the info values: those live only in
// the inputs until the call starts, then the inputs are cleared).
const PREFS_KEY = "call4me.prefs";
const PREF_FIELDS = ["company", "phone", "disclosure", "limits-notes", "max-hold", "max-spend"];
const PREF_CHECKS = ["allow-plan", "allow-cancel"];

function loadPrefs() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { prefs = {}; }
  for (const id of PREF_FIELDS) if (prefs[id] !== undefined) $(id).value = prefs[id];
  for (const id of PREF_CHECKS) if (prefs[id] !== undefined) $(id).checked = !!prefs[id];
  if (!$("disclosure").value) $("disclosure").value = DEFAULT_DISCLOSURE;
}

function savePrefs() {
  const prefs = {};
  for (const id of PREF_FIELDS) prefs[id] = $(id).value;
  for (const id of PREF_CHECKS) prefs[id] = $(id).checked;
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private mode */ }
}

// ---- info the agent can share (order number, email, ...) ----

function addInfoRow(label = "", value = "") {
  const row = el("div", "info-row");
  const name = el("input");
  name.placeholder = "What it is";
  name.maxLength = 40;
  name.value = label;
  name.className = "info-label";
  const val = el("input");
  val.placeholder = "Value";
  val.maxLength = 120;
  val.value = value;
  val.className = "info-value secret";
  val.autocomplete = "off";
  val.spellcheck = false;
  const del = el("button", "ghost");
  del.type = "button";
  del.textContent = "×";
  del.title = "Remove";
  del.setAttribute("aria-label", "Remove");
  del.addEventListener("click", () => row.remove());
  row.append(name, val, del);
  $("info-rows").appendChild(row);
  (label ? val : name).focus();
  return row;
}

function readInfo() {
  const out = [];
  for (const row of $("info-rows").querySelectorAll(".info-row")) {
    const label = row.querySelector(".info-label").value.trim();
    const value = row.querySelector(".info-value").value.trim();
    if (label && value) out.push({ label, value });
  }
  return out;
}

function clearInfo() {
  $("info-rows").textContent = "";
}

for (const chip of document.querySelectorAll("#info-chips .chip")) {
  chip.addEventListener("click", () => {
    const label = chip.dataset.label;
    // One row per label: jump to it instead of adding a duplicate.
    for (const row of $("info-rows").querySelectorAll(".info-row")) {
      if (label && row.querySelector(".info-label").value.trim() === label) {
        row.querySelector(".info-value").focus();
        return;
      }
    }
    addInfoRow(label);
  });
}

for (const chip of document.querySelectorAll("#goal-chips .chip")) {
  chip.addEventListener("click", () => {
    $("goal").value = chip.dataset.goal;
    $("goal").focus();
    checkGoal();
  });
}

/** Numbers in the goal are stored with the call; nudge them into Info instead. */
function checkGoal() {
  $("goal-hint").hidden = !/\d[\d -]{4,}\d/.test($("goal").value);
}
$("goal").addEventListener("input", checkGoal);

// Ctrl/Cmd+Enter starts the call from anywhere in the form.
$("start-form").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("start-form").requestSubmit();
});

/** FastAPI 422s come back as a list of {msg}; show just the message. */
function friendlyError(err) {
  try {
    const detail = JSON.parse(err.detail);
    if (Array.isArray(detail)) {
      return detail.map((d) => String(d.msg || d).replace(/^Value error, /, "")).join(" ");
    }
    return String(detail);
  } catch {
    return err.message;
  }
}

// The section 19 demo, one click away during the pitch.
$("preset").addEventListener("click", async () => {
  await searchCompanies("");
  const amazon = companies.find((c) => /amazon/i.test(c.name)) || companies[0];
  if (amazon) {
    $("company").value = amazon.name;
    fillPhoneFromCompany();
  }
  $("goal").value = "Get a refund for wireless headphones that arrived damaged.";
  checkGoal();
  clearInfo();
  addInfoRow("Order number", "112-4567890-1234567");
  $("limits-notes").value = "No new charges or subscriptions.";
  $("max-spend").value = 0;
  $("allow-plan").checked = false;
  $("allow-cancel").checked = false;
  if (!$("disclosure").value) $("disclosure").value = DEFAULT_DISCLOSURE;
});

$("start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("start-msg");
  const body = {
    company: $("company").value.trim(),
    phone_number: $("phone").value.trim(),
    goal: $("goal").value.trim(),
    limits: {
      notes: $("limits-notes").value.trim(),
      max_hold_minutes: Number($("max-hold").value) || 60,
      max_spend_usd: Number($("max-spend").value) || 0,
      allow_plan_changes: $("allow-plan").checked,
      allow_cancellation: $("allow-cancel").checked,
    },
    disclosure_text: $("disclosure").value.trim() || DEFAULT_DISCLOSURE,
    known_info: readInfo(),
  };
  setMsg(msg, "Starting…");
  try {
    const session = await http("POST", "/calls", body);
    body.known_info = null; // drop our only other reference to the values
    clearInfo();
    savePrefs();
    setMsg(msg, `Calling ${session.company}…`, "ok");
    await joinCall(session.id);
    loadRecentCalls();
    document.getElementById("live").scrollIntoView();
  } catch (err) {
    setMsg(msg, `Could not start: ${friendlyError(err)}`, "err");
  }
});

loadPrefs();

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

renderPath();
renderBanner();
renderStats();
searchCompanies("");
loadRecentCalls();
{
  const m = location.hash.match(/call=([^&]+)/);
  if (m) joinCall(decodeURIComponent(m[1]));
}
