# Inputs & channels — how clawd hears you and where his words go

> Start-here map for the **two ways clawd gets input** and the **public/private
> channel routing**. If you're picking this project back up, read this first —
> it's the mental model the code assumes but never states in one place.

## The big picture: ONE brain, TWO input surfaces, TWO output channels

The "video chat agent" is **not** the persona in this repo. This repo is clawd's
**ears and mouth**. The brain is **`claude -p`** via **`cc-bridge.py`**, importing
[`claude-p-agent`](https://github.com/clawdbotatg/claude-p-agent):

- **Engine** — `CLAUDE_P_AGENT_HOME` → `agent.run_turn()`
- **Persona + tools** — `CC_BRIDGE_CWD` (a brain clone: `CLAUDE.md`, `tools/`)
- **Channel policy** — **this repo's** `prompts/*.md`, passed as `append_system_prompt`
  per turn (voice = public/guarded, voice+ = trusted lock-open, backchannel = private)

Public/private is **not** in claude-p-agent. The bridge maps input → prompt file.

Everything below — both input surfaces and both output channels — shares **one**
`claude -p` session (resumed via `--resume` inside cc-bridge).

```
              ┌────────────── INPUT ──────────────┐
  "okay clawd…"  (voice, PUBLIC)                  (PRIVATE)  backchannel :7851
        │                                                        │
   index.html SR loop                                   prepends "[PRIVATE]"
   → onSend() → WS :7861                                → WS :7861
        │                                                        │
        └──────────────►  cc-bridge → run_turn()  ◄────────────┘
                                   │
                          append_system_prompt from prompts/
                                   │
              ┌──────────────── OUTPUT ───────────────┐
        voice path: [SAY]…[/SAY] → TTS          backchannel: text only
        (public — room hears)                   (unless brain emits [SAY])
              │                                         │
   voice page speaks via TTS                  voice page STRIPS it from TTS
   → the ROOM hears it (PUBLIC)               → silent. only YOU see it (PRIVATE)
```

The key insight: **routing is not transport-level.** It's the LLM choosing to
wrap its reply in the private tags or not, and the voice page **refusing to
speak** anything wrapped. The backchannel input is "private" only because it
biases clawd (via prefix + hint) to wrap the reply.

## Input surface 1 — voice, "okay clawd" (PUBLIC by default)

- Page: this repo's `index.html`, served on **:7900** (Chrome window OBS captures).
- `webkitSpeechRecognition` runs continuously (`startWakeRecog()` ~L3623),
  auto-restarting in `onend`.
- Wake match in `onresult` (~L3667) joins the last **~6s** of SR text
  (`WAKE_WINDOW_MS`), so the command may come *before* the wake word
  ("…what's the weather, **so claude**").
- Wake regex is permissive (mishearings: claude/clawd/cloud/clyde/clod…).
- Echo immunity (`inEchoWindow()` ~L3618): SR ignored for `ECHO_TAIL_MS`
  (800ms) after TTS ends, so clawd's own voice can't trigger him.
- Captured utterance → **`onSend()` (~L2841)** → WS to gateway. Arrives to clawd
  as a plain user message → he replies **unwrapped** → spoken → **room hears it.**

## Input surface 2 — the backchannel (PRIVATE, your ear only)

- Page: **`backchannel/index.html`** (folded into this repo from the former
  standalone `clawd-backchannel` repo; runs under launchd `com.clawd.backchannel`).
- URL: `http://<lan-ip>:7850/?k=<BACKCHANNEL_TOKEN>`
  - LAN IP is **DHCP** — re-check with `ipconfig getifaddr en0` (was .56, .75…).
  - token in `backchannel/.env` (`BACKCHANNEL_TOKEN`).
- It prepends **`[PRIVATE] `** to your text and appends a hint telling clawd to
  wrap his ENTIRE reply (`PRIVATE_PREFIX` / `PRIVATE_REPLY_HINT` ~L211–220).
- clawd, per `IDENTITY.md`, replies **wrapped in the private tags** → the voice
  page strips it from TTS → **silent on the call, visible only to you.**
- Override: tell clawd "say it out loud" / "tell the room" → he sends
  **unwrapped** → the room hears exactly that.

### Why a relay process exists (:7851)

The bridge/gateway binds **loopback only** and needs an Ed25519 nonce handshake
the browser can't do. `backchannel/server.py` is a pure **relay/proxy**:
it does the handshake server-side and forwards frames. It does **not**
tag anything `[PRIVATE]` — that's the page's job. The voice page (:7900) also
reaches the bridge *through* this proxy (its `OPENCLAW_WS_URL` points at :7851).
Without it → "gateway disconnected".

| port | what | where |
|---|---|---|
| 7900 | voice page (this repo) + TTS proxy | `clawd-video-chat/server.py` |
| 7850 | backchannel **page** (prepends `[PRIVATE]`) | `backchannel/` (this repo) |
| 7851 | backchannel **relay** to the loopback bridge | `backchannel/server.py` |
| 18789 | openclaw gateway (loopback) | launchd `ai.openclaw.gateway` |

## Output channels — wrap or don't (enforced in the voice page)

The voice page's TTS sanitizer is what makes "private" actually private:

- `_sanitizeForTts(text)` (~L3413) deletes `[PRIVATE]…[/PRIVATE]` blocks and,
  mid-stream, truncates at a half-arrived `[PRIV` tail so a partial tag never
  leaks a syllable before the close arrives.
- If the sanitized text is empty, the avatar goes **idle** (no fake moving
  mouth) — see ~L2262.
- `/stop` or a run abort in the backchannel → `ttsReset()` hard-stops audio
  immediately (~L2285).

### The leak trap (baked into IDENTITY.md, don't undo it)

clawd must **never type the literal private-tag tokens except as the real
wrapper.** If he types them while *explaining* the system, the page's parser
treats them as live routing tokens, closes the private block early, and dumps
the rest of the message onto the call out loud. When talking *about* the tags,
describe them in words ("the private wrapper"), never the raw tokens.

## Where to look next time (file → line landmarks)

| What | File:line |
|---|---|
| Wake-word SR loop | `index.html` ~L3623 `startWakeRecog()` |
| Wake match + 6s window | `index.html` ~L3667 `onresult`, L3549 `WAKE_WINDOW_MS` |
| Echo immunity | `index.html` ~L3618 `inEchoWindow()`, L3551 `ECHO_TAIL_MS` |
| Single submit path | `index.html` ~L2841 `onSend()` |
| WS to gateway | `index.html` ~L2406 `new WebSocket` |
| TTS private-strip | `index.html` ~L3413 `_sanitizeForTts()` |
| Backchannel prefix + hint | `backchannel/index.html` ~L211–220 |
| Gateway relay handshake | `backchannel/server.py` `_gateway_handshake()` |
| Brain / persona / channel rules | `~/clawd/clawd-md/backchannel.md`, `workspace-clawd/IDENTITY.md` |
| Agent model + tool denies | `~/.openclaw/openclaw.json` (agent id `clawd`) |
