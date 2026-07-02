#!/usr/bin/env python3
"""
clawd-video-chat — always-listening, OBS-feed-shaped UI for putting clawd
on a Zoom call. Same protocol bones as clawd-web-chat (browser does all
the WebSocket work, this Python server serves the static HTML and exposes
gateway config via /config), but the front-end is a fullscreen avatar on
chroma green that listens continuously and wakes on "hey claude" / "okay
claude". Pipe the page into OBS as a Browser Source, route OBS to a
virtual camera, and select that camera in Zoom.

No pip deps, stdlib only.
"""
import hmac
import json
import os
import queue
import socket
import ssl
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer


class ThreadedHTTPServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── SSE broadcast ─────────────────────────────────────────────────────────────
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()


def push_event(data: str):
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ── Load .env (optional) ─────────────────────────────────────────────────────
def load_dotenv(path=".env"):
    env_path = Path(__file__).parent / path
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # OVERRIDE, not setdefault: .env is THIS project's source of truth.
            # The clawd-harness leaks its own env (PORT=8787, its ELEVENLABS_VOICE_ID,
            # …) into every agent shell, so a server launched from an agent shell
            # inherits those. With setdefault the leaked value shadowed .env and
            # clawd ended up speaking in the harness's voice. Letting .env win makes
            # the voice correct no matter how the server is launched (and the
            # `unset` band-aid in slop-bridge.sh becomes redundant but harmless).
            os.environ[key.strip()] = val.strip()


load_dotenv()


# ── STT log ────────────────────────────────────────────────────────────────────
# The page POSTs every NON-echo finalized speech-recognition chunk it hears in
# the room to /api/stt-log, and a marker each time an "okay clawd" wake turn
# fires. We append it as JSONL here. This is the FULL room transcript — far more
# than what any single wake turn sends the brain (a wake turn only forwards the
# short wake-window utterance). The clawd -p brain is told about this file in its
# soul (~/clawd/clawd-harness/projects/claude-p-agent/CLAUDE.md) and can Read it to recall the whole call.
# Default lives inside the brain's cwd so it's readable without a permission prompt.
STT_LOG_PATH = os.path.expanduser(
    os.environ.get("STT_LOG_PATH", "~/clawd/clawd-harness/projects/claude-p-agent/stt-log.jsonl"))


def load_stt_rows(path=None):
    """Parse a JSONL STT log into a list of dicts (oldest-first). Bad lines skipped.

    Defaults to the global firehose (STT_LOG_PATH); pass a per-meeting file to
    read just that meeting's slice.
    """
    rows = []
    try:
        with open(path or STT_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


# ── Per-meeting transcripts ─────────────────────────────────────────────────────
# The STT log above is the always-on FIREHOSE: every non-echo chunk ever heard,
# one append-only file. A "meeting" carves a named slice out of it. When a meeting
# is active, handle_stt_log ALSO appends each heard line to a per-meeting file, so
# `meet summary` / `stt`-style reads can scope to exactly one Google Meet without
# grepping timestamps out of the firehose. State is a tiny pointer file (current.json)
# so it survives a server restart mid-call. Lives in the brain's cwd → readable by
# the clawd -p brain with no permission prompt (same reasoning as STT_LOG_PATH).
MEETINGS_DIR = os.path.expanduser(
    os.environ.get("MEETINGS_DIR",
                   "~/clawd/clawd-harness/projects/claude-p-agent/meetings"))
CURRENT_MEETING_PATH = os.path.join(MEETINGS_DIR, "current.json")


def meeting_file(mid):
    """Path to one meeting's transcript JSONL."""
    return os.path.join(MEETINGS_DIR, f"{mid}.jsonl")


def read_current_meeting():
    """The active meeting dict {id,title,url,started,started_ts}, or None."""
    try:
        with open(CURRENT_MEETING_PATH, encoding="utf-8") as f:
            m = json.load(f)
        return m if m.get("id") else None
    except (FileNotFoundError, ValueError):
        return None


def write_current_meeting(meeting):
    """Set (meeting dict) or clear (None) the active-meeting pointer."""
    os.makedirs(MEETINGS_DIR, exist_ok=True)
    if meeting is None:
        try:
            os.remove(CURRENT_MEETING_PATH)
        except FileNotFoundError:
            pass
        return
    with open(CURRENT_MEETING_PATH, "w", encoding="utf-8") as f:
        json.dump(meeting, f, ensure_ascii=False)


# ── Load gateway config from ~/.openclaw/openclaw.json ───────────────────────
def load_openclaw_config():
    path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[warn] could not parse {path}: {e}")
        return {}


def tts_backend():
    """Pick the active TTS backend by env var availability."""
    if os.environ.get("ELEVENLABS_API_KEY"):
        return "elevenlabs"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "none"


def resolve_agent_model(cfg, session_key):
    """Look up the agent's model by session key. sessionKey is `agent:<id>:<name>`."""
    if not session_key:
        return None
    parts = session_key.split(":")
    if len(parts) < 2 or parts[0] != "agent":
        return None
    agent_id = parts[1]
    for a in (cfg.get("agents") or {}).get("list", []) or []:
        if a.get("id") == agent_id:
            return a.get("model")
    # Fallback to default agent
    for a in (cfg.get("agents") or {}).get("list", []) or []:
        if a.get("default"):
            return a.get("model")
    return None


def context_window_for(model):
    """Context-window size (tokens) for a claude model id. Defaults to the
    standard 200k; the 1M-context beta tiers would override here if enabled."""
    m = (model or "").lower()
    if "[1m]" in m or "-1m" in m:
        return 1_000_000
    return 200_000


def resolve_gateway_settings():
    """Return {wsUrl, token, sessionKey, bankrKey, ttsBackend, model} — env vars override openclaw.json."""
    cfg = load_openclaw_config()
    gateway = cfg.get("gateway", {})
    port = gateway.get("port", 18789)
    token = (gateway.get("auth", {}) or {}).get("token", "")
    session_key = os.environ.get("OPENCLAW_SESSION_KEY") or "agent:clawd:main"

    return {
        "wsUrl": os.environ.get("OPENCLAW_WS_URL") or f"ws://127.0.0.1:{port}",
        "token": os.environ.get("OPENCLAW_TOKEN") or token,
        "sessionKey": session_key,
        "bankrKey": os.environ.get("BANKR_LLM_KEY") or "",
        "ttsBackend": tts_backend(),
        "model": resolve_agent_model(cfg, session_key) or "",
    }


# Steers the gpt-4o-mini-tts delivery — see /api/tts.
TTS_INSTRUCTIONS = (
    "Speak like a seasoned craftsman sharing hard-won wisdom. Unhurried. "
    "Warm. The kind of gravelly voice that comes from decades of real work. "
    "Confident because you've seen it all. Not corporate. Not polished. "
    "Just real. A guy you'd trust to build your house."
)


PORT = int(os.environ.get("PORT", "7900"))


def _page_token():
    """LAN auth key for this page (?k=<token>). Loopback never needs it.

    CLAWD_PAGE_TOKEN overrides; the default is the ?k= already embedded in
    OPENCLAW_WS_URL — i.e. the backchannel proxy key — so the whole rig
    shares ONE LAN key and the backchannel page can reuse its own token when
    it POSTs our /trigger-* endpoints cross-origin. Empty → LAN access is
    refused outright (fail closed); loopback still works.
    """
    tok = os.environ.get("CLAWD_PAGE_TOKEN", "")
    if tok:
        return tok
    try:
        q = urllib.parse.parse_qs(
            urllib.parse.urlsplit(os.environ.get("OPENCLAW_WS_URL", "")).query)
        return (q.get("k") or [""])[0]
    except Exception:
        return ""


PAGE_TOKEN = _page_token()

# ── Optional HTTPS listener ──────────────────────────────────────────────────
# Plain http on a LAN IP is not a secure origin, so Chrome strips
# navigator.mediaDevices (no mic / SR / setSinkId) for remote visitors. When a
# cert pair exists (mkcert-minted, see certs/), we ALSO serve the same Handler
# over TLS on TLS_PORT. The http listener stays untouched for the on-box rig.
# TLS visitors get a wss:// gateway URL from /config (an https page can't open
# ws:// — mixed content), pointing at the backchannel proxy's TLS twin.
TLS_PORT = int(os.environ.get("CLAWD_TLS_PORT", str(PORT + 1)))
TLS_CERT = os.environ.get("CLAWD_TLS_CERT",
                          str(Path(__file__).parent / "certs" / "lan.pem"))
TLS_KEY = os.environ.get("CLAWD_TLS_KEY",
                         str(Path(__file__).parent / "certs" / "lan-key.pem"))


def _wss_variant(ws_url):
    """The wss:// twin of a ws:// gateway URL — same host/query, port+1 (the
    backchannel proxy serves TLS on PROXY_TLS_PORT = its ws port + 1).
    OPENCLAW_WSS_URL overrides if the convention doesn't fit."""
    override = os.environ.get("OPENCLAW_WSS_URL")
    if override:
        return override
    try:
        parts = urllib.parse.urlsplit(ws_url)
        if parts.scheme != "ws" or not parts.port:
            return ws_url
        netloc = f"{parts.hostname}:{parts.port + 1}"
        return urllib.parse.urlunsplit(("wss", netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return ws_url


def _llm_chat_with_fallback(messages, max_tokens, bankr_model, venice_model,
                            anthropic_model, timeout=15, temperature=None):
    """Three-tier cascade: bankr → venice → anthropic-direct.

    Returns the assistant's raw content string. Each tier is skipped if its
    key isn't set. When a later tier is available, the earlier tier's timeout
    is capped so a hung provider can't burn the whole budget. Raises the last
    upstream exception if every available tier fails (or RuntimeError if none
    are configured).
    """
    bankr_key = os.environ.get("BANKR_LLM_KEY", "")
    venice_key = os.environ.get("VENICE_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    last_err = None

    if bankr_key:
        try:
            body_obj = {
                "model": bankr_model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if temperature is not None:
                body_obj["temperature"] = temperature
            body = json.dumps(body_obj).encode()
            req = urllib.request.Request(
                "https://llm.bankr.bot/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "X-API-Key": bankr_key},
                method="POST",
            )
            bt = min(5, timeout) if (venice_key or anthropic_key) else timeout
            with urllib.request.urlopen(req, timeout=bt) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e

    if venice_key:
        try:
            body_obj = {
                "model": venice_model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if temperature is not None:
                body_obj["temperature"] = temperature
            body = json.dumps(body_obj).encode()
            req = urllib.request.Request(
                "https://api.venice.ai/api/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {venice_key}"},
                method="POST",
            )
            vt = min(8, timeout) if anthropic_key else timeout
            with urllib.request.urlopen(req, timeout=vt) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e

    if anthropic_key:
        # Anthropic-messages API splits the system prompt out of `messages`.
        sys_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        payload = {
            "model": anthropic_model,
            "max_tokens": max_tokens,
            "messages": rest,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if sys_parts:
            payload["system"] = "\n\n".join(p for p in sys_parts if p)
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", "")
            return ""

    raise last_err or RuntimeError(
        "no LLM provider configured (BANKR_LLM_KEY / VENICE_API_KEY / ANTHROPIC_API_KEY)"
    )


# ── HTTP server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _authorized(self):
        """LAN auth gate. Loopback (the rig's own Chrome, slop-bridge, the
        backchannel server) is always allowed. Anyone else must present the
        rig key — ?k=<PAGE_TOKEN> in the URL, or the clawd_k cookie that the
        page's first tokened load set (so the page's same-origin fetches and
        EventSource pass without threading ?k= through every call site).
        The page can drive clawd — incl. the full-access flip — so this
        fails CLOSED: no token configured means no LAN access at all."""
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        if not PAGE_TOKEN:
            return False
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            k = (qs.get("k") or [""])[0]
        except Exception:
            k = ""
        if k and hmac.compare_digest(k, PAGE_TOKEN):
            self._set_auth_cookie = True
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, val = part.strip().partition("=")
            if name == "clawd_k" and hmac.compare_digest(val, PAGE_TOKEN):
                return True
        return False

    def _reject_unauthorized(self):
        self.send_json({"error": "missing or bad ?k= token"}, status=403)

    def do_GET(self):
        if not self._authorized():
            self._reject_unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif path in ("/audio-diag", "/audio-diag.html"):
            self.serve_file("audio-diag.html", "text/html; charset=utf-8")
        elif path == "/config":
            cfg = resolve_gateway_settings()
            cfg.pop("bankrKey", None)  # keep API key server-side only
            ws_url = cfg.get("wsUrl") or ""
            if getattr(self.server, "is_tls", False):
                # https page → must use the wss twin (mixed content rule)
                ws_url = _wss_variant(ws_url)
            cfg["wsUrl"] = self.rewrite_ws_host(ws_url)
            self.send_json(cfg)
        elif path == "/api/session-stats":
            self.handle_session_stats()
        elif path == "/health":
            self.send_json({"status": "ok"})
        elif path == "/api/meeting/status":
            self.handle_meeting_status()
        elif path == "/api/meeting/list":
            self.handle_meeting_list()
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=32)
            with _sse_lock:
                _sse_clients.append(q)
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    try:
                        _sse_clients.remove(q)
                    except ValueError:
                        pass
            return
        elif path.startswith("/clawdassets/"):
            name = path[len("/clawdassets/"):]
            if "/" in name or not name:
                self.send_error(404)
                return
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            mime = {"mp4": "video/mp4", "webm": "video/webm", "png": "image/png",
                    "jpg": "image/jpeg", "gif": "image/gif", "svg": "image/svg+xml"}.get(ext, "application/octet-stream")
            self.serve_file(f"clawdassets/{name}", mime)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._authorized():
            self._reject_unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/autotitle":
            self.handle_autotitle()
        elif path == "/api/filler":
            self.handle_filler()
        elif path == "/api/tts":
            self.handle_tts()
        elif path == "/api/stt-log":
            self.handle_stt_log()
        elif path == "/api/ask-transcript":
            self.handle_ask_transcript()
        elif path == "/api/should-respond":
            self.handle_should_respond()
        elif path == "/api/debug":
            self.handle_debug()
        elif path == "/api/meeting/start":
            self.handle_meeting_start()
        elif path == "/api/meeting/stop":
            self.handle_meeting_stop()
        elif path == "/api/meeting/summary":
            self.handle_meeting_summary()
        elif path == "/trigger-mic":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            push_event("toggle-mic")
            self.send_json({"ok": True})
        elif path == "/trigger-toggle-view":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            push_event("toggle-view")
            self.send_json({"ok": True})
        elif path == "/trigger-reveal-history":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            push_event("reveal-history")
            self.send_json({"ok": True})
        elif path == "/trigger-stop":
            # Cross-page PANIC STOP. The backchannel UI (:7850) POSTs here when
            # the user types /stop, so the voice cuts out IMMEDIATELY even when
            # no run is active (model done generating but TTS still draining).
            # The browser handles "stop" by hard-resetting the TTS pipeline.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            push_event("stop")
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif path in ("/trigger-ptt-down", "/trigger-ptt-up"):
            # PUSH-TO-TALK from the backchannel (:7850). Holding the button POSTs
            # ptt-down (start capturing speech, no wake word); releasing it POSTs
            # ptt-up (stop + submit immediately, no trailing-silence wait). A
            # perfectly-timed press/release replaces the "okay clawd" + silence-
            # gap dance. Cross-origin no-cors POST, so mirror /trigger-stop's CORS.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            push_event("ptt-down" if path.endswith("down") else "ptt-up")
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif path in ("/trigger-phone", "/trigger-trusted"):
            # Backchannel control buttons: relay an SSE toggle to the voice page,
            # which flips phone-call mode / full-access. Cross-origin no-cors POST,
            # so mirror /trigger-stop's CORS.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            push_event("toggle-phone" if path.endswith("phone") else "toggle-trusted")
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_error(404)

    def handle_stt_log(self):
        """Append one heard utterance — or a wake-turn marker — to STT_LOG_PATH.

        Body is either {"text": "<heard speech>"} for ambient room transcript,
        or {"wake": true, "sent": "<prompt forwarded to clawd>"} for the marker
        written when an "okay clawd" turn fires. Fire-and-forget from the page;
        we never let a logging failure break the call, so errors are swallowed
        into a 500 the client ignores.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        rec = {"ts": int(time.time() * 1000), "t": time.strftime("%Y-%m-%d %H:%M:%S")}
        if body.get("wake"):
            rec["wake"] = True
            rec["sent"] = (body.get("sent") or "").strip()
        else:
            text = (body.get("text") or "").strip()
            if not text:
                self.send_json({"ok": True, "skipped": True})
                return
            rec["text"] = text
        try:
            os.makedirs(os.path.dirname(STT_LOG_PATH), exist_ok=True)
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            with open(STT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            # If a Google Meet is in progress, mirror this line into its own
            # transcript so the meeting reads as a clean slice. Best-effort: a
            # per-meeting write failure must never break the firehose/call.
            meeting = read_current_meeting()
            if meeting:
                try:
                    with open(meeting_file(meeting["id"]), "a", encoding="utf-8") as mf:
                        mf.write(line)
                except Exception:
                    pass
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)
            return
        # Mirror to the live debug feed (SSE → backchannel page) so the user can
        # watch what clawd hears from their phone. Best-effort; never break logging.
        try:
            if rec.get("wake"):
                push_event(json.dumps({"ev": "vdbg", "kind": "sent", "msg": rec.get("sent", "")}))
            elif rec.get("text"):
                push_event(json.dumps({"ev": "vdbg", "kind": "heard", "msg": rec["text"]}))
        except Exception:
            pass
        self.send_json({"ok": True})

    def handle_debug(self):
        """Fan a debug line out to the live SSE feed (→ backchannel page) so the
        user can watch barge/gate/mic/phone events from their phone. Body:
        {kind: str, msg: str}. Fire-and-forget from the voice page; nothing is
        persisted. Non-STT companion to /api/stt-log's auto-push."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        kind = (body.get("kind") or "dbg").strip()[:24]
        msg = (body.get("msg") or "").strip()[:400]
        try:
            push_event(json.dumps({"ev": "vdbg", "kind": kind, "msg": msg}))
        except Exception:
            pass
        self.send_json({"ok": True})

    def handle_ask_transcript(self):
        """Answer a natural-language question about the room transcript via a
        cheap Bankr LLM call (Haiku by default).

        Semantic counterpart to `stt grep`: the page logs everything heard to
        STT_LOG_PATH; this loads the most-recent slice, hands it to a small fast
        model, and returns a one-line answer grounded ONLY in the transcript.
        Lets the (expensive) clawd brain ask "what is the magic number?" and get
        a crisp answer without reading the whole log into its own context.

        Body: {"q": str, "since"?: seconds, "limit"?: max recent lines,
               "model"?: bankr model id}. Returns {answer, model, lines}.
        """
        bankr_key = os.environ.get("BANKR_LLM_KEY", "")
        if not bankr_key:
            self.send_json({"error": "no bankr key"}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        question = (body.get("q") or body.get("question") or "").strip()
        if not question:
            self.send_json({"error": "missing question (q)"}, status=400)
            return
        model = (body.get("model") or "claude-haiku-4.5").strip()

        rows = load_stt_rows()
        # Optional recency window, then a hard cap on lines so the prompt stays
        # cheap. Most-recent-first selection, re-sorted oldest→newest for reading.
        since = body.get("since")
        if since:
            try:
                cutoff = time.time() * 1000 - float(since) * 1000
                rows = [r for r in rows if r.get("ts", 0) >= cutoff]
            except Exception:
                pass
        try:
            limit = int(body.get("limit") or 800)
        except Exception:
            limit = 800
        rows = rows[-limit:]
        if not rows:
            self.send_json({"answer": "Nothing's been heard on the call yet.",
                            "model": model, "lines": 0})
            return

        def line(r):
            t = (r.get("t", "") or "")[-8:]
            if r.get("wake"):
                return f"[{t}] (clawd was asked) {r.get('sent','')}"
            return f"[{t}] {r.get('text','')}"
        # Build newest-first under a char budget, then flip to chronological.
        budget, picked = 60000, []
        for r in reversed(rows):
            s = line(r)
            if budget - len(s) < 0:
                break
            picked.append(s)
            budget -= len(s) + 1
        transcript = "\n".join(reversed(picked))

        system = (
            "You answer questions about the transcript of a live audio/video "
            "call. The transcript is mic-derived speech-to-text and MAY contain "
            "mishearings, homophones, and dropped words — allow for that. Answer "
            "ONLY from what the transcript actually contains. Be very brief: a "
            "word or one short sentence, as if speaking it aloud. If the answer "
            "isn't in the transcript, say you didn't catch it — do NOT guess or "
            "use outside knowledge. Lines marked '(clawd was asked)' are prior "
            "questions put to clawd, for context."
        )
        user_msg = f"Transcript (most recent at the bottom):\n{transcript}\n\nQuestion: {question}"
        try:
            req_body = json.dumps({
                "model": model,
                "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            }).encode()
            req = urllib.request.Request(
                "https://llm.bankr.bot/v1/chat/completions",
                data=req_body,
                headers={"Content-Type": "application/json", "X-API-Key": bankr_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
            import re
            answer = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            self.send_json({"answer": answer, "model": model, "lines": len(picked)})
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_should_respond(self):
        """Phone-call turn gate. Given the latest user utterance (+ a little
        recent dialogue), a cheap fast model decides whether clawd should respond
        AT ALL — so continuous-conversation ("phone") mode doesn't reply to
        thinking-aloud, side-chatter, or nothing-to-add. The page calls this on
        every end-of-turn before it escalates to the (expensive) brain.

        Body: {utterance: str, history?: [{role, content}]}. Returns
        {respond: bool, reason: str}. On ANY error → {respond: true}: a gate
        hiccup must never make clawd go silent mid-conversation.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        utterance = (body.get("utterance") or "").strip()
        if not utterance:
            self.send_json({"respond": False, "reason": "empty"})
            return
        history = body.get("history") or []
        convo = []
        for h in history[-8:]:
            role = "clawd" if h.get("role") == "assistant" else "them"
            txt = (h.get("content") or "").strip()
            if txt:
                convo.append(f"{role}: {txt}")
        convo_txt = "\n".join(convo) if convo else "(start of call)"

        system = (
            "You are the turn-taking reflex of a voice AI ('clawd') on a live, "
            "phone-style call. You see recent dialogue and the latest thing the "
            "other person said (mic speech-to-text — it may be misheard or cut "
            "off). Decide if clawd should SPEAK NOW. Answer YES if they finished "
            "a thought and are handing over the floor or expecting a reply. "
            "Answer NO if they are clearly mid-sentence / thinking aloud (a "
            "trailing 'um', 'so...', 'let me', or an obvious fragment), if it is "
            "side-chatter not aimed at clawd, or if there is genuinely nothing "
            "worth saying. When unsure, lean YES — a real conversation keeps "
            "moving. Reply with STRICT JSON only: "
            '{"respond": true|false, "reason": "<a few words>"}'
        )
        user_msg = (
            f"Recent dialogue:\n{convo_txt}\n\n"
            f"They just said: \"{utterance}\"\n\nShould clawd respond now?"
        )
        try:
            raw = _llm_chat_with_fallback(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=40,
                bankr_model="claude-haiku-4.5",
                venice_model="llama-3.3-70b",
                anthropic_model="claude-haiku-4-5-20251001",
                timeout=6,
                temperature=0,
            )
        except Exception as e:
            self.send_json({"respond": True, "reason": f"gate error: {e}"})
            return

        import re
        cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
        respond, reason = True, ""
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                j = json.loads(m.group(0))
                respond = bool(j.get("respond", True))
                reason = str(j.get("reason", ""))[:80]
            except Exception:
                # Malformed JSON: fall back to a keyword read, biased to YES.
                low = cleaned.lower()
                respond = not ('"respond": false' in low or '"respond":false' in low)
        else:
            # No JSON at all — read a bare yes/no, defaulting to YES.
            low = cleaned.lower()
            respond = not (low.startswith("no") or low == "false" or "respond: no" in low)
        self.send_json({"respond": respond, "reason": reason or cleaned[:80]})

    # ── Per-meeting transcript endpoints ────────────────────────────────────
    def handle_meeting_start(self):
        """Begin a meeting: stamp an id, point current.json at it, open its file.

        Body: {title?, url?}. If a meeting is already active it is stopped first
        (one meeting at a time). Returns {id, title, url, started}.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        now = time.time()
        mid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        meeting = {
            "id": mid,
            "title": (body.get("title") or "").strip() or "Meeting",
            "url": (body.get("url") or "").strip(),
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "started_ts": int(now * 1000),
        }
        try:
            os.makedirs(MEETINGS_DIR, exist_ok=True)
            # touch the transcript file so reads don't 404 before the first line
            open(meeting_file(mid), "a", encoding="utf-8").close()
            write_current_meeting(meeting)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)
            return
        self.send_json({"ok": True, **meeting})

    def handle_meeting_stop(self):
        """End the active meeting: clear current.json, report line count + span."""
        meeting = read_current_meeting()
        if not meeting:
            self.send_json({"ok": True, "active": False})
            return
        rows = load_stt_rows(meeting_file(meeting["id"]))
        heard = [r for r in rows if not r.get("wake")]
        write_current_meeting(None)
        span_min = 0.0
        if rows:
            span_min = (rows[-1].get("ts", 0) - meeting.get("started_ts", rows[0].get("ts", 0))) / 60000.0
        self.send_json({"ok": True, "id": meeting["id"], "title": meeting.get("title"),
                        "lines": len(heard), "minutes": round(span_min, 1)})

    def handle_meeting_status(self):
        """Is a meeting active? How much has been captured so far?"""
        meeting = read_current_meeting()
        if not meeting:
            self.send_json({"active": False})
            return
        rows = load_stt_rows(meeting_file(meeting["id"]))
        heard = sum(1 for r in rows if not r.get("wake"))
        self.send_json({"active": True, "id": meeting["id"], "title": meeting.get("title"),
                        "url": meeting.get("url"), "started": meeting.get("started"),
                        "lines": heard})

    def handle_meeting_list(self):
        """List past meetings (newest first) from the meetings dir."""
        out = []
        try:
            names = sorted(
                (n for n in os.listdir(MEETINGS_DIR)
                 if n.endswith(".jsonl") and n != "current.json"),
                reverse=True)
        except FileNotFoundError:
            names = []
        current = read_current_meeting()
        for n in names[:50]:
            mid = n[:-len(".jsonl")]
            rows = load_stt_rows(os.path.join(MEETINGS_DIR, n))
            heard = sum(1 for r in rows if not r.get("wake"))
            out.append({"id": mid, "lines": heard,
                        "active": bool(current and current["id"] == mid)})
        self.send_json({"meetings": out})

    def handle_meeting_summary(self):
        """Summarize a meeting transcript via the cheap Bankr LLM (Haiku).

        Body: {id?, model?}. Defaults to the active meeting. Returns a structured
        recap (overview, key points, decisions, action items) grounded ONLY in
        the transcript. Counterpart to ask-transcript, but whole-meeting scope.
        """
        bankr_key = os.environ.get("BANKR_LLM_KEY", "")
        if not bankr_key:
            self.send_json({"error": "no bankr key"}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        mid = (body.get("id") or "").strip()
        if not mid:
            cur = read_current_meeting()
            if cur:
                mid = cur["id"]
        if not mid:
            self.send_json({"error": "no meeting id and none active"}, status=400)
            return
        model = (body.get("model") or "claude-haiku-4.5").strip()

        rows = load_stt_rows(meeting_file(mid))
        heard = [r for r in rows if not r.get("wake")]
        if not heard:
            self.send_json({"summary": "Nothing was transcribed for this meeting.",
                            "model": model, "lines": 0, "id": mid})
            return

        def line(r):
            t = (r.get("t", "") or "")[-8:]
            return f"[{t}] {r.get('text','')}"
        # Newest-first under a char budget, then flip chronological.
        budget, picked = 90000, []
        for r in reversed(heard):
            s = line(r)
            if budget - len(s) < 0:
                break
            picked.append(s)
            budget -= len(s) + 1
        transcript = "\n".join(reversed(picked))

        system = (
            "You summarize the transcript of a live video meeting. The transcript "
            "is mic-derived speech-to-text and MAY contain mishearings, homophones, "
            "and dropped words — read past them. Produce a concise recap grounded "
            "ONLY in what the transcript says; never invent facts or use outside "
            "knowledge. Format as short markdown sections: **Overview** (1-2 "
            "sentences), **Key points** (bullets), **Decisions** (bullets, or "
            "'none'), **Action items** (bullets with who if stated, or 'none'). If "
            "the transcript is too thin to summarize, say so plainly."
        )
        user_msg = f"Meeting transcript (most recent at the bottom):\n{transcript}\n\nWrite the recap."
        try:
            req_body = json.dumps({
                "model": model,
                "max_tokens": 900,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            }).encode()
            req = urllib.request.Request(
                "https://llm.bankr.bot/v1/chat/completions",
                data=req_body,
                headers={"Content-Type": "application/json", "X-API-Key": bankr_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
            import re
            summary = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            self.send_json({"summary": summary, "model": model,
                            "lines": len(picked), "id": mid})
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_autotitle(self):
        bankr_key = os.environ.get("BANKR_LLM_KEY", "")
        if not bankr_key:
            self.send_json({"error": "no bankr key"}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages", [])
            req_body = json.dumps({
                "model": "minimax-m2.7",
                "max_tokens": 200,
                "messages": [
                    {"role": "system", "content": "You generate ultra-short chat tab titles. Reply with ONLY 2-3 words, no punctuation, no explanation, no thinking."},
                ] + messages,
            }).encode()
            req = urllib.request.Request(
                "https://llm.bankr.bot/v1/chat/completions",
                data=req_body,
                headers={"Content-Type": "application/json", "X-API-Key": bankr_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
            # strip <think>...</think> reasoning blocks
            import re
            title = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            title = re.sub(r'["""\'\'.,!?:;]', "", title).strip()[:40]
            self.send_json({"title": title})
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_filler(self):
        """Bankr/Haiku stall-talk while the main model is still answering.

        Three kinds:
          - "ack":      quick verbal acknowledgement of the user's question
          - "tool":     casual narration of a tool call
          - "thinking": paraphrase of the assistant's inner reasoning delta
        Always answers in <=14 words, first person, no quotes, never answers
        the user's actual question.
        """
        if not (os.environ.get("BANKR_LLM_KEY") or os.environ.get("VENICE_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")):
            self.send_json({"error": "no LLM provider configured"}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            kind = body.get("kind") or "ack"
            history = body.get("history") or []
            last_user = (body.get("lastUser") or "").strip()[:600]
            tool_name = (body.get("toolName") or "").strip()[:60]
            tool_input = (body.get("toolInput") or "").strip()[:300]
            thinking_text = (body.get("thinkingText") or "").strip()[:600]

            if kind == "ack":
                # The ack fills the ~1s of dead air before the real claude -p
                # brain replies. It SEES the question, gauges DIFFICULTY, and
                # stalls accordingly — but it must NEVER answer. The danger of
                # showing it the question is that it slips into answering ("Yes,
                # I can hear you"), which then double-speaks once the real brain
                # answers the same thing. So the prompt is built entirely around
                # difficulty-gauging, never content:
                #   SIMPLE -> a bare thinking sound ("hmm", "mmm", "uhh") — just
                #             enough to cover the beat, never a word/answer.
                #   MEDIUM -> a brief non-committal stall ("let me think on that")
                #   HARD   -> acknowledge it's a tough one + that it'll take work,
                #             gesturing at WHY in general terms, never solving it.
                system = (
                    "You are clawd's voice, filling dead air OUT LOUD in the "
                    "~second before a smarter model delivers the real answer "
                    "to what the user just asked. You do NOT answer — you only "
                    "react to HOW HARD the request is, then stall accordingly. "
                    "Your reaction is spoken aloud on a live call.\n"
                    "\n"
                    "GOLDEN RULE — your length must match the WAIT. You are "
                    "covering a gap of silence: the bigger/longer the smart "
                    "model's job, the LONGER you should talk; the quicker its "
                    "answer, the SHORTER you should be. Picture how long it'll "
                    "take to think, and fill exactly that. Too short over a long "
                    "think = awkward silence; too long over a quick answer = you "
                    "run into the real reply. So scale yourself continuously, "
                    "not in fixed sizes.\n"
                    "\n"
                    "Silently gauge the difficulty of the user's request:\n"
                    "\n"
                    "SIMPLE — a yes/no, 'can you hear me', a trivial fact or "
                    "quick ask the smart model answers almost instantly. A "
                    "wordy stall would just step on the real reply.\n"
                    "  -> Just a SHORT, bare thinking sound — one little noise, "
                    "nothing more, no words: 'Hmm.' 'Hmmm.' 'Hrmm.' 'Mmm.' "
                    "'Umm.' 'Uhh.' 'Mm-hm.' Lean very short. NOT a phrase, NOT "
                    "'let me think' — just the sound.\n"
                    "\n"
                    "MEDIUM — takes a beat of thought but isn't deep: a normal "
                    "question, a small lookup, a short explanation.\n"
                    "  -> A brief, non-committal stall. Vary it: 'Let me think "
                    "about that.' 'Hmm, let me look at that.' 'Give me a sec.' "
                    "'One moment.' 'Okay, let me pull that up.'\n"
                    "\n"
                    "HARD — complex, multi-step, open-ended, research / coding "
                    "/ architectural, or genuinely nuanced; the kind of thing "
                    "that takes real work.\n"
                    "  -> The BEST hard stall MIRRORS THE ASK back in your own "
                    "casual words — a TL;DR of what THEY want — then signals "
                    "it'll take a moment. Restating the request is encouraged "
                    "and is NOT answering. This reflection is what makes each "
                    "stall DIFFERENT, so prefer it.\n"
                    "  -> SCALE THE MIRROR TO THE EXPECTED THINK TIME (golden "
                    "rule). For a hard-but-fairly-contained ask the model will "
                    "answer in a few seconds, keep the mirror to ONE short "
                    "clause: 'Right, untangling the gateway dropout — gimme a "
                    "sec.' For a genuinely deep, multi-step, research / coding / "
                    "architectural ask that'll take real time, go LONGER and "
                    "more detailed — walk back through the whole ask, naming "
                    "each part, to fill the bigger gap: 'Okay, so you want me to "
                    "rework the whole audio routing, stop the bridge from "
                    "dropping the gateway, AND layer in automatic fallbacks "
                    "across the blackhole devices — yeah, that's a real one, "
                    "let me genuinely dig in here.' The harder it is, the more "
                    "of their ask you play back — but SUMMARIZE each part in a "
                    "few words, don't transcribe it verbatim, and ALWAYS finish "
                    "your sentence and land the closing stall ('let me dig in', "
                    "'gimme a sec'). Never trail off mid-thought.\n"
                    "  -> You may instead (or also) just signal it's a real one "
                    "and you need a beat. But VARY THE WORDING HARD. Do NOT keep "
                    "reaching for the same opener. Rotate through openers like: "
                    "'Oof, okay.' 'Right, so.' 'Alright.' 'Hmm, juicy one.' "
                    "'Ooh.' 'Okay, meaty.' 'Buckle up.' 'Yeah, this'll take a "
                    "sec.' 'Let me actually think.' 'Big swing here.' 'Spicy.' "
                    "'Okay this is real.' 'Genuinely gotta work for this one.' "
                    "'Heh, not a quick one.' 'Deep cut.' 'Solid one.' 'Gonna "
                    "have to chew on this.' 'Whew.' 'Layered, this.' 'Real "
                    "homework here.' — and phrasings well beyond this list.\n"
                    "  -> BANNED because they're overused: 'a lot of moving "
                    "parts', 'big one' as your default, 'lots going on'. Reach "
                    "for something fresher every time.\n"
                    "  -> You must NOT start answering it or reveal the actual "
                    "answer or the specific steps you'll take — mirroring WHAT "
                    "they asked is fine; giving any of the HOW/answer is not.\n"
                    "\n"
                    "Hard rules (all tiers):\n"
                    "  - NEVER answer, confirm, deny, hint at, or state any "
                    "fact or opinion that addresses the request. Commenting "
                    "that it's hard is fine; solving any part of it is not.\n"
                    "  - NEVER reveal the actual content of the answer or the "
                    "specific approach you'll take.\n"
                    "  - NEVER echo greetings, thanks, or pleasantries.\n"
                    "  - First person, casual, spoken-aloud. No quotes, no "
                    "emojis.\n"
                    "  - Output ONLY the sound (SIMPLE) or the spoken stall "
                    "(MEDIUM / HARD) — nothing else."
                )
                user_msg = (
                    f"The user just asked:\n{last_user}\n\n"
                    "Gauge its difficulty (SIMPLE / MEDIUM / HARD) and how long "
                    "the smart model will take, then respond per your rules, "
                    "scaling your LENGTH to that wait (golden rule): a bare "
                    "thinking sound ('hmm') if SIMPLE, a brief stall if MEDIUM, "
                    "or — if HARD — mirror back what they're asking in your own "
                    "words (short clause if it'll answer fast; longer, naming "
                    "each part of the ask, if it'll take real thinking) plus a "
                    "'gimme a sec' (fresh wording, never 'moving parts'/'big "
                    "one'). Do NOT answer the question."
                )
            elif kind == "tool":
                system = (
                    "You narrate, casually and out loud, what an AI assistant "
                    "is about to do with a tool. One short sentence, under 14 "
                    "words, first person. No quotes, no emojis."
                )
                user_msg = (
                    f"Tool: {tool_name}\nInput: {tool_input}\n\n"
                    "Casually narrate in one short sentence what you're "
                    "about to do. Examples: 'Let me grep for that.', "
                    "'Pulling up the file now.', 'Running a quick check.'"
                )
            else:  # thinking / reasoning — a CONTINUED stall, never the content
                # IMPORTANT: do NOT paraphrase the model's reasoning. For simple
                # questions the reasoning IS the answer ("they asked if I can
                # hear them — yes, loud and clear"), so paraphrasing leaks it
                # out loud before the real reply. This kind is ONLY a "still
                # thinking" noise; we deliberately do NOT pass thinking_text in.
                system = (
                    "You are a voice filling dead air out loud while a smarter "
                    "model is STILL composing the real answer — it's been "
                    "thinking for a moment. Your ONLY job is a short, natural "
                    "'still working on it' stall. You are NOT answering and you "
                    "do NOT know the answer.\n"
                    "\n"
                    "Examples: 'Still thinking on this one.' 'Hmm, lemme keep "
                    "digging.' 'Bear with me a sec.' 'Almost there.' 'Working "
                    "through it.' 'Still on it.' 'One more moment.' 'Yeah, "
                    "lemme sit with this.'\n"
                    "\n"
                    "Hard rules:\n"
                    "  - NEVER answer, hint at, or reveal any part of the answer "
                    "or the model's reasoning. No facts, no conclusions.\n"
                    "  - NEVER name the topic or repeat words from the question.\n"
                    "  - Under 12 words, first person, spoken-aloud, no quotes, "
                    "no emojis. Output ONLY the stall."
                )
                user_msg = (
                    "The model is still thinking. Give ONE short, fresh "
                    "'still working on it' stall. Do NOT reveal or hint at "
                    "anything it's thinking about."
                )

            msgs = [{"role": "system", "content": system}]
            # The "ack" never gets conversation context — see the comment in its
            # branch above. Showing it the recent turns is another way it leaks
            # into answering. Only tool/thinking narration gets history.
            if history and kind != "ack":
                ctx = "\n".join(
                    f"{m.get('role')}: {(m.get('content') or '')[:300]}"
                    for m in history[-4:] if m.get("role") and m.get("content")
                )
                if ctx:
                    msgs.append({"role": "user", "content": f"Recent context (for reference, do not respond to it):\n{ctx}"})
                    msgs.append({"role": "assistant", "content": "Got it."})
            msgs.append({"role": "user", "content": user_msg})

            raw = _llm_chat_with_fallback(
                msgs,
                # Headroom for the longest HARD mirror (it scales up to a full
                # play-back of a deep multi-part ask); short stalls stay short.
                # Generous so the verbose case can land its closer instead of
                # truncating mid-word — the prompt keeps it from rambling.
                max_tokens=200,
                bankr_model="claude-haiku-4.5",
                venice_model="claude-sonnet-4-6",
                anthropic_model="claude-haiku-4-5",
                timeout=10,
                # ack: moderate temp — it has to gauge difficulty RELIABLY
                # (the silence/medium/hard call matters more than freshness),
                # while keeping the stall wording from going stale.
                # thinking: high temp — pure stall, freshness is its whole value.
                temperature=(0.85 if kind == "ack" else 1.0 if kind == "thinking" else None),
            )
            import re
            text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            text = text.strip("\"'`").strip()
            # Cap generously — the HARD mirror scales up to a full multi-part
            # play-back to cover a long think; 240 chopped those mid-word. If it
            # still overruns, cut at the last sentence boundary so we never end
            # on a half-word.
            if len(text) > 500:
                clipped = text[:500]
                cut = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
                text = clipped[:cut + 1] if cut > 200 else clipped
            # SIMPLE acks are a bare thinking sound. Haiku almost always picks
            # the SAME one ("Hmm."), so whenever the ack comes back as just a
            # filler sound (or the old NONE sentinel), swap in a random one for
            # variety on a live call. MEDIUM/HARD stalls have real words and
            # won't match, so they pass through untouched.
            if kind == "ack" and re.fullmatch(
                    r"\W*(?:h+m+|m+h?|u+h+|u+m+|h+r+m+|e+r+m?|mm[-\s]?hm|none)\W*",
                    text, flags=re.IGNORECASE):
                text = random.choice(
                    ["Hmm.", "Hmmm.", "Hrmm.", "Hummm.", "Mmm.", "Umm.",
                     "Uhh.", "Mm-hm.", "Hm."])
            self.send_json({"text": text})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            self.send_json({"error": f"upstream {e.code}: {detail}"}, status=502)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_session_stats(self):
        """Report the LIVE claude -p brain session's real context usage.

        The brain is now the claude -p cc-bridge (not openclaw), so the old
        `openclaw sessions --json` store is frozen — it always read back the
        same stale ~9.5k/200k. Instead we read the active claude transcript
        JSONL for the brain's cwd and sum the last assistant turn's usage
        (input + cache_read + cache_creation = tokens currently sitting in
        the model's context window). That's the number that actually moves
        toward compaction, so it's the one worth showing on the chip."""
        # The brain's cwd — same default cc-bridge.py uses (~/clawd/clawd-harness/projects/claude-p-agent).
        cwd = os.environ.get("CC_BRIDGE_CWD") or os.path.expanduser("~/clawd/clawd-harness/projects/claude-p-agent")
        cwd = os.path.realpath(cwd)
        # claude names a project dir by replacing every non-alphanumeric char
        # in the abspath with '-' (e.g. /Users/x/.clawd-agent →
        # -Users-x--clawd-agent).
        slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        proj = Path.home() / ".claude" / "projects" / slug
        # Prefer the sentinel cc-bridge writes (the LIVE brain session id) so we
        # read OUR session — NOT whatever transcript is newest in this dir.
        # Operator/dev `claude` sessions run in the same cwd; "newest file" would
        # otherwise show their context (and /new could never clear it).
        jsonl = None
        session_key = os.environ.get("OPENCLAW_SESSION_KEY") or "agent:clawd:main"
        sentinel = (Path.home() / ".cache" / "clawd" /
                    f"brain-session-{''.join(c if c.isalnum() else '-' for c in session_key)}.json")
        try:
            sid = json.loads(sentinel.read_text()).get("sessionId")
            if sid and (proj / f"{sid}.jsonl").exists():
                jsonl = proj / f"{sid}.jsonl"
        except Exception:
            pass
        if jsonl is None:   # no sentinel yet → fall back to newest file
            files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                           reverse=True) if proj.exists() else []
            if not files:
                self.send_json({"sessionKey": "claude-p:brain", "exists": False, "tokens": 0})
                return
            jsonl = files[0]
        last_usage = None
        model = None
        try:
            with open(jsonl, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Skip Task-subagent turns — their usage is a *separate*
                    # context, not the main thread's.
                    if e.get("isSidechain"):
                        continue
                    msg = e.get("message") or {}
                    if msg.get("role") == "assistant" and isinstance(msg.get("usage"), dict):
                        last_usage = msg["usage"]
                        model = msg.get("model") or model
        except Exception as ex:
            self.send_json({"error": str(ex)}, status=500)
            return
        if not last_usage:
            self.send_json({"sessionKey": "claude-p:brain", "exists": True,
                            "tokens": 0, "sessionId": jsonl.stem, "model": model})
            return
        ctx = (int(last_usage.get("input_tokens", 0) or 0)
               + int(last_usage.get("cache_read_input_tokens", 0) or 0)
               + int(last_usage.get("cache_creation_input_tokens", 0) or 0))
        self.send_json({
            "sessionKey": "claude-p:brain",
            "exists": True,
            "sessionId": jsonl.stem,
            "model": model,
            "tokens": ctx,                              # live context-window occupancy
            "tokensFresh": True,
            "contextTokens": context_window_for(model),
            "updatedAt": int(jsonl.stat().st_mtime * 1000),
            "kind": "claude-p",
        })

    def handle_tts(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            text = (body.get("text") or "").strip()
            if not text:
                self.send_json({"error": "empty text"}, status=400)
                return
            backend = tts_backend()
            voice = os.environ.get("ELEVENLABS_VOICE_ID", "<default>")[:12]
            print(f"[tts] backend={backend} voice={voice} chars={len(text)} sample={text[:60]!r}", flush=True)
            if backend == "elevenlabs":
                self._tts_elevenlabs(text[:4000])
            elif backend == "openai":
                self._tts_openai(text[:4000])
            else:
                print(f"[tts] WARN: no backend configured — returning 503", flush=True)
                self.send_json({"error": "no tts backend configured"}, status=503)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            self.send_json({"error": f"upstream {e.code}: {detail}"}, status=502)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def _tts_elevenlabs(self, text):
        api_key = os.environ["ELEVENLABS_API_KEY"]
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID") or "nPczCjzI2devNBz1zQrb"  # Brian
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            "?optimize_streaming_latency=3&output_format=mp3_44100_64"
        )
        req_body = json.dumps({
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {
                "stability": 0.65,
                "similarity_boost": 0.5,
                "use_speaker_boost": True,
                "speed": 1.2,
            },
        }).encode()
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        # Pipe ElevenLabs' chunked response straight through to the browser so
        # the first audio bytes hit the client as soon as they're generated.
        # No Content-Length + Connection: close = "read until EOF" streaming
        # (works on HTTP/1.0 without chunked transfer encoding).
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

    def _tts_openai(self, text):
        api_key = os.environ["OPENAI_API_KEY"]
        req_body = json.dumps({
            "model": "gpt-4o-mini-tts",
            "voice": "onyx",
            "input": text,
            "instructions": TTS_INSTRUCTIONS,
            "response_format": "mp3",
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            audio = resp.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def rewrite_ws_host(self, ws_url):
        """Make the gateway wsUrl reachable from a remote LAN browser.

        The configured URL (OPENCLAW_WS_URL) points at 127.0.0.1 — the
        backchannel proxy on THIS box. A browser on another machine resolves
        127.0.0.1 to its own loopback and the connect fails, so swap the
        loopback host for whatever hostname the browser used to reach us
        (the Host header). The proxy binds 0.0.0.0 and its ?k= auth token
        rides along in the query string, so the same port+token work from
        the LAN. Loopback visitors are untouched (their Host is loopback
        too), and a non-loopback wsUrl is passed through as-is.
        """
        try:
            parts = urllib.parse.urlsplit(ws_url)
            if parts.hostname not in ("127.0.0.1", "localhost"):
                return ws_url
            req_host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if not req_host or req_host in ("127.0.0.1", "localhost"):
                return ws_url
            netloc = req_host + (f":{parts.port}" if parts.port else "")
            return urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            return ws_url

    def serve_file(self, name, content_type):
        path = Path(__file__).parent / name
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        if getattr(self, "_set_auth_cookie", False):
            # LAN visitor arrived with a valid ?k= — hand them a cookie so the
            # page's same-origin fetches/EventSource authenticate without
            # threading ?k= through every call site.
            secure = "; Secure" if getattr(self.server, "is_tls", False) else ""
            self.send_header("Set-Cookie",
                             f"clawd_k={PAGE_TOKEN}; Path=/; SameSite=Lax; HttpOnly{secure}")
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


def check_allowed_origins(port):
    """Warn if the gateway isn't configured to accept our origin."""
    cfg = load_openclaw_config()
    allowed = (((cfg.get("gateway") or {}).get("controlUi") or {}).get("allowedOrigins") or [])
    needed = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    missing = [o for o in needed if o not in allowed]
    if not missing:
        return
    print()
    print("⚠  gateway.controlUi.allowedOrigins is missing our origin.")
    print("   The openclaw gateway will reject our WebSocket unless you add:")
    print()
    print("     \"gateway\": {")
    print("       \"controlUi\": {")
    print(f"         \"allowedOrigins\": {json.dumps(needed)}")
    print("       }")
    print("     }")
    print()
    print("   Then restart the gateway. (See README for details.)")
    print()


if __name__ == "__main__":
    settings = resolve_gateway_settings()
    print(f"🕸️  clawd-video-chat → http://127.0.0.1:{PORT}")
    print(f"   gateway          → {settings['wsUrl']}")
    print(f"   session          → {settings['sessionKey']}")
    print(f"   token            → {'set' if settings['token'] else 'MISSING — check ~/.openclaw/openclaw.json'}")
    print(f"   tts backend      → {settings['ttsBackend']}")
    if PAGE_TOKEN:
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            _lan_ip = _s.getsockname()[0]
            _s.close()
            print(f"   LAN              → http://{_lan_ip}:{PORT}/?k={PAGE_TOKEN}")
        except Exception:
            pass
    else:
        print("[warn] no page token (CLAWD_PAGE_TOKEN / OPENCLAW_WS_URL ?k=) — LAN visitors are refused; loopback only")
    # HTTPS twin: same Handler, TLS socket — gives LAN visitors a real secure
    # origin (mic/SR/setSinkId work with no chrome flags). Runs alongside the
    # http listener; the on-box rig keeps using plain http on loopback.
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        try:
            _ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _ctx.load_cert_chain(TLS_CERT, TLS_KEY)
            _tls_httpd = ThreadedHTTPServer(("0.0.0.0", TLS_PORT), Handler)
            _tls_httpd.socket = _ctx.wrap_socket(_tls_httpd.socket, server_side=True)
            _tls_httpd.is_tls = True
            threading.Thread(target=_tls_httpd.serve_forever, daemon=True).start()
            _host = socket.gethostname()  # e.g. atgsilver-4.local
            _kq = f"/?k={PAGE_TOKEN}" if PAGE_TOKEN else "/"
            print(f"   HTTPS            → https://{_host}:{TLS_PORT}{_kq}  ← LAN, no chrome flags (trust the mkcert root CA once)")
        except Exception as _e:
            print(f"[warn] HTTPS listener failed: {_e}")
    else:
        print(f"   [info] no TLS cert at {TLS_CERT} — HTTPS listener off (mkcert can mint one; see certs/)")
    if not settings["token"]:
        print("[warn] no gateway token found — the UI will prompt you to paste one")
    print("   tip: ?dev=1 in the URL drops OBS mode and shows the full chat UI.")
    check_allowed_origins(PORT)
    try:
        # Bind 0.0.0.0 (LAN) so the backchannel — which may be open on Austin's
        # phone — can reach POST /trigger-stop for the cross-page PANIC STOP.
        # Matches clawd-backchannel's existing 0.0.0.0 posture (same LAN, same
        # gateway token already exposed there). The Mac's OBS browser still uses
        # 127.0.0.1:7900 unchanged.
        ThreadedHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
