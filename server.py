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
import json
import os
import queue
import sys
import threading
import urllib.error
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
            os.environ.setdefault(key.strip(), val.strip())


load_dotenv()


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


# ── HTTP server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/config":
            cfg = resolve_gateway_settings()
            cfg.pop("bankrKey", None)  # keep API key server-side only
            self.send_json(cfg)
        elif path == "/health":
            self.send_json({"status": "ok"})
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
        path = self.path.split("?", 1)[0]
        if path == "/api/autotitle":
            self.handle_autotitle()
        elif path == "/api/filler":
            self.handle_filler()
        elif path == "/api/tts":
            self.handle_tts()
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
        else:
            self.send_error(404)

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
        bankr_key = os.environ.get("BANKR_LLM_KEY", "")
        if not bankr_key:
            self.send_json({"error": "no bankr key"}, status=503)
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
                system = (
                    "You produce generic 'I'm thinking' filler — a placeholder "
                    "noise played out loud while a smarter model composes "
                    "the real reply. One short phrase, under 8 words. "
                    "\n\n"
                    "Hard rules — every one of these matters:\n"
                    "  - Do NOT respond to the user's message.\n"
                    "  - Do NOT reference the topic, subject, or any words "
                    "from their message.\n"
                    "  - Do NOT use sentiment words like 'interesting', "
                    "'great', 'nice', 'cool', 'enjoy', 'fun', 'good'.\n"
                    "  - Do NOT echo greetings, thanks, or pleasantries.\n"
                    "  - Do NOT promise what the answer will be.\n"
                    "  - Do NOT say 'I think' or anything followed by a "
                    "claim.\n"
                    "\n"
                    "Pick something topic-neutral that just signals "
                    "'processing'. Vary phrasing across calls. "
                    "Good examples (these are the WHOLE response):\n"
                    "  'Hmm, lemme think.'\n"
                    "  'Okay, gimme a sec.'\n"
                    "  'Mmm, hold on.'\n"
                    "  'Right, one moment.'\n"
                    "  'Hmm, alright.'\n"
                    "  'Lemme work on that.'\n"
                    "  'Mmm, processing.'\n"
                    "  'Hmm, okay.'\n"
                    "\n"
                    "First-person, casual, no quotes, no emojis. The "
                    "user's message is provided ONLY so you don't say "
                    "something jarring — never quote from it."
                )
                user_msg = (
                    f"User's message (DO NOT respond to it, DO NOT echo it):\n"
                    f"{last_user}\n\n"
                    "Output ONE short generic 'thinking' phrase, under 8 "
                    "words. No topic reference. No sentiment. Just stall."
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
            else:  # thinking / reasoning
                system = (
                    "You paraphrase, casually and out loud, the inner thought "
                    "of an AI assistant. One short sentence, under 16 words, "
                    "first person, no quotes."
                )
                user_msg = (
                    f"Inner thought:\n{thinking_text}\n\n"
                    "Paraphrase the gist out loud in one short sentence."
                )

            msgs = [{"role": "system", "content": system}]
            if history:
                ctx = "\n".join(
                    f"{m.get('role')}: {(m.get('content') or '')[:300]}"
                    for m in history[-4:] if m.get("role") and m.get("content")
                )
                if ctx:
                    msgs.append({"role": "user", "content": f"Recent context (for reference, do not respond to it):\n{ctx}"})
                    msgs.append({"role": "assistant", "content": "Got it."})
            msgs.append({"role": "user", "content": user_msg})

            req_body = json.dumps({
                "model": "claude-haiku-4.5",
                "max_tokens": 80,
                "messages": msgs,
            }).encode()
            req = urllib.request.Request(
                "https://llm.bankr.bot/v1/chat/completions",
                data=req_body,
                headers={"Content-Type": "application/json", "X-API-Key": bankr_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
            import re
            text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            text = text.strip("\"'`").strip()
            text = text[:240]
            self.send_json({"text": text})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            self.send_json({"error": f"upstream {e.code}: {detail}"}, status=502)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

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
    if not settings["token"]:
        print("[warn] no gateway token found — the UI will prompt you to paste one")
    print("   tip: ?dev=1 in the URL drops OBS mode and shows the full chat UI.")
    check_allowed_origins(PORT)
    try:
        ThreadedHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
