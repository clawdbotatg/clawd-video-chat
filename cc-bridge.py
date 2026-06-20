#!/usr/bin/env python3
"""cc-bridge.py — a drop-in replacement for the openclaw gateway, backed by `claude -p`.

The clawd-video-chat page (index.html) is a generic gateway WS client: it sends
RPC `{type:"req",id,method,params}` and consumes streaming `chat` events. This
bridge speaks that exact wire protocol but runs **Claude Code** (`claude -p`) as
the brain instead of the openclaw `clawd` agent. Point the page's WebSocket URL
at this bridge and the call is driven by Claude Code — no openclaw, no gateway,
no backchannel proxy.

  page  ──WS RPC──►  cc-bridge.py  ──spawn──►  claude -p (stream-json)
        ◄─chat evt─                ◄─stdout──

Why this shape (decided with Austin): keep the page as clawd's mouth/ears (voice
"okay clawd" + backchannel in, TTS out with the [SAY] safety inversion already
in index.html). Replace ONLY the thing behind its socket.

PROTOCOL (reverse-engineered from index.html):
  on connect            → emit  {type:"event", event:"proxy.ready"}
  req sessions.patch    → res ok {}                         (no-op ack)
  req chat.history      → res ok {messages:[{role,content:[{type:"text",text}]}]}
  req chat.send {message,sessionKey} → res ok {runId}; then stream:
        event chat {runId,sessionKey,state:"delta", message:{content:[{type:"text",text:<cumulative>}]}}  (repeated)
        event chat {runId,sessionKey,state:"final", message:{content:[{type:"text",text:<full>}]}}
  req chat.abort {sessionKey,runId}  → kill the run, res ok {}
  req sessions.reset {key}           → forget claude session id for key (stateless call turn), res ok {}

CONTINUITY: each sessionKey maps to a claude --resume <session_id>. sessions.reset
drops the mapping so the next turn starts fresh (matches the page's stateless
voice-turn behavior). Typed/backchannel turns keep continuity.

SAFETY: the page already speaks ONLY text wrapped in [SAY]...[/SAY]. We also tell
claude that convention via the system prompt, so public speech is opt-in at BOTH
layers — a misbehaving brain stays silent rather than broadcasting.

Run:  CC_BRIDGE_MODEL=sonnet python3 cc-bridge.py   (then set the page's WS URL to ws://127.0.0.1:7861)
"""
import asyncio
import json
import os
import uuid

import websockets

PORT = int(os.environ.get("CC_BRIDGE_PORT", "7861"))
HOST = os.environ.get("CC_BRIDGE_HOST", "127.0.0.1")
MODEL = os.environ.get("CC_BRIDGE_MODEL", "")  # empty → claude's configured default
CWD = os.environ.get("CC_BRIDGE_CWD", os.path.dirname(os.path.abspath(__file__)))

# The channel convention, mirrored into the brain. The page enforces it too.
CHANNEL_RULES = (
    "You are clawd, the voice on a live call (slop.computer). You speak to two "
    "audiences. DEFAULT TO SILENCE on the call: anything you write is PRIVATE to "
    "Austin (he reads it; the room does NOT hear it) UNLESS you wrap it in "
    "[SAY]...[/SAY]. Only text inside [SAY] tags is spoken aloud to the room. "
    "So: speak aloud ONLY when asked to (\"say it out loud\", \"tell the room\", "
    "\"introduce yourself on the call\") — then put exactly those words inside "
    "[SAY]...[/SAY]. Status updates, acknowledgements, and answers to private "
    "questions stay unwrapped (silent). Never wrap something in [SAY] unless you "
    "mean for everyone on the call to hear it. Keep spoken lines short and warm."
)

# Scrub these so the child claude runs on the subscription (OAuth), not metered
# API, and isn't confused into embedded mode (same rule as the harness SCRUB_ENV).
SCRUB_PREFIXES = ("CLAUDE_CODE", "ANTHROPIC_API")
SCRUB_EXACT = {"CLAUDECODE", "ANTHROPIC_API_KEY"}


def child_env():
    env = {k: v for k, v in os.environ.items()
           if k not in SCRUB_EXACT and not k.startswith(SCRUB_PREFIXES)}
    return env


# Per-sessionKey state: claude session id (for --resume) and a transcript for chat.history.
sessions = {}   # sessionKey -> {"sid": str|None, "history": [ {role, content:[...]} ]}
# Running claude subprocesses by runId, so chat.abort can kill them.
runs = {}       # runId -> asyncio.subprocess.Process


def sess(key):
    return sessions.setdefault(key, {"sid": None, "history": []})


async def send_frame(ws, obj):
    await ws.send(json.dumps(obj))


async def emit_chat(ws, run_id, session_key, state, text):
    await send_frame(ws, {
        "type": "event", "event": "chat",
        "payload": {
            "runId": run_id, "sessionKey": session_key, "state": state,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    })


async def run_claude(ws, session_key, run_id, prompt):
    """Spawn claude -p, stream its output back as cumulative chat deltas, end with final."""
    s = sess(session_key)
    cmd = ["claude", "-p", "--output-format", "stream-json",
           "--include-partial-messages", "--verbose",
           "--append-system-prompt", CHANNEL_RULES]
    if MODEL:
        cmd += ["--model", MODEL]
    if s["sid"]:
        cmd += ["--resume", s["sid"]]

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=CWD, env=child_env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    runs[run_id] = proc
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    text = ""
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "system" and evt.get("subtype") == "init":
                if evt.get("session_id"):
                    s["sid"] = evt["session_id"]
            elif etype == "stream_event":
                # token-level deltas: content_block_delta -> text_delta
                inner = evt.get("event", {})
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text += delta["text"]
                        await emit_chat(ws, run_id, session_key, "delta", text)
            elif etype == "assistant":
                # Full assistant message; reconcile in case partials were missed.
                blocks = (evt.get("message") or {}).get("content") or []
                full = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if len(full) > len(text):
                    text = full
                    await emit_chat(ws, run_id, session_key, "delta", text)
            elif etype == "result":
                if evt.get("session_id"):
                    s["sid"] = evt["session_id"]
                if isinstance(evt.get("result"), str) and len(evt["result"]) > len(text):
                    text = evt["result"]
        await proc.wait()
    finally:
        runs.pop(run_id, None)

    s["history"].append({"role": "user", "content": [{"type": "text", "text": prompt}]})
    s["history"].append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    await emit_chat(ws, run_id, session_key, "final", text)


async def handle_req(ws, frame):
    method = frame.get("method")
    params = frame.get("params") or {}
    rid = frame.get("id")

    async def ok(payload):
        await send_frame(ws, {"type": "res", "id": rid, "ok": True, "payload": payload})

    if method == "sessions.patch":
        await ok({})
    elif method == "chat.history":
        s = sess(params.get("sessionKey", ""))
        limit = params.get("limit") or 100
        await ok({"messages": s["history"][-limit:]})
    elif method == "sessions.reset":
        sess(params.get("key", ""))["sid"] = None   # forget continuity; keep transcript
        await ok({})
    elif method == "chat.abort":
        proc = runs.get(params.get("runId"))
        if proc and proc.returncode is None:
            try: proc.kill()
            except ProcessLookupError: pass
        await ok({})
    elif method == "chat.send":
        session_key = params.get("sessionKey", "")
        message = params.get("message", "")
        run_id = "run-" + uuid.uuid4().hex[:12]
        await ok({"runId": run_id})
        if message.strip().startswith("/"):
            # Slash control (e.g. /model) — not a prompt for the brain. Silent ack.
            await emit_chat(ws, run_id, session_key, "final", "")
        else:
            asyncio.create_task(run_claude(ws, session_key, run_id, message))
    else:
        # Unknown method: ack empty so the page's RPC promise resolves.
        await ok({})


async def handler(ws):
    # The page waits for proxy.ready before sending any RPC (it expects the
    # backchannel proxy to have finished the gateway handshake server-side).
    await send_frame(ws, {"type": "event", "event": "proxy.ready"})
    async for raw in ws:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if frame.get("type") == "req":
            asyncio.create_task(handle_req(ws, frame))


async def main():
    print(f"cc-bridge: claude -p brain on ws://{HOST}:{PORT}"
          f"  (model={MODEL or 'default'}, cwd={CWD})")
    async with websockets.serve(handler, HOST, PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
