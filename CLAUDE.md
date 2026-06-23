# clawd-video-chat — operator notes for Claude

This is the single-page UI that puts clawd on a Zoom/Meet/slop call. See
`README.md` for the underlying architecture (wake word, WS protocol, TTS).
These notes are for the things you can't infer from reading code.

> **First time back on this project?** Read **[`INPUTS-AND-CHANNELS.md`](INPUTS-AND-CHANNELS.md)**
> — the mental model for how clawd gets input (voice "okay clawd" = PUBLIC vs the
> backchannel = PRIVATE) and how public/private routing actually works. One shared
> openclaw `clawd` session, two input surfaces, two output channels. File:line
> landmarks included.
>
> **What's running on leftclaw (and how to stop it)?** See **[`SERVICES.md`](SERVICES.md)**
> — the always-on launchd daemons this rig leaves running (`cc-bridge`,
> `cc-watcher`, the harness, the backchannel proxy), their ports/logs, health
> checks, and off-switches. Read it before reworking the call system, since these
> won't stop themselves.

## Quick verbs the user uses

| User says… | You do |
|---|---|
| "fire up the video chat" / "fire up the system" / "bring up the bridge" / "start everything" / "set it all up" | **The full runbook below** (mainly: `./slop-bridge.sh`) |
| "tear down" / "give me my mic back" / "restore audio" / "stop the bridge" | Run `./slop-bridge-stop.sh` |
| "open clawd" (no bridge, just the page) | `open -a "Google Chrome" http://127.0.0.1:7900` after confirming `server.py` is up |

## "Fire up the video chat" — full runbook

This project is the **harness clone** at
`clawd-harness/projects/clawd-video-chat` — the live source of truth. (An older
standalone clone at `~/clawd/clawd-video-chat` is **orphaned; ignore it.**) The
desktop buttons `🎙 Clawd Bridge UP/DOWN.command` already point here.

**You (an agent) can run the whole bring-up yourself** — Automation +
Screen-Recording perms work from the Bash tool (tested). You do **not** need the
user to double-click the desktop button. Steps:

1. **Pre-flight (one-time-ish):** ensure the gitignored **`.env`** exists in this
   dir (PORT=7900 + ELEVENLABS/OPENCLAW/OPENAI secrets + `OPENCLAW_WS_URL`
   pointing at the backchannel proxy). If missing, copy it:
   `cp ~/clawd/clawd-video-chat/.env ./.env` (never commit it).
2. **Deps clawd talks to** (`slop-bridge.sh` warns if down):
   - **openclaw gateway** — launchd `ai.openclaw.gateway`, `ws://127.0.0.1:18789`.
     Restart: `launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway`.
   - **clawd-backchannel proxy** on **`:7851`** — clawd reaches the gateway
     *through* it (it does the gateway's Ed25519 nonce handshake server-side; the
     browser can't). Start: `(cd ~/clawd/clawd-backchannel && nohup python3
     server.py >/tmp/clawd-backchannel.log 2>&1 &)`. Without it → "gateway
     disconnected". (See `gateway-via-backchannel-proxy` memory.)
3. **Run it:** `./slop-bridge.sh` (self-contained — strips a stray `PORT`,
   self-heals the OBS bind, opens slop in the right Canary profile). It:
   1. Snapshots current audio devices → `~/.cache/clawd/slop-bridge.state`.
   2. Sets system in **and** out → **BlackHole 2ch**, and spawns a 2s watcher
      that re-pins them.
   3. Starts `python3 server.py` on `:7900` if not already up.
   4. Opens a fresh **Chrome** window at `http://127.0.0.1:7900` (clawd) and
      matches its CGWindow id.
   5. Patches OBS scene `Untitled.json` so the screen-capture source in scene
      `CLAWD` points at that window; launches OBS `--startvirtualcam`; binds the
      window live over obs-websocket (retries until OBS is ready).
   6. Opens `https://live.slop.computer/…` in **Chrome Canary**, **Default
      ("openclaw") profile** (has the slop mic permission + MetaMask wallet).
4. **Known first-run hiccup:** OBS may pop *"virtual camera is not installed."*
   It's usually a **stale warning** (the extension is already
   `activated enabled` — check `systemextensionsctl list | grep obs`). Click OK,
   quit+relaunch OBS (`open -ga OBS --args --startvirtualcam`), re-run
   `obs_bind_window.py`. No System Settings change needed.
5. **Verify:** gateway detail log `/tmp/openclaw/openclaw-<date>.log` shows
   `device pairing auto-approved role=operator` then `✓ sessions.patch` /
   `✓ chat.history`; `system_profiler SPCameraDataType | grep OBS` lists the
   virtual cam; audio in/out both `BlackHole 2ch`.
6. **Wallet connect is NOT yours.** The slop `[connect wallet]` flow fires
   clawd's MetaMask (a real signature/identity action). **Hand that to openclaw /
   the user — do not auto-click or sign.**

### Two agent-shell gotchas (already handled by the script; FYI)
- The clawd-harness exports **`PORT=8787`** into agent shells; `slop-bridge.sh`
  now `unset PORT`s before launching `server.py` so `.env`'s 7900 applies.
- This Mac's **LAN IP is DHCP** and has changed (was `.56`, now check
  `ipconfig getifaddr en0`). The backchannel page is
  `http://<lan-ip>:7850/?k=<BACKCHANNEL_TOKEN>` (token in `clawd-backchannel/.env`).

## `slop-bridge-stop.sh` — teardown

1. Kills the audio-defaults watcher, then closes any **Chrome** tabs pointing at
   `http://127.0.0.1:7900`.
2. Kills whatever is listening on port 7900 (the clawd server).
3. Restores the previous default in/out audio devices from the state file.

Doesn't quit Chrome/Chrome Canary/OBS themselves — close those manually if you
want them gone. (Note: if audio was already BlackHole 2ch at bring-up time, the
snapshot restores it to 2ch — it can't recover devices a *prior* run overwrote.)

## Why Chrome for clawd + Chrome Canary for slop?

Two browsers are required to break Chrome's same-process AEC link: when
one Chrome tab outputs audio and another reads the same loopback as its
mic, Chrome treats them as a "same loop" and silently suppresses the
input. Splitting across two separate browser processes (Chrome + Chrome
Canary) gives them independent audio-capture stacks.

Why **Chrome** for clawd and not Safari? Safari has a `getUserMedia` bug
where it silently returns audio from a different BlackHole device than
the deviceId we requested — `track.label` and `track.getSettings()` lie
about which device the stream is actually from. Chrome (and Chromium-
based browsers) honor the device choice correctly.

Why **Chrome Canary** for slop? Canary is a separate app/process from
stable Chrome, so the AEC link is broken, but it's still Chromium — so
slop's mic-device picker reliably opens BlackHole 2ch just like Chrome.
(Brave works equally well here for the same reason and was the prior
setup; the current desktop rig uses Chrome Canary.)

## Audio topology (what each cable carries)

Current desktop rig: **system input AND output are both BlackHole 2ch**
(each at full volume), pinned there by `slop-bridge.sh`'s watcher.

```
remote voices on slop (other computer)
  → slop tab in Chrome Canary plays → system default output = BlackHole 2ch
  → clawd's getUserMedia → mic meter
  → clawd's webkitSpeechRecognition (uses system default input
    = BlackHole 2ch) → SR meter → wake-word match → onSend
  → TTS chunks → routed via AudioContext.setSinkId(BlackHole 2ch)
  → Chrome Canary's slop tab mic input (set to BlackHole 2ch in site
    permissions) → broadcast to remote participants
```

OBS captures the **clawd-video-chat Chrome window** as the slop camera
feed; OBS audio is muted (audio routes through BlackHole, not OBS).

## Prereqs / one-time setup

- BlackHole 2ch installed (`brew install blackhole-2ch`). (16ch is no
  longer used by the current desktop rig — both in/out are 2ch.)
- `brew install switchaudio-osx`.
- OBS configured with:
  - Scene named `CLAWD`
  - Window-capture source inside it named `CLAWDSCREEN`
- Screen Recording permission for whatever Terminal app you run
  `slop-bridge.sh` from (the script uses a swift snippet to enumerate
  CGWindows; macOS gates this on Screen Recording perms).
- One-time per-browser:
  - Chrome first-launch of clawd: allow mic when prompted (mic = BlackHole 2ch).
  - Chrome Canary first-launch of slop: allow mic → device picker → **BlackHole 2ch**.
- In OBS: audio should be **muted** on the virtual cam output (we route
  audio through BlackHole, not through OBS).

## In-page debugging aids

In OBS mode (default) the page shows three meters bottom-left:
- **🎤 MIC** — raw `getUserMedia` analyser level off BlackHole 2ch.
- **🗣️ SR** — pulses each time `webkitSpeechRecognition` fires `onresult`.
- **🔊 OUT** — raw analyser level off the TTS audio routed to BlackHole 2ch.

Plus a `routing:` line under them showing the actual `OUT →` sink and
`IN ←` track label. Known-good reads `OUT → BlackHole 2ch` and
`IN ← BlackHole 2ch (Virtual)`. If `IN` says anything else, the system
default input was changed (re-run `slop-bridge.sh`).

If MIC moves but SR stays dark, SR is broken on whatever device is the
current default input — check Sound settings, then restart the browser
running clawd (SR caches the device per browser-process).

Press **Shift+D** anywhere in the page to dump full state to the
JavaScript console.

## Files in this repo

- `index.html` — the entire frontend (single file, no build step).
- `server.py` — HTTP + TTS proxy, port 7900.
- `slop-bridge.sh` / `slop-bridge-stop.sh` — bridge bring-up / tear-down.
- `stream-setup.sh` — older Chrome `--app` + OBS setup, kept for the
  non-slop streaming workflow.
- `clawdassets/` — avatar video clips.
- **The `claude -p` call brain** (folded in from the old `claude-code-driver`
  branch/worktree on 2026-06-22 — now one branch, one directory):
  - `cc-bridge.py` — WS gateway-protocol bridge on `:7861` that runs `claude -p`
    as the brain (launchd `com.clawd.cc-bridge`). Three voice trust tiers live
    here (`VOICE_SYS` guarded / `VOICE_TRUSTED_SYS` full-access / `PRIVATE_SYS`).
  - `cc-watcher.py` — cross-turn worker watcher (launchd `com.clawd.cc-watcher`).
  - `cc-cdp.py` — CDP client for the slop Canary on `:9222`.
  - `deploy/com.clawd.cc-bridge.plist` / `…cc-watcher.plist` — the launchd jobs
    (ProgramArguments point here, in `projects/clawd-video-chat`).
  - `SERVICES.md` — the always-on daemons + off-switches.
