#!/usr/bin/env python3
"""cc-cdp.py — minimal Chrome DevTools Protocol client for the slop Canary.

Foundation for the Claude-Code driver (branch: claude-code-driver). It attaches
to the debug Canary launched by ./open-slop-canary.sh (remote-debugging on
:9222) and lets us PERCEIVE (read DOM / screenshot) and ACT (eval JS, click,
type) on the slop tab — without launching its own browser.

This is the load-bearing spike: before building any brain loop, prove CDP can
actually read and drive the slop page.

Usage:
    python3 cc-cdp.py tabs                 # list open tabs (title + url)
    python3 cc-cdp.py eval 'document.title' # run JS in the slop tab, print result
    python3 cc-cdp.py shot out.png         # screenshot the slop tab
    python3 cc-cdp.py text                  # dump visible innerText of the slop tab

Tab selection: defaults to the first tab whose URL contains 'slop.computer'.
Override with --match SUBSTR or --url-contains SUBSTR.
"""
import argparse
import base64
import json
import sys
import urllib.request

from websocket import create_connection  # websocket-client (already installed)

DEBUG_PORT = 9222
DEFAULT_MATCH = "slop.computer"


def list_tabs(port=DEBUG_PORT):
    """Return the CDP /json tab list (page targets only)."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as r:
        targets = json.load(r)
    return [t for t in targets if t.get("type") == "page"]


def pick_tab(match=DEFAULT_MATCH, port=DEBUG_PORT):
    """Find the tab whose URL contains `match`. Raises if none/ambiguous-but-empty."""
    tabs = list_tabs(port)
    hits = [t for t in tabs if match in (t.get("url") or "")]
    if not hits:
        urls = "\n".join(f"  - {t.get('url')}" for t in tabs) or "  (no page tabs)"
        raise SystemExit(
            f"no tab matching {match!r} on :{port}. open tabs:\n{urls}\n"
            f"is the debug Canary running? (./open-slop-canary.sh <url>)"
        )
    return hits[0]


class CDP:
    """One-shot CDP session over a tab's webSocketDebuggerUrl."""

    def __init__(self, ws_url):
        # Chrome 111+ rejects the WS upgrade unless the Origin is allowlisted at
        # launch (--remote-allow-origins). Suppressing the Origin header sidesteps
        # that without needing the flag (works against an already-running browser).
        self.ws = create_connection(ws_url, max_size=None, suppress_origin=True)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        # CDP interleaves events with replies; read until our id comes back.
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expression, await_promise=False):
        res = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        result = res.get("result", {})
        if "value" in result:
            return result["value"]
        return result.get("description")

    def screenshot(self, path):
        res = self.call("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))
        return path

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["tabs", "eval", "shot", "text"])
    ap.add_argument("arg", nargs="?", help="JS expression (eval) or output path (shot)")
    ap.add_argument("--match", default=DEFAULT_MATCH, help="tab URL substring to target")
    ap.add_argument("--port", type=int, default=DEBUG_PORT)
    args = ap.parse_args(argv)

    if args.cmd == "tabs":
        for t in list_tabs(args.port):
            print(f"{t.get('title','')[:60]:60}  {t.get('url')}")
        return 0

    tab = pick_tab(args.match, args.port)
    cdp = CDP(tab["webSocketDebuggerUrl"])
    try:
        if args.cmd == "eval":
            if not args.arg:
                ap.error("eval needs a JS expression")
            print(json.dumps(cdp.evaluate(args.arg), indent=2, default=str))
        elif args.cmd == "text":
            print(cdp.evaluate("document.body && document.body.innerText"))
        elif args.cmd == "shot":
            out = args.arg or "slop.png"
            print("wrote", cdp.screenshot(out))
    finally:
        cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
