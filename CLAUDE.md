# clawd-video-chat — operator notes for Claude

This is the single-page UI that puts clawd on a Zoom/Meet/slop call. See
`README.md` for the underlying architecture (wake word, WS protocol, TTS).
These notes are for the things you can't infer from reading code.

## Quick verbs the user uses

| User says… | You do |
|---|---|
| "fire up the system" / "bring up the bridge" / "start everything" / "set it all up" | Run `./slop-bridge.sh` |
| "tear down" / "give me my mic back" / "restore audio" / "stop the bridge" | Run `./slop-bridge-stop.sh` |
| "open clawd" (no bridge, just the page) | `open -a Safari http://127.0.0.1:7900` after confirming `server.py` is up |

## `slop-bridge.sh` — what it does

End-to-end startup for the working Chrome↔Brave BlackHole bridge:

1. Saves current macOS default in/out devices → `~/.cache/clawd/slop-bridge.state`.
2. Sets system input + output to **BlackHole 16ch** via `SwitchAudioSource`.
3. Starts `python3 server.py` if port 7900 isn't responding.
4. Opens a fresh **Chrome** window at `http://127.0.0.1:7900` (clawd).
5. Captures that window's CGWindow ID; patches OBS scene
   `Untitled.json` so source `CLAWDSCREEN` in scene `CLAWD` points at it
   (macOS Screen Capture, `application = com.google.Chrome`).
6. Launches OBS with `--startvirtualcam` (audio muted in OBS).
7. Opens `https://live.slop.computer/?invite=…` in **Brave**.

## `slop-bridge-stop.sh` — teardown

1. Closes any Safari tabs pointing at `http://127.0.0.1:7900`.
2. Kills whatever is listening on port 7900 (the clawd server).
3. Restores the previous default in/out audio devices from the state file.

Doesn't quit Safari/Chrome/OBS themselves — close those manually if you
want them gone.

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
