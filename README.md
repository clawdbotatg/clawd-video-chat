# 🎥 clawd-video-chat

An always-listening, OBS-feed-shaped UI for putting clawd on a Zoom call.

The page renders the clawd avatar fullscreen on a chroma-green background.
A continuous `webkitSpeechRecognition` loop scans for the wake phrase
**"hey claude"** or **"okay claude"**. When it hears one, it captures the
trailing utterance and sends it to the OpenClaw gateway over the same
WebSocket protocol [`clawd-web-chat`](https://github.com/clawdbotatg/clawd-web-chat)
uses. The reply is streamed back as ElevenLabs audio while the avatar
swaps between idle / listening / chatting / building clips.

To put clawd on an actual Zoom call:

1. Run this server, open the page in Chrome, grant mic permission.
2. In OBS: add a **Browser Source** pointing at `http://127.0.0.1:7900`.
3. Use OBS's **Start Virtual Camera**.
4. In Zoom (or Meet / FaceTime / etc.): select the OBS virtual camera as
   your video device. Use a chroma-key filter on the Browser Source if
   you want clawd to float on your real-world background.

## Setup

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

## The brain

This UI talks to whatever session the gateway is configured to load.
Wire [`clawd-md`](https://github.com/clawdbotatg/clawd-md) into that
session as your system prompt / persona — same as you would for any
other clawd front-end. (See clawd-md's README for how its files are
loaded into the agent.)

## Wake word

The recognizer matches a permissive regex covering common mishearings:

```
/\b(hey|okay|ok)[\s,]+(claude|clawd|claud|cloud|clyde|claud[oe])\b/i
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
