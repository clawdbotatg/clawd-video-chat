#!/bin/bash
# open-slop-canary.sh — open a slop URL in a dedicated Chrome Canary profile with
# the DevTools remote-debugging port ON, fully detached so an openclaw `/new`
# (session tear-down) can't kill it.
#
# Why a dedicated --user-data-dir (and not the normal "Default" profile):
#   Chrome/Canary 136+ IGNORES --remote-debugging-port when the default
#   user-data-dir is used (a security mitigation). To let openclaw attach via
#   CDP we must point Canary at its own data dir. Trade-off: the slop mic
#   permission and the MetaMask wallet live in THIS dir now — grant/import them
#   once on first run and they persist.
#
# How a fresh openclaw "sees" the window after /new:
#   The debug endpoint belongs to the BROWSER process, not the openclaw session.
#   Any new openclaw just connects to  http://127.0.0.1:9222/json  to list tabs
#   and drive them over the DevTools Protocol — no need to re-launch anything.
#
# Usage:  ./open-slop-canary.sh "https://live.slop.computer/pokernight?invite=XXXX"

URL="$1"
if [ -z "$URL" ]; then
    echo "usage: $0 <slop-url>" >&2
    exit 1
fi

CANARY="/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
DEBUG_PORT=9222
USER_DATA_DIR="$HOME/.clawd-canary-slop"   # persistent: wallet + mic perm live here

# nohup + & + disown = survives the openclaw session that launched it.
nohup "$CANARY" \
    --user-data-dir="$USER_DATA_DIR" \
    --remote-debugging-port="$DEBUG_PORT" \
    "$URL" >/dev/null 2>&1 &
disown

echo "opened in Chrome Canary (debug profile): $URL"
echo "CDP endpoint for openclaw: http://127.0.0.1:$DEBUG_PORT/json"
