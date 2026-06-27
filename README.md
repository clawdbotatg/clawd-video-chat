# 🎥 clawd-video-chat

An always-listening, OBS-feed-shaped UI for putting clawd on a Zoom call.

The page renders the clawd avatar fullscreen on a chroma-green background.
A continuous `webkitSpeechRecognition` loop scans for the wake phrase
**"okay claude"** / **"ok clod"** (and a few common Web-Speech
mishearings). When it hears one, it captures the
trailing utterance and sends it to the OpenClaw gateway over the same
WebSocket protocol [`clawd-web-chat`](https://github.com/clawdbotatg/clawd-web-chat)
uses. The reply is streamed back as ElevenLabs audio while the avatar
swaps between idle / listening / chatting / building clips.

> **Voice/backchannel adapter for [claude-p-agent](https://github.com/clawdbotatg/claude-p-agent).**
> This repo is the face (mic, TTS, avatar, backchannel). The **brain** is a separate
> clone of `claude-p-agent` with your `CLAUDE.md`. **Public/private/trust policy
> lives here** — in `cc-bridge.py` + `prompts/`, not in the engine.

To put clawd on an actual Zoom call:

1. Run this server, open the page in Chrome, grant mic permission.
2. In OBS: add a **Browser Source** pointing at `http://127.0.0.1:7900`.
3. Use OBS's **Start Virtual Camera**.
4. In Zoom (or Meet / FaceTime / etc.): select the OBS virtual camera as
   your video device. Use a chroma-key filter on the Browser Source if
   you want clawd to float on your real-world background.

## Setup

**Two repos:**

1. **Brain** — clone [claude-p-agent](https://github.com/clawdbotatg/claude-p-agent), copy `CLAUDE.md.example` → `CLAUDE.md`, add your tools.
2. **Face** — this repo (UI + `cc-bridge.py` adapter).

```bash
git clone https://github.com/clawdbotatg/clawd-video-chat
cd clawd-video-chat
cp .env.example .env
# set OPENCLAW_WS_URL=ws://127.0.0.1:7861
# set CLAUDE_P_AGENT_HOME and CC_BRIDGE_CWD to your brain clone

pip install websockets   # cc-bridge only dependency
python3 cc-bridge.py &   # brain WS on :7861
python3 server.py        # UI on :7900 → http://127.0.0.1:7900
```

Channel policy (public voice vs private backchannel vs trusted lock-open voice) is
in **`prompts/`** — see `INPUTS-AND-CHANNELS.md`.

No pip deps for `server.py`. Append `?dev=1` for the debug chat panel.

### Production (Austin's slop rig)

`./slop-bridge.sh` starts server + backchannel proxy + OBS wiring. `cc-bridge` runs
under launchd (`deploy/com.clawd.cc-bridge.plist`) with `CLAUDE_P_AGENT_HOME` and
`CC_BRIDGE_CWD` set to the brain clone.

## Legacy: openclaw gateway

Older setups pointed the UI at openclaw on `:18789`. The brain is **`cc-bridge.py`**
(`claude -p`) now. If you still use openclaw for something else, leave it alone — but
the call UI should use `OPENCLAW_WS_URL=ws://127.0.0.1:7861`.

<details>
<summary>Old openclaw pairing steps (not needed for cc-bridge)</summary>

```bash
git clone https://github.com/clawdbotatg/clawd-video-chat
cd clawd-video-chat
cp .env.example .env
# add ELEVENLABS_API_KEY (strongly recommended — browser TTS won't carry
# cleanly through OBS) and BANKR_LLM_KEY for stall-talk
python3 server.py
# open http://127.0.0.1:7900
```

No pip dependencies. No build step. Gateway token is auto-read from
`~/.openclaw/openclaw.json`. Append `?dev=1` to the URL to drop OBS mode
and show the full chat panel for debugging.

## First-time gateway config

Same one-time setup as `clawd-web-chat` — the gateway needs to allow our
origin and grant admin scope so it streams tool events.

**1. Add this origin to `~/.openclaw/openclaw.json`:**

```json5
"gateway": {
  "controlUi": {
    "allowedOrigins": [
      "http://localhost:7900",
      "http://127.0.0.1:7900"
    ]
  }
}
```

Restart the gateway after this.

**2. Pair the device** (first connect creates a pending request):

```bash
openclaw devices list          # grab the pending request id
openclaw devices approve <id>
```

</details>

## The brain (claude-p-agent)

Persona and tools live in **`CC_BRIDGE_CWD`** — your brain clone's `CLAUDE.md` and
`tools/`. This repo only adds per-channel prompts (`prompts/voice.md`, etc.) via
`append_system_prompt` when spawning `run_turn()`.

| Channel | How it arrives | Prompt |
|---|---|---|
| Voice (guarded) | wake word → plain text | `prompts/voice.md` |
| Voice (lock open) | plain + `trusted=true` | `prompts/voice-trusted.md` |
| Backchannel | `[PRIVATE]` prefix stripped | `prompts/backchannel.md` |

## Wake word

The recognizer matches a permissive regex covering common mishearings:

```
/\b(okay|ok)[\s,]+(claude|clawd|claud|cloud|clyde|clod|claud[oe])\b/i
```

After the wake phrase, anything that follows is captured until 1.5s of
silence, then submitted as a chat message. While clawd is speaking the
loop ignores SR results (echo immunity) for the duration of the audio
playback plus an 800ms tail.

## URL params

- `?dev=1` — drop OBS mode, show the full chat panel.
- `?nostatus=1` — hide the small "● listening" status pill in the corner.

## What's borrowed from clawd-web-chat

Almost everything below the surface — the WebSocket protocol client,
the streaming TTS chunker, the avatar state machine, the tool-call
cards, the autotitle / Haiku-filler stall-talk, the settings overlay.
Only the visible chrome and the voice-input loop differ.

## Caveats / known weak spots

- Web Speech API is Chrome-only. Firefox and Safari won't do continuous
  speech recognition.
- Chrome occasionally kills the recognizer mid-stream; we auto-restart
  in `onend`, so there's a sub-second blind spot.
- The wake phrase regex is permissive on purpose — Web Speech often
  hears "claude" as "cloud" or "clyde". Expect occasional false
  triggers in noisy rooms.
- Echo cancellation is the *system mic*'s job. If clawd hears himself
  through your speakers and triggers his own wake word, use
  headphones — or tighten `ECHO_TAIL_MS` in `index.html`.
