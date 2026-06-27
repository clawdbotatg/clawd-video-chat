#!/usr/bin/env python3
"""
clawd-backchannel — private side-channel chat into the same clawd session
that clawd-video-chat / clawd-web-chat use.

This server does two jobs:
  1. Serves the static HTML + a /config endpoint (stdlib http.server).
  2. Runs a WebSocket reverse-proxy so other machines on the LAN can use it.

Why the proxy: the browser talks to the OpenClaw gateway over a WebSocket,
but the gateway binds loopback-only. A phone/laptop elsewhere on the LAN
can load the page (we bind 0.0.0.0) but couldn't reach ws://127.0.0.1:<gw>.
The proxy listens on the LAN and relays each browser connection to the
loopback gateway — so the gateway is never exposed to the network and needs
no restart. /config hands the browser a wsUrl pointing at the proxy on
whatever host it loaded the page from, so localhost and LAN both just work.

Differences from clawd-web-chat:
  - Binds to 0.0.0.0 so a phone on the same LAN can reach it.
  - No TTS proxy, no autotitle, no filler — backchannel is text-only.
  - Default page port 7850; WS proxy on 7851.

Stdlib only, except the `websockets` package for the proxy (degrades to
localhost-only if it isn't installed).
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # proxy disabled; localhost-only direct connection
    websockets = None
    ConnectionClosed = Exception

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:  # proxy auth disabled
    Ed25519PrivateKey = None


class ThreadedHTTPServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def load_dotenv(path=".env"):
    env_path = Path(__file__).parent / path
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


load_dotenv()


def load_openclaw_config():
    path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[warn] could not parse {path}: {e}")
        return {}


def gateway_target():
    """The loopback gateway the proxy relays to (and direct-connect fallback)."""
    cfg = load_openclaw_config()
    gateway = cfg.get("gateway", {})
    port = gateway.get("port", 18789)
    token = (gateway.get("auth", {}) or {}).get("token", "")
    return {
        "wsUrl": os.environ.get("OPENCLAW_WS_URL") or f"ws://127.0.0.1:{port}",
        "token": os.environ.get("OPENCLAW_TOKEN") or token,
        "sessionKey": os.environ.get("OPENCLAW_SESSION_KEY") or "agent:clawd:main",
    }


PORT = int(os.environ.get("PORT", "7850"))
PROXY_PORT = int(os.environ.get("PROXY_PORT", str(PORT + 1)))
BIND = os.environ.get("BIND", "0.0.0.0")

# Quick-fill shortcuts persist HERE, server-side — NOT in browser localStorage.
# localStorage is keyed by origin (IP:port), so a DHCP IP change silently
# orphaned the user's saved shortcuts. This file is origin-independent: every
# client (phone on any IP, desktop on 127.0.0.1) reads/writes the same list.
# Gitignored — it's user data, not code.
SHORTCUTS_FILE = Path(__file__).parent / "shortcuts.json"


def read_shortcuts():
    """Return the saved shortcut list, or [] if none/unreadable."""
    try:
        data = json.loads(SHORTCUTS_FILE.read_text())
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[shortcuts] read failed: {e}", flush=True)
        return []


def write_shortcuts(items):
    """Atomically persist the shortcut list. Returns True on success."""
    try:
        tmp = SHORTCUTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2))
        tmp.replace(SHORTCUTS_FILE)
        return True
    except Exception as e:
        print(f"[shortcuts] write failed: {e}", flush=True)
        return False

# The Origin the proxy presents to the gateway. The gateway trusts loopback
# origins ("from the gateway host"), so use one here — that's what makes LAN
# clients work without editing the gateway's allowedOrigins.
GATEWAY_ORIGIN = os.environ.get("GATEWAY_ORIGIN") or f"http://127.0.0.1:{PORT}"


# ── Shared-secret gate ────────────────────────────────────────────────────────
# Both the page (7850) and the WS proxy (7851) bind 0.0.0.0, and the proxy
# authenticates EVERY connection to the gateway with the operator token. Without
# a gate, anyone on the LAN who reaches these ports gets operator-admin control.
# So require a shared secret `?k=<TOKEN>` on the page, /config, and the proxy.
# The token is persisted to .env so the URL stays stable across restarts/reboots.
def _load_or_create_token():
    tok = os.environ.get("BACKCHANNEL_TOKEN")
    if tok:
        return tok
    tok = secrets.token_urlsafe(24)
    env_path = Path(__file__).parent / ".env"
    try:
        with env_path.open("a") as f:
            f.write(f"\nBACKCHANNEL_TOKEN={tok}\n")
        print(f"[init] generated BACKCHANNEL_TOKEN and saved to {env_path}")
    except Exception as e:
        print(f"[warn] could not persist BACKCHANNEL_TOKEN to .env: {e}")
    os.environ["BACKCHANNEL_TOKEN"] = tok
    return tok


TOKEN = _load_or_create_token()


def _token_from_path(path):
    try:
        return parse_qs(urlparse(path or "").query).get("k", [""])[0]
    except Exception:
        return ""


# ── Proxy device identity (server-side Ed25519) ───────────────────────────────
# The browser used to do the gateway handshake itself, which needs WebCrypto —
# only available in a secure context (HTTPS / localhost), so a plain-HTTP LAN
# client couldn't connect. Instead the proxy does the handshake server-side and
# the browser becomes a thin client that needs no crypto.
#
# Each browser connection gets its OWN ephemeral Ed25519 identity (unique
# deviceId, shared clientId "openclaw-control-ui" which is required for session
# patching). Sharing ONE identity across connections makes the gateway treat
# concurrent clients as the same device and they starve each other — so phone +
# laptop at once would collide. Per-connection identities avoid that; the
# operator token authorizes each, so no device pairing is needed.
def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _new_identity():
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    device_id = hashlib.sha256(raw_pub).hexdigest()
    return priv, device_id, _b64url(raw_pub)


# Must mirror openclaw's buildDeviceAuthPayloadV3 EXACTLY (gateway protocol v4):
#   ["v3", deviceId, clientId, clientMode, role, scopes, signedAtMs, token,
#    nonce, platform, deviceFamily].join("|")
# platform/deviceFamily are normalized (trim + lowercase) and MUST equal the
# values sent in client.{platform,deviceFamily} below, or the gateway's
# signature reconstruction fails ("device nonce mismatch").
_PLATFORM = "web"        # matches client.platform in _connect_params
_DEVICE_FAMILY = ""      # client sends none → gateway normalizes to "" → match


def _sign_device(identity, token, nonce):
    priv, device_id, public_key = identity
    scopes = ["operator.read", "operator.write", "operator.admin"]
    signed_at = int(time.time() * 1000)
    parts = ["v3", device_id, "openclaw-control-ui", "ui", "operator",
             ",".join(scopes), str(signed_at), token or "", nonce,
             _PLATFORM, _DEVICE_FAMILY]
    sig = priv.sign("|".join(parts).encode())
    return {"id": device_id, "publicKey": public_key,
            "signature": _b64url(sig), "signedAt": signed_at, "nonce": nonce}


def _connect_params(identity, token, nonce):
    p = {
        "minProtocol": 4, "maxProtocol": 4,
        "client": {"id": "openclaw-control-ui", "displayName": "clawd-backchannel",
                   "version": "0.1.0", "platform": "web", "mode": "ui"},
        "role": "operator",
        "scopes": ["operator.read", "operator.write", "operator.admin"],
        "caps": ["tool-events"],
        "device": _sign_device(identity, token, nonce),
    }
    if token:
        p["auth"] = {"token": token}
    return p


async def _gateway_handshake(gw, identity, token):
    """Do the protocol-v4 connect handshake on the gateway socket server-side.
    The gateway sends an UNSOLICITED `connect.challenge` (with a nonce) right
    after the socket opens; we sign that nonce with a v3 device payload and send
    a single `connect`. Returns (ok, error_message)."""
    # 1) wait for the gateway's challenge nonce (don't send connect first)
    nonce = None
    for _ in range(8):
        frame = json.loads(await gw.recv())
        if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
            nonce = (frame.get("payload") or {}).get("nonce")
            break
        # ignore unrelated frames (tick/presence/etc.) before the challenge
    if not nonce:
        return False, "no connect.challenge nonce from gateway"
    # 2) send connect ONCE, signed with the challenge nonce
    await gw.send(json.dumps({"type": "req", "id": "c1", "method": "connect",
                              "params": _connect_params(identity, token, nonce)}))
    for _ in range(8):
        frame = json.loads(await gw.recv())
        if frame.get("type") == "res" and frame.get("id") == "c1":
            if frame.get("ok"):
                return True, None
            return False, (frame.get("error") or {}).get("message") or "connect rejected"
        # ignore unrelated frames during the connect round-trip
    return False, "no connect response from gateway"


# ── WebSocket reverse-proxy ───────────────────────────────────────────────────
async def _relay(client, gateway_url, token):
    """Authenticate to the loopback gateway server-side, then relay frames to
    the (crypto-free) browser. The browser waits for a synthetic `proxy.ready`
    event before it starts sending app RPCs."""
    # Gate: reject any connection that doesn't present the shared secret BEFORE
    # touching the gateway. Without this, any LAN client gets operator-admin.
    req_path = getattr(getattr(client, "request", None), "path", None)
    if req_path is None:
        req_path = getattr(client, "path", "")
    if not TOKEN or _token_from_path(req_path) != TOKEN:
        print(f"[proxy] rejected unauthorized connection (bad/missing ?k)", flush=True)
        try:
            await client.close(code=1008, reason="unauthorized")
        except Exception:
            pass
        return
    try:
        gw = await websockets.connect(
            gateway_url, max_size=None, ping_interval=None, open_timeout=10,
            # Present a loopback origin the gateway trusts, regardless of where
            # the browser actually loaded the page from — lets LAN clients work
            # without editing gateway.controlUi.allowedOrigins.
            origin=GATEWAY_ORIGIN,
        )
    except Exception as e:
        print(f"[proxy] gateway connect failed: {e}")
        try:
            await client.send(json.dumps({"type": "event", "event": "proxy.error",
                                          "payload": {"message": "gateway unavailable"}}))
            await client.close()
        except Exception:
            pass
        return

    identity = _new_identity()
    ok, err = await _gateway_handshake(gw, identity, token)
    if not ok:
        print(f"[proxy] gateway handshake failed: {err}")
        hint = err or "gateway auth failed"
        try:
            await client.send(json.dumps({"type": "event", "event": "proxy.error",
                                          "payload": {"message": hint}}))
            await client.close()
        except Exception:
            pass
        await gw.close()
        return

    try:
        await client.send(json.dumps({"type": "event", "event": "proxy.ready"}))
    except Exception:
        await gw.close()
        return

    async def pump(src, dst, label):
        try:
            async for msg in src:
                await dst.send(msg)
        except ConnectionClosed:
            pass
        except Exception as e:
            print(f"[proxy] {label} relay error: {e}", flush=True)
        finally:
            try:
                await dst.close()
            except Exception:
                pass

    await asyncio.gather(pump(client, gw, "ui→gw"), pump(gw, client, "gw→ui"))


async def _serve_proxy(bind, port, gateway_url, token):
    # max_size=None: chat.history frames can exceed the 1 MiB default.
    # ping_interval=None: the gateway speaks its own protocol; don't let
    # websocket-level keepalive pings tear down an idle session.
    async with websockets.serve(
        lambda c: _relay(c, gateway_url, token), bind, port,
        max_size=None, ping_interval=None,
    ):
        print(f"[proxy] ws proxy on {bind}:{port} → {gateway_url}")
        await asyncio.Future()  # run forever


def start_proxy_thread(bind, port, gateway_url, token):
    def run():
        try:
            asyncio.run(_serve_proxy(bind, port, gateway_url, token))
        except Exception as e:
            print(f"[proxy] crashed: {e}")
    threading.Thread(target=run, daemon=True).start()


# ── Static HTTP + /config ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":  # open: liveness probe leaks nothing
            self.send_json({"status": "ok"})
            return
        if _token_from_path(self.path) != TOKEN:
            self.send_error(403, "forbidden: missing or bad ?k token")
            return
        if path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/config":
            self.send_json(self.client_config())
        elif path == "/shortcuts":
            self.send_json(read_shortcuts())
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if _token_from_path(self.path) != TOKEN:
            self.send_error(403, "forbidden: missing or bad ?k token")
            return
        if path == "/shortcuts":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"[]"
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.send_json({"ok": False, "error": f"bad body: {e}"}, status=400)
                return
            if not isinstance(data, list):
                self.send_json({"ok": False, "error": "expected a JSON array"}, status=400)
                return
            # Normalize to {label, text} string pairs; drop anything malformed.
            clean = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                clean.append({
                    "label": str(item.get("label", "")),
                    "text": str(item.get("text", "")),
                })
            if write_shortcuts(clean):
                self.send_json({"ok": True, "count": len(clean)})
            else:
                self.send_json({"ok": False, "error": "could not write file"}, status=500)
        else:
            self.send_error(404)

    def client_config(self):
        """Point the browser at the proxy on the SAME host it loaded the page
        from, so localhost and LAN both work with zero manual config. If the
        proxy lib is unavailable, fall back to the direct loopback URL."""
        target = gateway_target()
        host = (self.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0]
        proxy = websockets is not None and Ed25519PrivateKey is not None
        if proxy:
            # Embed the shared secret so the browser's WS connect passes the
            # proxy gate; the browser never sees the gateway operator token.
            ws_url = f"ws://{host}:{PROXY_PORT}?k={TOKEN}"
        else:
            ws_url = target["wsUrl"]  # localhost-only direct connect
        return {
            "wsUrl": ws_url,
            "proxy": proxy,
            # Only the direct (no-proxy, loopback) path needs the gateway token;
            # never hand it to a proxied/LAN client.
            "token": "" if proxy else target["token"],
            "sessionKey": target["sessionKey"],
        }

    def serve_file(self, name, content_type):
        path = Path(__file__).parent / name
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def lan_ip():
    """Best-effort: surface the LAN address so phone access is one tap away."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    target = gateway_target()
    ip = lan_ip()
    proxy_ok = websockets is not None and Ed25519PrivateKey is not None
    print(f"📟 clawd-backchannel → http://127.0.0.1:{PORT}/?k={TOKEN}")
    if ip and BIND == "0.0.0.0":
        print(f"   LAN              → http://{ip}:{PORT}/?k={TOKEN}  ← open this on another machine")
    print(f"   shared token     → {TOKEN}  (required as ?k= on page + proxy; in .env)")
    print(f"   gateway (target) → {target['wsUrl']}  (loopback, not exposed)")
    print(f"   session          → {target['sessionKey']}")
    print(f"   token            → {'set' if target['token'] else 'MISSING — check ~/.openclaw/openclaw.json'}")
    if proxy_ok:
        start_proxy_thread(BIND, PROXY_PORT, target["wsUrl"], target["token"])
        if ip:
            print(f"   ws proxy         → ws://{ip}:{PROXY_PORT} (browser → proxy → gateway, server-side auth, per-conn identity)")
    else:
        missing = []
        if websockets is None:
            missing.append("websockets")
        if Ed25519PrivateKey is None:
            missing.append("cryptography")
        print(f"   [warn] {' + '.join(missing)} not installed — LAN proxy OFF; only localhost will connect.")
        print(f"          install with: pip3 install {' '.join(missing)}")
    try:
        ThreadedHTTPServer((BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
