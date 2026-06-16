#!/bin/bash
# slop-bridge.sh — bring up the entire clawd ↔ slop.computer bridge.
#
# WORKING CONFIG (locked in after a lot of trial and error):
#   • Chrome  hosts clawd at http://127.0.0.1:7900/
#   • OBS     does macOS Screen Capture of that Chrome window, virtual-cam
#             feeds it out (audio muted in OBS — we route audio separately)
#   • Chrome Canary hosts https://live.slop.computer/ — picks up OBS virtual
#             cam for video and reads BlackHole 2ch for clawd's audio
#
# Why this specific pair of browsers:
#   • Chrome (not Safari) for clawd because Safari has a getUserMedia bug
#     where it silently returns audio from a different BlackHole device than
#     the one we asked for, breaking clean routing.
#   • Chrome Canary (not stable Chrome) for slop because Chrome's AEC links
#     audio between tabs in the same browser process, causing feedback when
#     clawd's TTS and slop's mic both live in Chrome. Canary is a separate
#     app/process, so no AEC link. (Canary is Chromium-based so its
#     getUserMedia works correctly, unlike Safari.)
#
# What this script does, in order:
#   1. Snapshot current macOS default in/out devices so we can restore them.
#   2. Set system input + output → BlackHole 2ch.
#   3. Start python3 server.py on port 7900 if not already running.
#   4. Open a fresh Chrome window at clawd; capture its CGWindow ID.
#   5. Patch the OBS scene so $OBS_SOURCE_NAME captures that Chrome window.
#   6. Launch OBS with virtual-cam started.
#   7. Open https://live.slop.computer/?invite=... in Chrome Canary.
#
# What it intentionally does NOT do (one-time UI setup; browsers remember):
#   • Grant Chrome mic permission for clawd — first-time prompt.
#   • Pick "BlackHole 2ch" as the mic device in Chrome Canary's slop tab —
#     do this once via 🔒 → site permissions → Microphone.
#
# Requires:
#   • brew install switchaudio-osx
#   • BlackHole 2ch
#   • Google Chrome Canary installed
#   • OBS with a window-capture source named CLAWDSCREEN inside scene CLAWD
#   • Screen Recording permission granted to whichever Terminal app you
#     run this from (needed by the swift CGWindow snippet below)

set -euo pipefail

CLAWD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAWD_URL="http://127.0.0.1:7900/"
SLOP_URL="https://live.slop.computer/clawdbotatg?invite=o6nYhKLvQAZiOoAk"

SYS_INPUT_DEVICE="BlackHole 2ch"
SYS_OUTPUT_DEVICE="BlackHole 2ch"

OBS_SCENE="$HOME/Library/Application Support/obs-studio/basic/scenes/Untitled.json"
OBS_SOURCE_NAME="CLAWDSCREEN"
OBS_ACTIVE_SCENE="CLAWD"

say()  { printf "\n\033[1;35m▸ %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m! %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# ── 1. Prereqs ───────────────────────────────────────────────────────────────
command -v SwitchAudioSource >/dev/null \
    || die "SwitchAudioSource not found. Install:  brew install switchaudio-osx"

DEVICES="$(SwitchAudioSource -a)"
grep -q "BlackHole 2ch"  <<<"$DEVICES" || die "BlackHole 2ch not installed."

# ── 2. Snapshot prior audio defaults ─────────────────────────────────────────
STATE_FILE="$HOME/.cache/clawd/slop-bridge.state"
mkdir -p "$(dirname "$STATE_FILE")"
PREV_OUT="$(SwitchAudioSource -c -t output)"
PREV_IN="$(SwitchAudioSource -c -t input)"
{
    echo "PREV_OUT=$PREV_OUT"
    echo "PREV_IN=$PREV_IN"
} > "$STATE_FILE"
say "Snapshot saved → $STATE_FILE (restore with ./slop-bridge-stop.sh)"

# ── 3. Set audio defaults ────────────────────────────────────────────────────
say "Setting system OUTPUT → $SYS_OUTPUT_DEVICE"
SwitchAudioSource -t output -s "$SYS_OUTPUT_DEVICE"
say "Setting system INPUT  → $SYS_INPUT_DEVICE"
SwitchAudioSource -t input  -s "$SYS_INPUT_DEVICE"

# ── 3b. Audio-defaults watcher ───────────────────────────────────────────────
# macOS auto-switches the default output/input whenever a new device is
# plugged in (headphones, USB mics, etc.) — which would silently break the
# bridge. Spawn a tiny background watcher that re-asserts the bridge devices
# (output=$SYS_OUTPUT_DEVICE, input=$SYS_INPUT_DEVICE) every 2 seconds.
# slop-bridge-stop.sh kills this watcher.
WATCH_PIDFILE="$HOME/.cache/clawd/slop-bridge-watch.pid"
# Kill any stale watcher from a prior run before starting a new one.
if [ -f "$WATCH_PIDFILE" ]; then
    OLD_PID="$(cat "$WATCH_PIDFILE" 2>/dev/null || true)"
    [ -n "$OLD_PID" ] && kill "$OLD_PID" 2>/dev/null || true
    rm -f "$WATCH_PIDFILE"
fi
say "Spawning audio-defaults watcher (out=$SYS_OUTPUT_DEVICE, in=$SYS_INPUT_DEVICE, every 2s)…"
nohup bash -c '
  out="'"$SYS_OUTPUT_DEVICE"'"
  in_="'"$SYS_INPUT_DEVICE"'"
  while true; do
    cur_out=$(SwitchAudioSource -c -t output 2>/dev/null)
    cur_in=$(SwitchAudioSource -c -t input  2>/dev/null)
    [ "$cur_out" != "$out" ] && SwitchAudioSource -t output -s "$out" >/dev/null 2>&1
    [ "$cur_in"  != "$in_" ] && SwitchAudioSource -t input  -s "$in_" >/dev/null 2>&1
    sleep 2
  done
' >/tmp/slop-bridge-watch.log 2>&1 &
echo $! > "$WATCH_PIDFILE"
disown 2>/dev/null || true

# ── 4. Server ────────────────────────────────────────────────────────────────
if curl -sf -m 2 "$CLAWD_URL" >/dev/null; then
    say "Clawd server already up on $CLAWD_URL"
else
    say "Starting clawd server.py…"
    LOG="/tmp/clawd-server.log"
    (cd "$CLAWD_DIR" && nohup python3 server.py >"$LOG" 2>&1 &)
    for _ in $(seq 1 60); do
        curl -sf -m 1 "$CLAWD_URL" >/dev/null && break
        sleep 0.5
    done
    curl -sf -m 1 "$CLAWD_URL" >/dev/null \
        || die "Clawd server didn't come up. Check $LOG"
    say "Clawd server up. Log: $LOG"
fi

# ── 5. Quit OBS so JSON edits stick on next launch ───────────────────────────
if pgrep -xq OBS; then
    say "Quitting OBS so the new scene config takes effect…"
    osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        pgrep -xq OBS || break
        sleep 0.25
    done
fi

# ── 6. Open a fresh Chrome window at clawd ───────────────────────────────────
# Approach:
#   • AppleScript opens the clawd page in a new Chrome window.
#   • Chrome's AppleScript dictionary returns window.bounds as {left, top,
#     right, bottom} — same shape as Safari.
#   • Swift walks the CGWindowList and matches by bounds — guaranteed to be
#     the right Chrome window even if other Chrome windows are open.
say "Opening clawd in a new Chrome window…"
BOUNDS="$(osascript <<APPLE
tell application "Google Chrome"
    activate
    set newWin to make new window
    set URL of active tab of newWin to "$CLAWD_URL"
    delay 1.0
    set b to bounds of newWin
    return ((item 1 of b) as text) & " " & ((item 2 of b) as text) & " " & (((item 3 of b) - (item 1 of b)) as text) & " " & (((item 4 of b) - (item 2 of b)) as text)
end tell
APPLE
)"
read -r TX TY TW TH <<<"$BOUNDS"
[ -n "${TX:-}" ] && [ -n "${TY:-}" ] && [ -n "${TW:-}" ] && [ -n "${TH:-}" ] \
    || die "Couldn't read clawd window bounds from Chrome ($BOUNDS)"
say "clawd Chrome window at $TX,$TY ${TW}x${TH} — matching to CGWindow…"

NEW_WID=""
for _ in $(seq 1 60); do
    NEW_WID="$(swift - "$TX" "$TY" "$TW" "$TH" <<'SWIFT'
import Cocoa
let a = CommandLine.arguments
guard a.count >= 5,
      let tx = Int(a[1]), let ty = Int(a[2]),
      let tw = Int(a[3]), let th = Int(a[4]) else { exit(1) }
let infos = (CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) ?? []
for w in infos {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    if owner != "Google Chrome" { continue }
    let layer = w[kCGWindowLayer as String] as? Int ?? -1
    if layer != 0 { continue }
    guard let bounds = w[kCGWindowBounds as String] as? [String: CGFloat] else { continue }
    if Int(bounds["X"] ?? 0) == tx && Int(bounds["Y"] ?? 0) == ty
       && Int(bounds["Width"] ?? 0) == tw && Int(bounds["Height"] ?? 0) == th {
        if let n = w[kCGWindowNumber as String] as? Int { print(n); exit(0) }
    }
}
SWIFT
)"
    [ -n "$NEW_WID" ] && break
    sleep 0.25
done
[ -n "$NEW_WID" ] || die "Couldn't find a Chrome CGWindow matching $TX,$TY ${TW}x${TH}. Grant Screen Recording permission to this Terminal?"
say "Matched clawd Chrome window → CGWindow ID $NEW_WID"

# ── 7. Patch OBS scene JSON ──────────────────────────────────────────────────
[ -f "$OBS_SCENE" ] || die "OBS scene not found: $OBS_SCENE"
cp "$OBS_SCENE" "$OBS_SCENE.bak.$(date +%s)"

say "Patching OBS scene → $OBS_SOURCE_NAME.window=$NEW_WID, active scene=$OBS_ACTIVE_SCENE"
python3 - "$OBS_SCENE" "$OBS_SOURCE_NAME" "$NEW_WID" "$OBS_ACTIVE_SCENE" <<'PY'
import json, sys, pathlib
scene, source_name, wid, active = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
p = pathlib.Path(scene)
d = json.loads(p.read_text())

# Match the capture source by name first. OBS sometimes loses the custom name
# (delete + re-add leaves the default "macOS Screen Capture"), so fall back to
# the lone screen_capture source by its type id — the name is cosmetic; what
# matters is pointing the one screen-capture source at the clawd window.
hit = next((s for s in d.get("sources", []) if s.get("name") == source_name), None)
if hit is None:
    caps = [s for s in d.get("sources", []) if s.get("id") == "screen_capture"]
    if len(caps) == 1:
        hit = caps[0]
        print(f"note: source '{source_name}' not found; using screen_capture "
              f"source '{hit.get('name')}' instead", file=sys.stderr)
    elif len(caps) > 1:
        names = [s.get("name") for s in caps]
        sys.exit(f"source '{source_name}' not found and multiple screen_capture "
                 f"sources exist ({names}) — rename one to {source_name!r}")
if hit is None:
    sys.exit(f"source '{source_name}' not found in scene and no screen_capture "
             f"source to fall back to")
hit.setdefault("settings", {})
hit["settings"]["window"] = wid
hit["settings"]["application"] = "com.google.Chrome"
hit["settings"]["type"] = 1   # 1 = window capture in screen_capture source
d["current_scene"] = active
d["current_program_scene"] = active
p.write_text(json.dumps(d, indent=4))
print(f"patched {source_name} → window={wid}; active scene → {active}")
PY

# ── 8. Launch OBS with virtual cam ───────────────────────────────────────────
# Ensure obs-websocket is enabled BEFORE launch so we can bind the capture
# window live afterwards. Cold-loading the window id from the scene JSON does
# not reliably re-establish the ScreenCaptureKit stream on macOS (OBS grabs
# the wrong window despite correct saved settings) — a live SetInputSettings
# over the websocket reproduces the by-hand re-select that does work. OBS is
# guaranteed quit here (section 5), so this edit sticks on next launch.
WS_CFG="$HOME/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
if [ -f "$WS_CFG" ]; then
    python3 - "$WS_CFG" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
if not c.get("server_enabled"):
    c["server_enabled"] = True
    json.dump(c, open(p, "w"), indent=4)
    print("enabled obs-websocket (was off)")
PY
else
    warn "obs-websocket config not found — live window-bind will be skipped"
fi

say "Launching OBS (--startvirtualcam)…"
open -ga OBS --args --startvirtualcam

# ── 8b. Bind capture source → clawd window LIVE (the actual fix) ──────────────
# obs_bind_window.py connects over obs-websocket, finds the clawd window by
# title (falling back to the CGWindow id matched above), and pushes the
# settings live — forcing the rebind a cold load misses.
say "Binding OBS capture → clawd window (live, via obs-websocket)…"
if python3 "$CLAWD_DIR/obs_bind_window.py" \
        --scene "$OBS_ACTIVE_SCENE" \
        --match "7900,clawd-video-chat,clawd,127.0.0.1" \
        --fallback-window "$NEW_WID"; then
    :
else
    warn "Live window-bind failed. In OBS: double-click the screen-capture source → pick the 127.0.0.1:7900 window."
fi

# ── 9. Open slop in Chrome Canary ────────────────────────────────────────────
# Chrome Canary (not stable Chrome) so it's a separate app/process from clawd's
# Chrome instance, breaking Chrome's same-browser AEC link that would otherwise
# feedback the two tabs together. Canary is Chromium so its getUserMedia
# respects the explicit BlackHole 2ch device choice (Safari does not — confirmed).
say "Opening slop in Chrome Canary → $SLOP_URL"
open -a "Google Chrome Canary" "$SLOP_URL"

# ── 10. Recap ────────────────────────────────────────────────────────────────
cat <<EOF

──────────────────────────────────────────────────────────────────
$(printf "\033[1;32m✓ bridge is up\033[0m")

audio routing now in effect:
  remote voices on slop → BlackHole 2ch → clawd's mic / SR
  clawd's TTS           → BlackHole 2ch → slop's mic

one-time per-browser setup (skip if already done):
  • Chrome clawd tab        : grant mic permission on first prompt.
  • Chrome Canary slop tab  : grant mic and pick \"BlackHole 2ch\" in the
                              device picker, OR click 🔒 → site
                              permissions → Microphone → BlackHole 2ch.

obs:
  • Scene CLAWD active, CLAWDSCREEN pointed at Chrome window $NEW_WID
  • Virtual camera started (audio muted in OBS — audio routes via BH-2ch)

teardown (restore prior system audio defaults):
  ./slop-bridge-stop.sh
──────────────────────────────────────────────────────────────────

EOF
