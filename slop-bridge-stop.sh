#!/bin/bash
# slop-bridge-stop.sh — full teardown for the clawd ↔ slop bridge:
#   1. Close any Chrome tabs pointing at the clawd server.
#   2. Kill anything listening on port 7900 (the clawd server).
#   3. Restore system audio defaults to what they were before bring-up.
# Does NOT quit Chrome, Chrome Canary, or OBS — close those manually if desired.

set -euo pipefail

STATE_FILE="$HOME/.cache/clawd/slop-bridge.state"
WATCH_PIDFILE="$HOME/.cache/clawd/slop-bridge-watch.pid"
CLAWD_HOST="http://127.0.0.1:7900"
CLAWD_PORT=7900

say()  { printf "\n\033[1;35m▸ %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m! %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# ── 0. Kill the audio-defaults watcher first ────────────────────────────────
if [ -f "$WATCH_PIDFILE" ]; then
    PID="$(cat "$WATCH_PIDFILE" 2>/dev/null || true)"
    if [ -n "$PID" ]; then
        say "Killing audio-defaults watcher (PID $PID)"
        kill "$PID" 2>/dev/null || true
    fi
    rm -f "$WATCH_PIDFILE"
fi

# ── 1. Close any Chrome tabs at the clawd page ──────────────────────────────
say "Closing Chrome tabs at ${CLAWD_HOST}…"
osascript >/dev/null 2>&1 <<APPLE || warn "Chrome AppleScript failed (Chrome not running?) — skipping."
tell application "Google Chrome"
    repeat with w in (every window)
        set victims to {}
        repeat with t in tabs of w
            if URL of t starts with "$CLAWD_HOST" then
                set end of victims to t
            end if
        end repeat
        repeat with t in victims
            try
                close t
            end try
        end repeat
    end repeat
end tell
APPLE

# ── 2. Kill the clawd server on port 7900 ───────────────────────────────────
PIDS="$(lsof -ti:"$CLAWD_PORT" 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
    say "Killing clawd server on port $CLAWD_PORT (PIDs: $PIDS)"
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
    for _ in $(seq 1 12); do
        sleep 0.25
        ALIVE="$(lsof -ti:"$CLAWD_PORT" 2>/dev/null || true)"
        [ -z "$ALIVE" ] && break
    done
    ALIVE="$(lsof -ti:"$CLAWD_PORT" 2>/dev/null || true)"
    if [ -n "$ALIVE" ]; then
        warn "Server still alive after SIGTERM — sending SIGKILL to $ALIVE"
        # shellcheck disable=SC2086
        kill -9 $ALIVE 2>/dev/null || true
    fi
else
    say "No clawd server running on port $CLAWD_PORT."
fi

# ── 3. Restore system audio defaults ────────────────────────────────────────
if [ ! -f "$STATE_FILE" ]; then
    warn "No state file at $STATE_FILE — leaving audio defaults alone."
else
    command -v SwitchAudioSource >/dev/null \
        || die "SwitchAudioSource not found. Install with:  brew install switchaudio-osx"

    # Parse key=value lines without sourcing — values can contain spaces
    # (e.g. "BlackHole 16ch"), which would break `. "$STATE_FILE"`.
    PREV_OUT=""
    PREV_IN=""
    while IFS='=' read -r key val; do
        case "$key" in
            PREV_OUT) PREV_OUT="$val" ;;
            PREV_IN)  PREV_IN="$val" ;;
        esac
    done < "$STATE_FILE"
    [ -n "$PREV_OUT" ] || die "PREV_OUT missing in $STATE_FILE"
    [ -n "$PREV_IN"  ] || die "PREV_IN missing in $STATE_FILE"

    say "Restoring system OUTPUT → $PREV_OUT"
    SwitchAudioSource -t output -s "$PREV_OUT" || true
    say "Restoring system INPUT  → $PREV_IN"
    SwitchAudioSource -t input  -s "$PREV_IN"  || true

    rm -f "$STATE_FILE"
fi

say "Done."
