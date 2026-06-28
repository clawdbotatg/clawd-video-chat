#!/usr/bin/env python3
"""cc-bridge.py — a drop-in replacement for the openclaw gateway, backed by `claude -p`.

The clawd-video-chat page (index.html) is a generic gateway WS client: it sends
RPC `{type:"req",id,method,params}` and consumes streaming `chat` events. This
bridge speaks that exact wire protocol but runs **Claude Code** (`claude -p`) as
the brain instead of the openclaw `clawd` agent.

  voice page (:7900) ─┐
                      ├─WS──►  cc-bridge.py  ──spawn──►  claude -p (stream-json)
  backchannel (:7851)─┘            │ broadcasts chat events to ALL clients

KEY behaviors (learned the hard way):
- BROADCAST: chat events fan out to ALL connected sockets, like the openclaw
  gateway did. Without this, a backchannel-initiated [SAY] never reaches the
  VOICE page's mouth (the voice page is what does TTS), so nothing is spoken.
- VOICE vs PRIVATE: a backchannel message arrives prefixed "[PRIVATE]"; a voice
  ("okay clawd…") turn arrives as plain text. The prefix is a ROUTING signal only
  — we strip it before the brain sees it (it used to mirror "[PRIVATE]" back,
  speaking it aloud / cluttering the backchannel). Voice turns are PUBLIC — the
  whole reply must be SPOKEN, so we wrap it in [SAY]…[/SAY] for the page's TTS
  gate. Backchannel turns are PRIVATE — silent unless the brain itself emits [SAY]
  (which it does when Austin says "say it out loud").
- The brain runs with cwd = CC_BRIDGE_CWD (a claude-p-agent clone). Channel prompts
  live in this repo under prompts/; the engine is imported from CLAUDE_P_AGENT_HOME.
- MEMORY: claude-p-agent's ONE system — a conversation has a *key*, and the engine
  remembers it. We call run_turn(remember=session_key); the engine loads/resumes
  that key's claude session and saves the new id back. We keep NO session id of our
  own. So a turn sends only the new message + persona, NEVER prior turns (the
  resumed session already holds them). Memory survives a bridge restart; /new (or
  sessions.reset) calls forget(key) to reset the conversation.

PROTOCOL: on connect → connect.challenge + proxy.ready (satisfies both the direct
voice page and the backchannel proxy's gateway handshake). RPC: sessions.patch
(ack), chat.history, chat.send → {runId} then streaming chat deltas/final,
chat.abort, sessions.reset.

Run:  CC_BRIDGE_MODEL=opus python3 cc-bridge.py
"""
import asyncio
import json
import os
import sys
import time
import uuid

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(HERE, "prompts")

AGENT_HOME = os.path.abspath(
    os.environ.get("CLAUDE_P_AGENT_HOME", os.path.expanduser("~/clawd/clawd-harness/projects/claude-p-agent"))
)
if AGENT_HOME not in sys.path:
    sys.path.insert(0, AGENT_HOME)
from agent import current_session, forget, read_prompt, run_turn  # noqa: E402

PORT = int(os.environ.get("CC_BRIDGE_PORT", "7861"))
HOST = os.environ.get("CC_BRIDGE_HOST", "127.0.0.1")
MODEL = os.environ.get("CC_BRIDGE_MODEL", "")  # empty → claude's configured default
CWD = os.environ.get("CC_BRIDGE_CWD", AGENT_HOME)

# Channel prompts live in this repo (prompts/*.md), not in claude-p-agent.
_CHANNEL_PROMPTS = {
    "voice": "voice.md",
    "voice+": "voice-trusted.md",
    "priv": "backchannel.md",
}


def _channel_prompt(chan):
    return read_prompt(os.path.join(PROMPTS_DIR, _CHANNEL_PROMPTS[chan]))


def _claude_extra_args():
    args = ["--include-partial-messages"]
    if MODEL:
        args += ["--model", MODEL]
    return args


def _add_dirs():
    return [
        os.path.expanduser("~/clawd/clawd-md"),
        os.path.expanduser("~/clawd/clawd-chronicle"),
    ]


PRIVATE_PREFIX = "[PRIVATE]"

# Full observability: every input (voice/backchannel) and every reply is logged
# here so the conversation can be watched/debugged with `tail -f`.
CONVO_LOG = os.environ.get("CC_BRIDGE_CONVO_LOG", "/tmp/cc-bridge-convo.log")


def convo_log(kind, channel, session_key, text):
    try:
        with open(CONVO_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%H:%M:%S')}] {kind} ({channel}) {session_key}\n"
                    f"  {text.strip()}\n")
    except Exception:
        pass


sessions = {}          # sessionKey -> {"history": [...]}  (display buffer for chat.history;
                       #                NOT fed to the brain — the resumed session holds history)
runs = {}              # runId -> asyncio.subprocess.Process
clients = set()        # all connected browser/proxy sockets


def sess(key):
    return sessions.setdefault(key, {"history": []})


# Memory is claude-p-agent's ONE system: a conversation has a *key*, and the engine
# remembers it. We pass `remember=session_key` to run_turn — the engine loads that
# key's stored claude session id, --resume's it, captures the new id, and saves it
# back. We keep NO session bookkeeping of our own. `forget(key)` resets a
# conversation; `current_session(key)` reads a key's live id without a turn (we use
# it to publish the just-abandoned id on reset, and we mirror the per-turn id into
# the :7900 gauge sentinel). (Our sessionKeys — e.g. "agent:clawd:main" — are plain
# names, so the engine stores them in its own .memory/ dir.) See README "Memory".


def _write_brain_session(session_key, sid):
    """Record the LIVE brain session id so the :7900 context gauge reads OUR
    session, not whatever transcript is newest in the (shared) cwd project dir —
    operator/dev `claude` sessions run in the same cwd and would otherwise
    pollute the gauge. One sentinel file per sessionKey."""
    try:
        d = os.path.expanduser("~/.cache/clawd")
        os.makedirs(d, exist_ok=True)
        slug = "".join(c if c.isalnum() else "-" for c in session_key)
        with open(os.path.join(d, f"brain-session-{slug}.json"), "w") as f:
            json.dump({"sessionKey": session_key, "sessionId": sid,
                       "cwd": CWD, "ts": int(time.time() * 1000)}, f)
    except Exception:
        pass


async def send_frame(ws, obj):
    await ws.send(json.dumps(obj))


async def broadcast_chat(run_id, session_key, state, text):
    """Fan a chat event out to EVERY connected client (voice page + backchannel)."""
    data = json.dumps({
        "type": "event", "event": "chat",
        "payload": {
            "runId": run_id, "sessionKey": session_key, "state": state,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    })
    for c in list(clients):
        try:
            await c.send(data)
        except Exception:
            clients.discard(c)


async def broadcast_event(event, payload):
    """Fan an arbitrary gateway event out to EVERY connected client. Used for
    out-of-band state the page can't infer from chat deltas — e.g. a session
    reset, which changes nothing on disk yet (the fresh transcript only appears
    on the NEXT turn) so the :7900 context gauge would otherwise keep reading
    the just-abandoned session."""
    data = json.dumps({"type": "event", "event": event, "payload": payload})
    for c in list(clients):
        try:
            await c.send(data)
        except Exception:
            clients.discard(c)


def say_wrap(text, final):
    """Voice turns: wrap the reply so the page's [SAY]-only TTS speaks it.
    Streaming-aware — an open [SAY] (no close yet) makes the page speak arrived
    text incrementally; the close lands on the final frame."""
    t = text.replace("[SAY]", "").replace("[/SAY]", "")
    return f"[SAY]{t}[/SAY]" if final else f"[SAY]{t}"


async def run_claude(session_key, run_id, prompt, public, trusted=False):
    """Spawn claude -p via claude-p-agent.run_turn; stream to all clients."""
    s = sess(session_key)
    chan = ("voice+" if trusted else "voice") if public else "priv"
    sys_prompt = _channel_prompt(chan)
    loop = asyncio.get_running_loop()
    proc_holder = {}
    runs[run_id] = proc_holder
    text = ""

    async def emit(state):
        await broadcast_chat(run_id, session_key, state,
                             say_wrap(text, state == "final") if public else text)

    def on_event(evt):
        nonlocal text
        etype = evt.get("type")
        if etype == "stream_event":
            inner = evt.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text += delta["text"]
                    asyncio.run_coroutine_threadsafe(emit("delta"), loop)
        elif etype == "assistant":
            blocks = (evt.get("message") or {}).get("content") or []
            full = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if len(full) > len(text):
                text = full
                asyncio.run_coroutine_threadsafe(emit("delta"), loop)
        elif etype == "result":
            if isinstance(evt.get("result"), str) and len(evt["result"]) > len(text):
                text = evt["result"]
                asyncio.run_coroutine_threadsafe(emit("delta"), loop)

    err = ""
    try:
        result = await asyncio.to_thread(
            run_turn,
            prompt,
            append_system_prompt=sys_prompt,
            remember=session_key,   # engine resumes this conversation's session + saves the new id
            cwd=CWD,
            add_dirs=_add_dirs(),
            extra_args=_claude_extra_args(),
            on_event=on_event,
            input_via="stdin",
            return_meta=True,
            proc_holder=proc_holder,
        )
        text = result["text"] or text
        if result.get("session_id"):
            # The engine already persisted the id; we only mirror it into the gauge
            # sentinel so the :7900 context chip reads OUR live session.
            _write_brain_session(session_key, result["session_id"])
    except Exception as e:
        err = str(e)
    finally:
        runs.pop(run_id, None)

    if err or not text.strip():
        convo_log("ERR", chan, session_key,
                  f"turn failed: reply_empty={not text.strip()}; err: {err[-600:] or '(none)'}")

    s["history"].append({"role": "user", "content": [{"type": "text", "text": prompt}]})
    s["history"].append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    convo_log("OUT", chan, session_key, text or "(empty)")
    await emit("final")


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
        key = params.get("key", "")
        s = sess(key)
        prev_sid = current_session(key)   # read the abandoned id from the engine (no bookkeeping)
        forget(key)                       # engine drops this conversation → fresh claude -p next turn
        s["history"] = []
        await ok({})
        # Tell every client the context was cleared, naming the now-abandoned session
        # so the :7900 gauge can drain and ignore that stale session on its poll.
        await broadcast_event("context.cleared",
                              {"sessionKey": key, "prevSessionId": prev_sid})
    elif method == "chat.abort":
        holder = runs.get(params.get("runId"))
        proc = holder.get("proc") if isinstance(holder, dict) else None
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await ok({})
    elif method == "chat.send":
        session_key = params.get("sessionKey", "")
        message = params.get("message", "")
        run_id = "run-" + uuid.uuid4().hex[:12]
        await ok({"runId": run_id})
        # Detect "/new" / "/clear" — even when sent from the backchannel, which
        # prepends "[PRIVATE]". These DROP the resumed session so the next turn
        # starts a genuinely fresh claude -p (no --resume). Other slash messages
        # (e.g. the page's "/model …" switch) stay silent no-ops.
        cmd = message.lstrip()
        if cmd.startswith(PRIVATE_PREFIX):
            cmd = cmd[len(PRIVATE_PREFIX):].lstrip()
        cmd_word = cmd.split(maxsplit=1)[0].lower() if cmd.split() else ""
        if cmd.lower() in ("/new", "/clear"):
            s = sess(session_key)
            prev_sid = current_session(session_key)   # abandoned id, read from the engine
            forget(session_key)        # engine drops this conversation → next turn starts fresh
            s["history"] = []
            convo_log("CMD", "—", session_key, f"{cmd.lower()} → session reset (fresh)")
            await broadcast_chat(run_id, session_key, "final", "")
            await broadcast_event("context.cleared",
                                  {"sessionKey": session_key, "prevSessionId": prev_sid})
        elif cmd_word == "/compact":
            # REAL compaction. claude -p only runs a slash command when it's the
            # WHOLE prompt — so a backchannel "/compact" wrapped as
            # "[PRIVATE] /compact … [hint]" is read as chat text and the brain
            # merely *narrates* compacting while context keeps climbing. Forward
            # the bare "/compact" against the resumed session so it actually
            # shrinks; run silent (public=False) — the confirmation isn't spoken,
            # and run_claude captures the post-compact session_id from --resume.
            convo_log("CMD", "—", session_key, "/compact → real compaction")
            asyncio.create_task(run_claude(session_key, run_id, "/compact", public=False))
        elif message.strip().startswith("/"):
            await broadcast_chat(run_id, session_key, "final", "")  # slash control: silent
        else:
            # Voice turns arrive plain (PUBLIC → speak). Backchannel turns are
            # prefixed "[PRIVATE]" (PRIVATE → silent unless the brain emits [SAY]).
            public = not message.lstrip().startswith(PRIVATE_PREFIX)
            # The "[PRIVATE]" prefix is a ROUTING signal ONLY — strip it so the
            # brain never SEES the literal token. It used to mirror it back,
            # speaking "[PRIVATE] …" aloud on voice turns and cluttering the
            # backchannel; the turn's system prompt (PRIVATE_SYS vs VOICE_SYS) is
            # what tells the brain which channel it's on, not a tag in the text.
            prompt = message
            if not public:
                p = message.lstrip()
                prompt = p[len(PRIVATE_PREFIX):].lstrip()
            # FULL-ACCESS voice: the control page sets trusted=true when Austin has
            # the on-screen lock OPEN. Only meaningful on the public/voice path —
            # a backchannel turn is already full-trust. (params.get tolerates the
            # flag being absent, e.g. the openclaw page or an older client.)
            trusted = public and bool(params.get("trusted"))
            convo_log("IN ", ("voice+" if trusted else "voice") if public else "priv",
                      session_key, message)
            asyncio.create_task(run_claude(session_key, run_id, prompt, public, trusted))
    else:
        await ok({})


async def handler(ws):
    # Emit connect.challenge FIRST (the backchannel proxy's gateway handshake reads
    # it), then proxy.ready (the direct voice page acts on it; the proxy swallows
    # it during its handshake loops). See PROTOCOL note up top.
    await send_frame(ws, {"type": "event", "event": "connect.challenge",
                          "payload": {"nonce": uuid.uuid4().hex}})
    await send_frame(ws, {"type": "event", "event": "proxy.ready"})
    clients.add(ws)
    try:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "req":
                asyncio.create_task(handle_req(ws, frame))
    finally:
        clients.discard(ws)


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
