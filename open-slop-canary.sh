#!/bin/bash
# open-slop-canary.sh — open a slop URL in a dedicated Chrome Canary profile with
# the DevTools remote-debugging port ON, fully detached so an openclaw `/new`
# (session tear-down) can't kill it.
#
# Why a dedicated --user-data-dir (and not the live "Default" profile):
#   Chrome/Canary 136+ IGNORES --remote-debugging-port when the default
#   user-data-dir is used (a security mitigation). To let openclaw attach via
#   CDP we point Canary at its own data dir. To avoid re-importing the wallet,
#   that dir is SEEDED once from a copy of the real Default profile — so the
#   MetaMask vault + slop mic permission come along. First run: just UNLOCK
#   MetaMask with your password (no seed phrase / reimport).
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
LIVE_DIR="$HOME/Library/Application Support/Google/Chrome Canary"  # real profile (read-only here)
USER_DATA_DIR="$HOME/.clawd-canary-slop"                          # debug profile (wallet + mic live here)

# Seed the debug profile once from the live Default profile (caches excluded).
# Reads the live profile only — never modifies it.
if [ ! -d "$USER_DATA_DIR/Default" ]; then
    echo "first run: seeding debug profile from live Default (wallet + mic perm)…"
    mkdir -p "$USER_DATA_DIR"
    rsync -a \
        --exclude 'Cache/' --exclude 'Code Cache/' --exclude 'GPUCache/' \
        --exclude 'Service Worker/CacheStorage/' --exclude 'DawnGraphiteCache/' \
        --exclude 'DawnWebGPUCache/' --exclude 'GraphiteDawnCache/' \
        --exclude 'Singleton*' \
        "$LIVE_DIR/Default" "$USER_DATA_DIR/"
    cp "$LIVE_DIR/Local State" "$USER_DATA_DIR/Local State" 2>/dev/null || true
    echo "seeded → unlock MetaMask with your password on first launch."
fi

# nohup + & + disown = survives the openclaw session that launched it.
nohup "$CANARY" \
    --user-data-dir="$USER_DATA_DIR" \
    --remote-debugging-port="$DEBUG_PORT" \
    "$URL" >/dev/null 2>&1 &
disown

echo "opened in Chrome Canary (debug profile): $URL"
echo "CDP endpoint for openclaw: http://127.0.0.1:$DEBUG_PORT/json"
