# EXPECTATIONS.md — the transcript contract

**Read this in full before responding to any report that the transcript /
STT / "words in the backchannel" stopped working.** This file exists because
Austin has reported this same failure MANY times across months — his words on
2026-09-01: *"this marks time number 10000 i have asked you to fixed the
transcript words are not showing up in the backchannel — i ask you this
fucking every other episode."* If you are reading this because he reported it
again, the system below has **failed its contract**, and a tab reload is not
an acceptable answer.

## The expectation

1. **The room transcript never silently dies.** Words spoken on the call
   appear in `stt-log.jsonl` and the backchannel within seconds, around the
   clock, across sleeps, across weeks of uptime.
2. **When the pipeline breaks, it heals itself** — within one
   `com.clawd.stt-selftest` cycle (20 min).
3. **When it can't heal itself, it tells Austin** via macOS notification —
   he must never be the detector.
4. **If Austin reports it anyway, the auto-detect/auto-heal system itself is
   broken.** Fixing the transcript for today is the *smaller* half of the
   job. The larger half: find WHY the selftest daemon didn't catch or heal
   it, close that hole, prove the closure with an unattended end-to-end test,
   and extend `stt-selftest.sh` / the watchdog so this *class* of failure is
   covered. Then log it (AUDIO-RUNBOOK §6, memory). A fix that requires a
   human next time is not a fix.

## The history (why band-aids are banned)

Every one of these looked like "reload the tab" on the day, and each hid a
deeper hole that guaranteed a repeat:

- **2026-08-05** — deaf on a live call: uncommitted `slop-bridge.sh` edit
  pinned default input to 16ch.
- **Aug 7→10** — transcript dead 3 days: Chrome's recognizer went zombie
  (no `onresult`, no `onend`); nothing watched for it. → SR flatline
  watchdog.
- **2026-08-18** — deaf, all green: 2ch per-device volume at 0.
- **2026-08-20** — dings on speakers; the reflex volume-down deafened clawd.
- **2026-08-31 → 09-01** — dead 19h, every daemon green: the watchdog was
  *blind* (sleep suspended the AudioContext feeding its meter), and the
  REAL recurring killer surfaced — the audio-unlock overlay gated SR behind
  a human click, so every automatic reload "healed" the page into a deaf
  overlay. Auto-heal had been structurally impossible since it shipped.
- **2026-09-02 → 09-03** — 34 selftest FAILs in two days (one reload landed
  mid-call), and a **push-to-talk hold captured nothing** on a live show.
  Root cause: the 09-01 "prophylactic kick" aborts the recognizer and starts
  a fresh one 250ms later, but Chrome delivers the aborted one's `onend`
  LATE — it nulled `_recog`, orphaned the live replacement, and scheduled a
  start that aborted it (one session per page), whose late `onend` did the
  same… a **perpetual abort/restart chain** (each recognizer lived ~250ms;
  MIC moving, zero results) that only a reload broke. The heal was creating
  the disease. Fixes: identity-guarded `onend` (`test_sr_lifecycle.mjs`
  proves the old code churns and the new one settles), PTT restarts a
  stale/stalled recognizer itself and says on the backchannel when a hold
  heard nothing, selftest trusts a flowing transcript over its marker (no
  more mid-call reloads), and the page's debug feed is persisted to
  `~/.cache/clawd/voice-debug.log` so the next report has evidence.

Pattern: **the display always looks fine, the daemons are always green, and
the failure is always one layer below wherever the last fix looked.** That is
why the only trusted signal is the end-to-end probe: speak into the cable,
see the words come out.

## The machinery that upholds the contract (don't regress it)

- **`stt-selftest.sh` + launchd `com.clawd.stt-selftest`** (20 min): passes
  outright when real room text reached the server in the last 90s (a busy
  call masks the marker — a false FAIL reloaded the page mid-call on
  2026-09-03); otherwise speaks
  "transcript self test check" into BlackHole 2ch → must arrive at
  `/api/stt-log` (server heartbeats `~/.cache/clawd/stt-selftest-heard`,
  filters the marker from the transcript/brain). Heals: page self-reload
  (flag file + `/health` poll) → AppleScript tab reload → notification.
  Log: `/tmp/stt-selftest.log`.
- **Ears auto-start** (`index.html`, commit `1d70e99`): SR + meters start on
  EVERY page load with no click; the overlay only unlocks TTS playback and
  dismisses itself when autoplay is allowed. Never re-gate hearing behind a
  gesture.
- **Recognizer lifecycle** (`index.html` `startWakeRecog`, 2026-09-03): only
  the CURRENT recognizer's `onend` may drive the restart loop (`if (_recog
  !== r) return`). Any code that aborts/replaces a recognizer must keep that
  guard or the abort chain comes back. `node test_sr_lifecycle.mjs` runs the
  real functions against a Chrome-like fake and must pass before a push.
- **Push-to-talk self-defense** (`pttDown`/`pttUp` + the PTT block in the
  watchdog tick): a press with no recognizer or 2 min of no results restarts
  it first; 2.5s of room sound with zero results DURING a hold restarts it
  once; an empty release is announced on the backchannel with the SR age.
  Every hold leaves `ev: ptt-down/ptt-up` rows in `stt-log.jsonl`.
- **Evidence trail**: `/api/debug` lines (srwd / ptt / sr recognizer errors)
  append to `~/.cache/clawd/voice-debug.log`. Read it before guessing.
- **SR flatline watchdog** (`index.html`): sound-vs-results detector, plus
  the 2026-09-01 blind-spot patches — AudioContext resume, muted-track
  reopen, and the 10-min no-sound prophylactic recognizer kick. Never make
  its actions depend solely on the mic analyser hearing sound.
- **slop-bridge watcher**: device pinning + volume flooring (see
  AUDIO-RUNBOOK §3).

## When it happens again

Run **AUDIO-RUNBOOK.md §0** ("Transcript dead? Run this first") — one
command proves or heals the whole pipeline. Then honor expectation #4 above:
the selftest missed it, so the selftest (or its heal path) gets extended
before the session ends. Update this file's history list while you're at it.
