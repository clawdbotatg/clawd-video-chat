#!/usr/bin/env python3
"""
obs_bind_window.py — bind OBS's macOS Screen Capture source to the clawd
window *live*, over obs-websocket.

Why this exists: writing the window id into the scene JSON and letting OBS
load it cold does NOT reliably re-establish the ScreenCaptureKit stream on
macOS — OBS often ends up capturing the wrong window even though the saved
settings are correct (confirmed: a by-hand re-select produces byte-identical
JSON yet works). Selecting the window while OBS is running forces the rebind.
This script reproduces that by-hand action programmatically: it asks OBS for
the source's live window list, picks the clawd window by title, and pushes a
SetInputSettings update — exactly what the properties dialog does on OK.

No external config needed: host/port/password are read from OBS's own
obs-websocket config.json. Requires the `websocket-client` package (already
present on this machine).

Usage:
  obs_bind_window.py --input "macOS Screen Capture" --scene CLAWD \
      --match 7900,clawd-video-chat,clawd,127.0.0.1 [--fallback-window <cgwid>]

Exit 0 on success, non-zero on failure (the bridge treats failure as a
warning and tells the user to pick the window by hand).
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("websocket-client not installed (pip3 install websocket-client)")

WS_CONFIG = os.path.expanduser(
    "~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
)


def load_ws_config():
    with open(WS_CONFIG) as f:
        c = json.load(f)
    return {
        "port": c.get("server_port", 4455),
        "password": c.get("server_password", "") or "",
    }


def connect(port, password, timeout=20):
    """Connect + complete the obs-websocket v5 handshake (op0 Hello → op1
    Identify → op2 Identified). Retries until OBS's ws server is accepting."""
    url = f"ws://127.0.0.1:{port}"
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            ws = websocket.create_connection(url, timeout=5)
        except Exception as e:  # server not up yet
            last_err = e
            time.sleep(0.5)
            continue
        hello = json.loads(ws.recv())
        d = hello.get("d", {})
        identify = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
        auth = d.get("authentication")
        if auth:
            salt, challenge = auth["salt"], auth["challenge"]
            secret = base64.b64encode(
                hashlib.sha256((password + salt).encode()).digest()
            ).decode()
            identify["d"]["authentication"] = base64.b64encode(
                hashlib.sha256((secret + challenge).encode()).digest()
            ).decode()
        ws.send(json.dumps(identify))
        ident = json.loads(ws.recv())
        if ident.get("op") != 2:
            ws.close()
            raise SystemExit(f"obs-websocket identify failed: {ident}")
        return ws
    raise SystemExit(f"could not reach obs-websocket on {url}: {last_err}")


_rid = 0


def request(ws, rtype, data=None):
    global _rid
    _rid += 1
    rid = str(_rid)
    ws.send(json.dumps({"op": 6, "d": {
        "requestType": rtype, "requestId": rid, "requestData": data or {},
    }}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
            status = msg["d"].get("requestStatus", {})
            if not status.get("result"):
                raise SystemExit(
                    f"{rtype} failed: code={status.get('code')} "
                    f"{status.get('comment')}"
                )
            return msg["d"].get("responseData") or {}


def resolve_capture_input(ws, hint):
    """Find the screen_capture input by kind, so we don't depend on its name
    (OBS sometimes loses the custom 'CLAWDSCREEN' name → 'macOS Screen
    Capture'). Prefer an exact name match on `hint` if present."""
    inputs = request(ws, "GetInputList", {}).get("inputs", [])
    caps = [i for i in inputs
            if i.get("inputKind") == "screen_capture"
            or i.get("unversionedInputKind") == "screen_capture"]
    for i in caps:
        if i.get("inputName") == hint:
            return hint
    if len(caps) == 1:
        return caps[0]["inputName"]
    if not caps:
        raise SystemExit("no screen_capture input found in OBS")
    names = [i["inputName"] for i in caps]
    raise SystemExit(f"multiple screen_capture inputs ({names}); pass --input")


def pick_window(items, matches):
    """items: [{itemName, itemValue, itemEnabled}]. Return itemValue of the
    first window whose name contains a match string (in priority order)."""
    for needle in matches:
        n = needle.strip().lower()
        if not n:
            continue
        for it in items:
            if n in str(it.get("itemName", "")).lower():
                return it.get("itemValue"), it.get("itemName")
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="macOS Screen Capture")
    ap.add_argument("--scene", default="CLAWD")
    ap.add_argument("--match", default="7900,clawd-video-chat,clawd,127.0.0.1")
    ap.add_argument("--fallback-window", type=int, default=0,
                    help="CGWindow id to use if no window matches by title")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args()

    cfg = load_ws_config()
    ws = connect(cfg["port"], cfg["password"], timeout=args.timeout)
    try:
        # The websocket server comes up before OBS finishes loading the scene
        # collection, so the capture input may not exist for a beat. Retry.
        deadline = time.time() + args.timeout
        input_name = None
        while True:
            try:
                input_name = resolve_capture_input(ws, args.input)
                break
            except SystemExit as e:
                if "no screen_capture input" in str(e) and time.time() < deadline:
                    time.sleep(0.5)
                    continue
                raise
        # Live window list from the source itself (titles + CGWindow ids).
        # Best-effort: if this query fails or finds nothing, we still push the
        # fallback CGWindow id live — the live SetInputSettings is the fix, the
        # title match is just robustness against ids changing.
        value, name, titles = None, None, []
        try:
            resp = request(ws, "GetInputPropertiesListPropertyItems",
                           {"inputName": input_name, "propertyName": "window"})
            items = resp.get("propertyItems", [])
            titles = [it.get("itemName") for it in items]
            value, name = pick_window(items, args.match.split(","))
        except SystemExit as e:
            print(f"window-list query failed ({e}); using fallback id",
                  file=sys.stderr)
        if value is None and args.fallback_window:
            value, name = args.fallback_window, f"(fallback cgwid {args.fallback_window})"
        if value is None:
            raise SystemExit(
                f"no window matched {args.match!r} and no fallback given. "
                f"available windows: {titles}"
            )
        # Live update — this is the part that forces the ScreenCaptureKit
        # rebind that a cold JSON load misses.
        request(ws, "SetInputSettings", {
            "inputName": input_name,
            "inputSettings": {
                "type": 1,  # 1 = window capture
                "application": "com.google.Chrome",
                "window": value,
            },
            "overlay": True,
        })
        # Make sure the clawd scene is the one on program out.
        try:
            request(ws, "SetCurrentProgramScene", {"sceneName": args.scene})
        except SystemExit:
            pass  # scene name mismatch shouldn't fail the bind
        print(f"bound {input_name!r} → window {value} [{name}]")
    finally:
        ws.close()


if __name__ == "__main__":
    main()
