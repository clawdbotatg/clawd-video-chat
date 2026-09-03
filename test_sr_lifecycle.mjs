#!/usr/bin/env node
// test_sr_lifecycle.mjs — regression guard for the recognizer restart race
// (2026-09-03). Runs index.html's REAL startWakeRecog / stopWakeRecog /
// srWdKickRecog against a fake webkitSpeechRecognition that behaves like
// Chrome: one live session per page (starting a new instance aborts the live
// one) and asynchronous onerror("aborted")/onend delivered LATE — later than
// RESTART_DELAY_MS. Before the identity guard in onend, a single watchdog kick
// under those conditions produced a perpetual abort/restart chain (every
// recognizer lived ~250ms → zero results forever = the "zombie" that only a
// reload fixed). The test asserts the loop settles on exactly one live
// recognizer that _recog points at, and stays quiet.
//
//   node test_sr_lifecycle.mjs            # checks ./index.html
//   node test_sr_lifecycle.mjs other.html # e.g. `git show HEAD~1:index.html > old.html`
import fs from "node:fs";
import vm from "node:vm";

const file = process.argv[2] || new URL("./index.html", import.meta.url).pathname;
const html = fs.readFileSync(file, "utf8");

function fn(name, required = true) {
  const m = html.match(new RegExp(`\\nfunction ${name}\\(\\) \\{[\\s\\S]*?\\n\\}\\n`));
  if (!m) { if (required) throw new Error(`function ${name} not found in ${file}`); return ""; }
  return m[0];
}
const consts = ["RESTART_DELAY_MS", "SR_ERR_BACKOFF_MAX_MS"].map(c => {
  const m = html.match(new RegExp(`\\nconst ${c} = [^;]+;`));
  return m ? m[0] : `\nconst ${c} = 250;`;
}).join("");
const src = consts + `
let _recog = null, _restartTimer = null, _wakeManuallyPaused = false;
let _srLastResultTs = Date.now(), _srErrStreak = 0;
let _wakeArmed = false, _ttsAudio = null, _phoneMode = false;
` + fn("srRestartDelay", false) + fn("startWakeRecog") + fn("stopWakeRecog") + fn("srWdKickRecog") + `
globalThis.__api = { start: startWakeRecog, stop: stopWakeRecog, kick: srWdKickRecog, recog: () => _recog };
`;

// ── fake Chrome ──
const LATE_MS = 400;            // abort→onend latency, deliberately > RESTART_DELAY_MS
const stats = { created: 0, starts: 0, aborts: 0, lastAbortTs: 0 };
let live = null;
class FakeSR {
  constructor() { stats.created++; this.id = stats.created; }
  _end(err) {
    setTimeout(() => {
      try { if (err && this.onerror) this.onerror({ error: err }); } catch {}
      try { if (this.onend) this.onend(); } catch {}
    }, LATE_MS);
  }
  start() {
    if (live === this) throw new Error("InvalidStateError: recognition has already started");
    if (live) { const old = live; live = null; stats.aborts++; stats.lastAbortTs = Date.now(); old._end("aborted"); }
    live = this; stats.starts++;
    setTimeout(() => { try { this.onstart && this.onstart(); } catch {} }, 5);
  }
  abort() { if (live === this) { live = null; stats.aborts++; stats.lastAbortTs = Date.now(); this._end("aborted"); } }
  stop()  { if (live === this) { live = null; this._end(null); } }
}
const ctx = {
  console: { log() {}, warn() {} },
  setTimeout, clearTimeout, Date, Math,
  window: { webkitSpeechRecognition: FakeSR, __clawdMeters: null },
  setPillState() {}, bcLog() {}, currentTab: () => null,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: "sr-lifecycle-extract.js" });
const api = ctx.__api;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0;
function check(label, ok, detail) {
  console.log(`${ok ? "ok  " : "FAIL"} ${label}${detail ? " — " + detail : ""}`);
  if (!ok) failed++;
}
async function settled(label) {
  const abortsBefore = stats.aborts;
  await sleep(1500);
  const quiet = stats.aborts === abortsBefore;
  check(`${label}: exactly one live recognizer`, !!live && api.recog() === live,
        `live=${live && live.id} _recog=${api.recog() && api.recog().id} created=${stats.created}`);
  check(`${label}: no abort churn while idle`, quiet, `aborts in last 1.5s: ${stats.aborts - abortsBefore}`);
}

api.start();
await sleep(50);
check("boot: recognizer live", live && api.recog() === live);

// 1. watchdog kick with a LATE onend (the 2026-09-03 zombie trigger)
api.kick();
await sleep(1200);
await settled("after watchdog kick");

// 2. stop + later start (mic toggle path)
api.stop();
await sleep(LATE_MS + 300);
api.start();
await sleep(300);
await settled("after stop/start");

// 3. Chrome-initiated end (no-speech): the live one ends by itself
live.stop();
await sleep(LATE_MS + RESTART(ctx) + 200);
await settled("after natural onend");

// 4. two kicks back to back (idle kick racing the flatline kick)
api.kick(); api.kick();
await sleep(1200);
await settled("after double kick");

console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
process.exit(failed ? 1 : 0);

function RESTART(c) { return vm.runInContext("RESTART_DELAY_MS", c); }
