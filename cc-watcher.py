#!/usr/bin/env python3
"""cc-watcher.py — the heartbeat that lets clawd MANAGE coding workers across turns.

The call-brain (cc-bridge `claude -p`) is purely reactive: it only runs when a
voice/backchannel message arrives. So a worker session it started via the `code`
helper can BLOCK on a question or FINISH after the brain's turn ended — with nobody
listening. This daemon is the missing loop:

  harness :8787  ──(watch sessions/hooks)──►  cc-watcher  ──(wake on event)──►  bridge :7861
                                                  │
        a worker clawd started goes BLOCKED  ─────┤  inject "[PRIVATE] worker X needs you…"
        or finishes a turn (idle)            ─────┘  inject "[PRIVATE] worker X is idle — review/ship"

So clawd gets poked EXACTLY when there's something to do, in a normal backchannel
turn, and does one step (answer / ship / follow up) — no babysitting, works across
turns. Both hops are loopback, no auth.

Which sessions count as "clawd's workers": only sessions in an ELIGIBLE project — a
harness project whose dir is a symlink into ~/clawd and not infra (same rule as the
`code` helper). The operator's own harness sessions are ignored.

Safety: injections are IDLE-GATED — the watcher tracks clawd's reply state off the
bridge's chat broadcasts and waits for him to be idle before waking him, so it never
fires a second turn on top of one already running. Edge-triggered + debounced: one
ping per blocked episode, one per turn-completion.

Run:  python3 cc-watcher.py            (CC_WATCHER_DRYRUN=1 → log, don't inject)
Env:  CC_WATCHER_SESSION_KEY (default agent:clawd:main)
"""
import base64
import json
import os
import socket
import struct
import threading
import time
import uuid

HARNESS_URL = ("127.0.0.1", 8787, "/ws")
BRIDGE_URL = ("127.0.0.1", 7861, "/")
HARNESS_TOKEN_FILE = os.path.expanduser("~/clawd/clawd-harness/.clawd-harness.token")
CLAWD_DIR = os.path.realpath(os.path.expanduser("~/clawd"))
SESSION_KEY = os.environ.get("CC_WATCHER_SESSION_KEY", "agent:clawd:main")
DRYRUN = os.environ.get("CC_WATCHER_DRYRUN") == "1"
LOG = os.environ.get("CC_WATCHER_LOG", "/tmp/cc-watcher.log")

INFRA = {
    "clawd-harness", "clawd-video-chat-cc", "clawd-video-chat", "clawd-backchannel",
    "clawd-md", "clawd-chronicle", "clawd-call-brain", ".clawd-call-brain",
    "clawd-containers", "clawd-hermes",
}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── RFC 6455 client (same minimal framing as the `code` helper) ──────────────
def _ws_send(wfile, lock, data, opcode=0x1):
    payload = data.encode("utf-8") if isinstance(data, str) else data
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mk = os.urandom(4)
    header += mk
    payload = bytes(payload[i] ^ mk[i % 4] for i in range(len(payload)))
    with lock:
        wfile.write(bytes(header) + payload)
        wfile.flush()


def _ws_read(rfile):
    payload = b""
    msg_opcode = None
    while True:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return None
        b0, b1 = hdr[0], hdr[1]
        fin, opcode, masked, length = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if length == 126:
            ext = rfile.read(2)
            if len(ext) < 2:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = rfile.read(8)
            if len(ext) < 8:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(length) if length else b""
        if masked and chunk:
            chunk = bytes(chunk[i] ^ mask[i % 4] for i in range(len(chunk)))
        if opcode == 0x8:
            return ("close", chunk)
        if opcode == 0x9:
            return ("ping", chunk)
        if opcode == 0xA:
            return ("pong", chunk)
        if opcode != 0x0:
            msg_opcode = opcode
        payload += chunk
        if fin:
            return (msg_opcode or 0x1, payload)


def connect(host, port, path):
    sock = socket.create_connection((host, port), timeout=20)
    key = base64.b64encode(os.urandom(16)).decode()
    req = "\r\n".join([
        f"GET {path} HTTP/1.1", f"Host: {host}:{port}",
        "Upgrade: websocket", "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
    ]) + "\r\n\r\n"
    sock.sendall(req.encode())
    sock.settimeout(None)
    rfile = sock.makefile("rb")
    status = rfile.readline()
    if b" 101 " not in status:
        sock.close()
        raise ConnectionError(f"handshake failed: {status!r}")
    while True:
        h = rfile.readline()
        if h in (b"\r\n", b"", b"\n"):
            break
    return sock, rfile, sock.makefile("wb")


def harness_path():
    try:
        tok = open(HARNESS_TOKEN_FILE).read().strip()
        return f"/ws?t={tok}"
    except OSError:
        return "/ws"


# ── the brain link: wake clawd, idle-gated off his reply stream ──────────────
class Brain:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.last_final = time.time()
        self.wfile = None
        self.connected = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                sock, rfile, wfile = connect(*BRIDGE_URL)
                with self.lock:
                    self.wfile = wfile
                self.connected.set()
                log("bridge: connected")
                while True:
                    msg = _ws_read(rfile)
                    if msg is None:
                        break
                    kind, data = msg
                    if kind == "close":
                        break
                    if kind == "ping":
                        _ws_send(wfile, self.lock, data, opcode=0xA)
                        continue
                    if kind != 0x1:
                        continue
                    self._track(data)
            except Exception as e:
                log(f"bridge: reconnect after {e!r}")
            self.connected.clear()
            with self.lock:
                self.wfile = None
            time.sleep(2)

    def _track(self, data):
        """Follow clawd's reply state so we only wake him when he's idle."""
        try:
            f = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            return
        if f.get("event") != "chat":
            return
        p = f.get("payload") or {}
        if p.get("sessionKey") not in (SESSION_KEY, "", None):
            return
        if p.get("state") == "final":
            self.busy = False
            self.last_final = time.time()
        else:
            self.busy = True

    def wake(self, message):
        """Wait for clawd to be idle, then inject a [PRIVATE] backchannel turn."""
        if DRYRUN:
            log(f"DRYRUN would wake clawd: {message}")
            return
        self.connected.wait(timeout=30)
        # idle-gate: hold off while a reply is streaming (avoid a concurrent
        # --resume on the same session). Cap the wait so we never drop a ping.
        deadline = time.time() + 45
        while self.busy and time.time() < deadline:
            time.sleep(0.5)
        # small settle after the last final, so the session file is released
        while time.time() - self.last_final < 1.5:
            time.sleep(0.3)
        frame = {"type": "req", "id": "watch-" + uuid.uuid4().hex[:8],
                 "method": "chat.send",
                 "params": {"sessionKey": SESSION_KEY, "message": message}}
        with self.lock:
            if not self.wfile:
                log("bridge: not connected, dropping wake")
                return
            try:
                _ws_send(self.wfile, self.lock, json.dumps(frame))
            except Exception as e:
                log(f"bridge: wake send failed {e!r}")
                return
        self.busy = True  # our injection starts a turn
        log(f"woke clawd → {message[:90]}")


BLOCKED_MSG = (
    "[PRIVATE] [auto-watch] Your coding worker {c8} in {proj} is BLOCKED and waiting "
    "for you{onmsg}. Read it with `code tail {cid}`, decide it yourself, and answer — "
    "`code say {cid} \"…\"` for a question, or `code key {cid} …` to accept a "
    "menu/permission. Don't stall. (If it reported UNRELATED uncommitted changes in "
    "the repo, do NOT proceed — tell Austin and wait.)"
)
DONE_MSG = (
    "[PRIVATE] [auto-watch] Your coding worker {c8} in {proj} just went idle (a turn "
    "finished). Review it: `code tail {cid}`. If the feature is done, ship it (commit "
    "+ push a feature branch, then run a deploy step only if the repo has one) and "
    "`code close {cid}`. If more is needed, send the next step with `code say`. If "
    "you already handled this, ignore."
)


def main():
    log(f"cc-watcher up (dryrun={DRYRUN}, session={SESSION_KEY})")
    brain = Brain()
    projects = {}          # pid -> {"name","path","eligible"}
    state = {}             # cid -> {"status","proj","seen"}

    def eligible(path, name):
        if name in INFRA or not path:
            return False
        try:
            return (os.path.islink(path)
                    and os.path.realpath(path).startswith(CLAWD_DIR + os.sep))
        except OSError:
            return False

    while True:
        try:
            sock, rfile, wfile = connect(HARNESS_URL[0], HARNESS_URL[1], harness_path())
            lock = threading.Lock()
            _ws_send(wfile, lock, json.dumps({"type": "list"}))
            log("harness: connected")
            seeded = False
            while True:
                msg = _ws_read(rfile)
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    _ws_send(wfile, lock, data, opcode=0xA)
                    continue
                if kind != 0x1:
                    continue
                try:
                    f = json.loads(data.decode("utf-8", "replace"))
                except Exception:
                    continue
                t = f.get("type")
                if t == "projects":
                    projects = {p["pid"]: {"name": p.get("name", ""),
                                           "path": p.get("path", ""),
                                           "eligible": eligible(p.get("path", ""),
                                                                p.get("name", ""))}
                                for p in f.get("projects", [])}
                elif t == "sessions":
                    sessions = f.get("sessions", [])
                    for s in sessions:
                        cid = s.get("cid")
                        proj = projects.get(s.get("pid"), {})
                        if not cid or not proj.get("eligible"):
                            continue
                        status = s.get("status", "idle")
                        prev = state.get(cid)
                        state[cid] = {"status": status, "proj": proj.get("name"),
                                      "seen": True}
                        if not seeded or prev is None:
                            continue  # seed silently — don't wake for pre-existing state
                        if prev["status"] != status:
                            _maybe_wake(brain, cid, status, prev["status"],
                                        proj.get("name"), s.get("blocked_on"))
                    seeded = True
                elif t == "exit":
                    state.pop(f.get("cid"), None)
        except Exception as e:
            log(f"harness: reconnect after {e!r}")
        time.sleep(2)


def _maybe_wake(brain, cid, status, prev, proj, blocked_on):
    c8 = cid[:8]
    if status == "blocked":
        onmsg = (f" — it asks: {blocked_on}" if blocked_on else "")
        brain.wake(BLOCKED_MSG.format(c8=c8, cid=cid, proj=proj, onmsg=onmsg))
    elif status == "idle" and prev == "working":
        brain.wake(DONE_MSG.format(c8=c8, cid=cid, proj=proj))


if __name__ == "__main__":
    main()
