#!/bin/bash
# stt-selftest.sh — external end-to-end probe of the room-transcript pipeline.
#
# Runs under launchd (com.clawd.stt-selftest, every 20 min). The in-page SR
# flatline watchdog can only heal what the page's own JS can see — it is blind
# when the tab is closed, Chrome is hung, the metering path died with the
# recognizer, or the watchdog itself regressed. This prober trusts NOTHING
# in-page: it speaks a marker phrase into BlackHole 2ch (clawd's ear — a
# virtual cable, inaudible in the room and never sent to the call) and checks
# that the marker traveled the whole pipeline: cable → getUserMedia → SR →
# handleFinalChunk → POST /api/stt-log → server heartbeat file. server.py
# filters the marker out of stt-log.jsonl, so probes never pollute the room
# transcript or reach the brain.
#
# On failure it heals in stages, then alerts:
#   1. touch the page-reload flag → the page polls /health and reloads itself
#      (no TCC needed; the page defers mid-turn)
#   2. AppleScript-reload (or open) the tab from outside — works when page JS
#      is dead; skipped while TTS was streaming in the last 30s
#   3. macOS notification — a human has to look (see AUDIO-RUNBOOK.md)
#
# Log: /tmp/stt-selftest.log  ·  Off-switch + details: SERVICES.md

PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
HB="$HOME/.cache/clawd/stt-selftest-heard"
REQ="$HOME/.cache/clawd/clawd-page-reload.req"
LOG="/tmp/stt-selftest.log"
BASE="http://127.0.0.1:7900"

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

# Rig down (:7900 not serving) → intentionally off, nothing to test.
health=$(curl -sf -m 3 "$BASE/health" 2>/dev/null) || exit 0

# Default input not the ear cable = the SR-DEAF case: the probe would fail for
# a reason with its own banner + runbook fix. Log it once per run and bail —
# the audio watcher (slop-bridge) owns re-pinning devices, not us.
cur_in=$(SwitchAudioSource -c -t input 2>/dev/null)
if [ "$cur_in" != "BlackHole 2ch" ]; then
  log "skip: default input is '$cur_in', not BlackHole 2ch (SR DEAF case — see AUDIO-RUNBOOK.md)"
  exit 0
fi

# Last REAL room text the page logged (ms epoch, from /health stt_text_ts).
text_ts() {
  curl -sf -m 3 "$BASE/health" 2>/dev/null \
    | python3 -c 'import sys,json;print(int(json.load(sys.stdin).get("stt_text_ts",0)))' 2>/dev/null || echo 0
}
now_ms() { echo $(($(date +%s) * 1000)); }

# A transcript that is demonstrably FLOWING is the strongest possible proof —
# real words reached /api/stt-log moments ago. Don't speak the marker over a
# busy room: mixed with live speech it is often masked or finalized well after
# the wait window, and that false FAIL reloaded the voice page MID-CALL
# (2026-09-03 11:58, during a live show). Never reload a page that is hearing.
FLOW_OK_MS=90000
if [ "$(text_ts)" -gt $(($(now_ms) - FLOW_OK_MS)) ]; then
  exit 0
fi

probe() {
  # Marker heard (heartbeat advanced) OR real room text arrived while we
  # waited — either proves cable → SR → server end to end.
  local before now t0
  before=$(stat -f %m "$HB" 2>/dev/null || echo 0)
  t0=$(text_ts)
  say -a "BlackHole 2ch" "transcript self test check" 2>>"$LOG"
  for _ in $(seq 1 15); do
    sleep 1
    now=$(stat -f %m "$HB" 2>/dev/null || echo 0)
    [ "$now" -gt "$before" ] && return 0
    [ "$(text_ts)" -gt "$t0" ] && return 0
  done
  return 1
}

probe && exit 0   # healthy — the common case, no log spam

# ── Stage 1: ask the page to reload itself ──
log "FAIL: marker not transcribed — requesting page self-reload"
date >"$REQ"
sleep 45
probe && { log "healed: page self-reload"; exit 0; }

# ── Stage 2: reload/open the tab from outside ──
# Don't yank the tab while clawd is mid-sentence: if TTS streamed in the last
# 30s the page is alive-but-deferring (or busy) — retry next cycle instead.
last_tts=$(curl -sf -m 3 "$BASE/health" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("last_tts_ts",0))' 2>/dev/null || echo 0)
now_ms=$(($(date +%s) * 1000))
if [ "${last_tts:-0}" -gt $((now_ms - 30000)) ]; then
  log "deferring external reload: TTS active in the last 30s — will retry next cycle"
  exit 0
fi
log "still dead — reloading tab via AppleScript"
r=$(osascript -e 'tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t contains "127.0.0.1:7900" then
        reload t
        return "reloaded"
      end if
    end repeat
  end repeat
  return "notab"
end tell' 2>>"$LOG")
log "AppleScript reload returned '${r:-<error>}'"
if [ "$r" = "notab" ]; then
  log "no clawd tab found — opening one"
  open -a "Google Chrome" "$BASE"
fi
sleep 20
probe && { log "healed: external tab reload"; exit 0; }

# ── Stage 3: human ──
log "STILL DEAD after both heals — notifying"
osascript -e 'display notification "Room transcript pipeline is DEAD and auto-heal failed. See /tmp/stt-selftest.log and AUDIO-RUNBOOK.md." with title "clawd stt-selftest" sound name "Basso"' 2>>"$LOG"
exit 1
