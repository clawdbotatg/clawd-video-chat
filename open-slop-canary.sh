#!/bin/bash
# open-slop-canary.sh — open a slop URL in the REAL openclaw Canary profile with
# the DevTools remote-debugging port ON, detached so an openclaw `/new` (or a
# claude -p session ending) can't kill it.
#
# THE openclaw profile (where the MetaMask wallet + slop login live) is openclaw's
# own managed browser dir — NOT a profile in the standard Chrome Canary dir:
#     user-data-dir = ~/.openclaw/browser/openclaw/user-data
#     profile-dir    = "Profile 1"   (the MetaMask extension + vault are here)
# (Confirmed from ~/clawd/clawd-md/metamask-slop-signin.md + the wallet's actual
# on-disk location. The sibling ~/.openclaw/browser/canary-persist is a fresh,
# wallet-less dir openclaw spun up on 2026-06-20.)
#
# Why no copy anymore: Chrome/Canary 136+ only ignores --remote-debugging-port on
# the DEFAULT user-data-dir. This profile already lives in its own non-default
# dir, so the debug port works on it DIRECTLY — we drive the real profile, with
# no point-in-time clone to drift out of sync. (The old version seeded a copy
# into ~/.clawd-canary-slop; that's obsolete — delete it if it's lying around.)
#
# How a claude -p session sees/controls it: the CDP endpoint belongs to the
# BROWSER process, not the session. Any claude -p attaches via
# http://127.0.0.1:9222/json → the slop tab's webSocketDebuggerUrl (see
# cc-cdp.py, which suppresses the Origin header to clear Chrome's WS check).
# `/new` just drops the CDP connection; Chrome stays up.
#
# Usage:  ./open-slop-canary.sh "https://live.slop.computer/pokernight?invite=XXXX"

URL="$1"
if [ -z "$URL" ]; then
    echo "usage: $0 <slop-url>" >&2
    exit 1
fi

CANARY="/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
DEBUG_PORT="${CANARY_DEBUG_PORT:-9222}"
USER_DATA_DIR="${CANARY_USER_DATA_DIR:-$HOME/.openclaw/browser/openclaw/user-data}"
PROFILE_DIR="${CANARY_PROFILE_DIR:-Profile 1}"

if [ ! -d "$USER_DATA_DIR/$PROFILE_DIR" ]; then
    echo "warning: openclaw profile not found at: $USER_DATA_DIR/$PROFILE_DIR" >&2
    echo "         (override with CANARY_USER_DATA_DIR / CANARY_PROFILE_DIR)" >&2
fi

# Can't run two Chrome instances on the same user-data-dir. If a Canary is
# already up on this dir, this just opens the URL in it (debug port persists).
nohup "$CANARY" \
    --user-data-dir="$USER_DATA_DIR" \
    --profile-directory="$PROFILE_DIR" \
    --remote-debugging-port="$DEBUG_PORT" \
    "$URL" >/dev/null 2>&1 &
disown

echo "opened slop in the openclaw Canary profile:"
echo "  user-data-dir = $USER_DATA_DIR"
echo "  profile       = $PROFILE_DIR"
echo "  URL           = $URL"
echo "CDP for claude -p: http://127.0.0.1:$DEBUG_PORT/json"
