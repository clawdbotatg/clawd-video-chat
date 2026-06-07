#!/bin/bash
# stream-setup.sh — bring up clawd-video-chat end-to-end:
#   1. Start server.py in a new iTerm window (if not already running)
#   2. Launch Chrome in --app mode pointing at the front-end
#   3. Patch the OBS scene JSON so CLAWDSCREEN points at that Chrome window
#   4. Open OBS

set -euo pipefail

CLAWD_DIR="/Users/austingriffith/clawd/clawd-video-chat"
URL="http://127.0.0.1:7900/"
CHROME_PROFILE="$HOME/Library/Application Support/clawd-chrome-profile"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OBS_SCENE="$HOME/Library/Application Support/obs-studio/basic/scenes/Untitled.json"
OBS_SOURCE_NAME="CLAWDSCREEN"
OBS_ACTIVE_SCENE="CLAWD"

# Chrome --app window geometry. Override via env if you re-tune.
WIN_X="${WIN_X:-510}"
WIN_Y="${WIN_Y:-70}"
WIN_W="${WIN_W:-1096}"
WIN_H="${WIN_H:-712}"

say() { printf "\n\033[1;35m▸ %s\033[0m\n" "$*"; }
die() { printf "\n\033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# ── 1. Server ────────────────────────────────────────────────────────────────
if curl -sf -m 2 "$URL" >/dev/null; then
    say "Server already up on $URL."
else
    say "Starting server.py in iTerm…"
    open -ga iTerm
    for _ in $(seq 1 20); do
        osascript -e 'tell application "iTerm2" to return name' >/dev/null 2>&1 && break
        sleep 0.5
    done
    osascript <<APPLE
tell application "iTerm2"
    activate
    set newWindow to (create window with default profile)
    tell current session of newWindow
        write text "cd $CLAWD_DIR && python3 server.py"
    end tell
end tell
APPLE
    say "Waiting for server to respond…"
    for _ in $(seq 1 60); do
        curl -sf -m 1 "$URL" >/dev/null && { say "Server up."; break; }
        sleep 0.5
    done
    curl -sf -m 1 "$URL" >/dev/null || die "Server didn't come up at $URL"
fi

# ── 2. Quit OBS so JSON edits stick on next launch ───────────────────────────
if pgrep -xq OBS; then
    say "Quitting OBS so the new scene config takes effect…"
    osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        pgrep -xq OBS || break
        sleep 0.25
    done
fi

# ── 3. Chrome --app with isolated profile ────────────────────────────────────
mkdir -p "$CHROME_PROFILE"
# Keep Spotlight from indexing Chrome's profile internals (thousands of files
# → mds/mdworker_shared can hog the CPU and make the OS feel choppy).
touch "$CHROME_PROFILE/.metadata_never_index" 2>/dev/null || true

# Pre-set Chrome's saved app window placement so it lands at our exact rect.
# Chrome stores per-app window bounds under browser.app_window_placement.{host}…
# and those override --window-position on macOS. We rewrite every rect leaf.
CHROME_PREFS="$CHROME_PROFILE/Default/Preferences"
if [ -f "$CHROME_PREFS" ]; then
    say "Pinning Chrome app_window_placement → ${WIN_X},${WIN_Y} ${WIN_W}x${WIN_H}"
    python3 - "$CHROME_PREFS" "$WIN_X" "$WIN_Y" "$WIN_W" "$WIN_H" <<'PY'
import json, sys, pathlib
prefs_path = pathlib.Path(sys.argv[1])
x, y, w, h = map(int, sys.argv[2:6])
d = json.loads(prefs_path.read_text())
def patch(node):
    if isinstance(node, dict):
        if {"left", "top", "right", "bottom"}.issubset(node.keys()):
            node["left"], node["top"] = x, y
            node["right"], node["bottom"] = x + w, y + h
            node["maximized"] = False
        for v in node.values():
            patch(v)
patch(d.get("browser", {}).get("app_window_placement", {}))
prefs_path.write_text(json.dumps(d, separators=(",", ":")))
PY
fi

# Snapshot existing Chrome window IDs *before* we launch the new one.
chrome_window_ids() {
    swift - <<'SWIFT'
import Cocoa
let infos = (CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) ?? []
for w in infos {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    let layer = w[kCGWindowLayer as String] as? Int ?? -1
    if owner == "Google Chrome" && layer == 0 {
        if let n = w[kCGWindowNumber as String] as? Int { print(n) }
    }
}
SWIFT
}

BEFORE=$(chrome_window_ids | sort -u)

say "Launching Chrome --app…"
"$CHROME_BIN" \
    --user-data-dir="$CHROME_PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --window-position="$WIN_X,$WIN_Y" \
    --window-size="$WIN_W,$WIN_H" \
    --autoplay-policy=no-user-gesture-required \
    --use-fake-ui-for-media-stream \
    --app="$URL" \
    >/dev/null 2>&1 &

say "Hunting for the new window's CGWindow ID…"
NEW_WID=""
for _ in $(seq 1 60); do
    AFTER=$(chrome_window_ids | sort -u)
    NEW_WID=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | head -n1)
    [ -n "$NEW_WID" ] && break
    sleep 0.25
done
[ -n "$NEW_WID" ] || die "Couldn't detect a new Chrome window — is the --app window visible on this Space?"
say "New Chrome window CGWindow ID: $NEW_WID"

# ── 4. Patch OBS scene JSON ──────────────────────────────────────────────────
[ -f "$OBS_SCENE" ] || die "OBS scene not found: $OBS_SCENE"
cp "$OBS_SCENE" "$OBS_SCENE.bak.$(date +%s)"

say "Patching OBS scene → $OBS_SOURCE_NAME.window=$NEW_WID, active scene=$OBS_ACTIVE_SCENE"
python3 - "$OBS_SCENE" "$OBS_SOURCE_NAME" "$NEW_WID" "$OBS_ACTIVE_SCENE" <<'PY'
import json, sys, pathlib
scene, source_name, wid, active = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
p = pathlib.Path(scene)
d = json.loads(p.read_text())
hit = False
for s in d.get("sources", []):
    if s.get("name") == source_name:
        s.setdefault("settings", {})
        s["settings"]["window"] = wid
        s["settings"]["application"] = "com.google.Chrome"
        s["settings"]["type"] = 1  # 1 = window capture in screen_capture source
        hit = True
        break
if not hit:
    sys.exit(f"source '{source_name}' not found in scene")
d["current_scene"] = active
d["current_program_scene"] = active
p.write_text(json.dumps(d, indent=4))
print(f"patched {source_name} → window={wid}; active scene → {active}")
PY

# ── 5. Launch OBS (with virtual camera auto-started) ─────────────────────────
say "Launching OBS (--startvirtualcam)…"
open -ga OBS --args --startvirtualcam

say "Done. Front-end → $URL ; OBS source $OBS_SOURCE_NAME → window $NEW_WID"
