# clawd on Google Meet — silent note-taker mode

clawd can sit in a Google Meet **signed in as himself** (camera ON = his avatar via
the OBS virtual cam, **mic muted**) and keep a **per-meeting transcript**. He's a
visible, silent participant: he watches and listens, he doesn't talk on the call.

You drive it from the **backchannel**: paste a Meet link, say "join and take notes,"
and his brain runs `tools/local/meet join <url>`.

## Why this is mostly the existing rig
Google Meet is **just another room** for the audio/video plumbing:
- **Audio:** the Meet tab plays remote voices → system output = **BlackHole 2ch** →
  the :7900 page's speech-recognition transcribes it. Identical to the slop path.
- **Video:** OBS captures the :7900 avatar window → Meet selects **OBS Virtual
  Camera** as its camera. Identical to slop.
- **Transcript:** the :7900 page already logs every non-echo chunk to the STT
  firehose. A "meeting" just carves a named slice out of it (see below).

The only genuinely new parts are: a **dedicated signed-in browser profile** for
Meet, and a **join/transcript helper** (`meet`).

## The pieces

| Piece | Where | What |
|---|---|---|
| `meet` helper | `claude-p-agent/tools/local/meet` | `join` / `status` / `leave` / `summary` / `transcript` / `list`. The verb clawd's brain calls. |
| Meet browser launcher | `claude-p-agent/tools/local/open-meet-chrome.sh` | Opens the Meet URL in a dedicated Canary profile, debug port **9223**. |
| Per-meeting transcript API | `server.py` (:7900) | `POST /api/meeting/start\|stop\|summary`, `GET /api/meeting/status\|list`. Mirrors STT lines into a per-meeting file + a Haiku recap. |
| Transcript files | `claude-p-agent/meetings/<id>.jsonl` + `current.json` | One JSONL per meeting; `current.json` is the active-meeting pointer (survives a server restart mid-call). |
| Brain wiring | `claude-p-agent/CLAUDE.md` | "Joining a Google Meet" section — tells clawd to be silent + use `meet`. |

## Browser isolation — why a separate Canary profile (don't collapse it)
The Meet tab **must be a different browser process** from the :7900 page. The :7900
page reads BlackHole as its mic; the Meet tab plays remote audio to BlackHole. In
the **same** Chrome process Chrome's AEC treats them as "one loop" and silently
mutes clawd's input — the exact bug that forces slop into Canary. So:

- **:7900 page** → stable Google Chrome (as today).
- **Meet** → Chrome Canary, dedicated `--user-data-dir=~/.clawd-meet/user-data`,
  port 9223. Separate process from BOTH the :7900 Chrome and the slop
  openclaw-Canary profile (so the Google login never tangles with the wallet).

## One-time setup (the part automation can't do)
Google login can't be scripted (2FA/captcha). Do this **once**:

1. Launch the Meet profile and sign clawd's Google account in:
   ```bash
   ~/clawd/clawd-harness/projects/claude-p-agent/tools/local/open-meet-chrome.sh \
     "https://meet.google.com/landing"
   ```
   (or any Meet URL). Sign in with clawd's account in the window that opens.
2. Start/join any test meeting once and, in Meet's pre-join screen:
   - set the **camera** to **OBS Virtual Camera** (needs OBS up with the virtual
     cam running — i.e. the normal bridge OBS),
   - **mute the mic**,
   - grant camera+mic permission when Chrome prompts.
   Meet remembers these per profile, so every later `meet join` is just a click.

After that, `meet join <url>` handles muting + the Join/Ask-to-join click itself.

## Per-meeting transcript API (lives in server.py, :7900)
Additive — the always-on STT firehose (`stt-log.jsonl`) is untouched; when a
meeting is active each heard line is **also** appended to `meetings/<id>.jsonl`.

- `POST /api/meeting/start {title?,url?}` → `{id,title,url,started}`
- `POST /api/meeting/stop` → `{id,lines,minutes}`
- `GET  /api/meeting/status` → `{active,id,title,lines,…}`
- `GET  /api/meeting/list` → recent meetings
- `POST /api/meeting/summary {id?}` → markdown recap via the Bankr/Haiku proxy
  (same model wired for `stt ask`), grounded only in the transcript.

> **The live :7900 must be restarted once** to pick up these endpoints (they're new).
> It's launched by `slop-bridge.sh`, not launchd, so either re-run the bridge or:
> `env -u PORT -u ELEVENLABS_VOICE_ID python3 server.py` from this dir (so `.env`'s
> port/voice win — see the harness-env-leak note in memory).

## Day-to-day flow
1. Bridge up as usual (`./slop-bridge.sh`) — gives you OBS virtual cam + BlackHole.
2. On the backchannel: paste the Meet link, "join and take notes."
3. clawd → `meet join <url>` → he appears (avatar), muted, transcript running.
4. During: ask him things on the backchannel; he answers from `meet summary` /
   `stt ask` without speaking on the call.
5. End: "leave" → `meet leave` → he drops + sends you the recap.

## Gotchas
- **Host admission:** if the meeting needs admitting, `meet join` reports `LOBBY`
  and the transcript only fills once a host lets him in. Admit "clawd" like any guest.
- **Meet DOM drift:** join/mute/leave use text + aria-label selectors (`Join now`,
  `Ask to join`, `Leave call`, `Turn off microphone`) — resilient, but if Google
  reworks Meet and a selector misses, `meet` says so and leaves the page open to
  finish by hand. Update the `JS_*` snippets in `tools/local/meet`.
- **Don't run a slop call and a Meet at the same time** unless you've thought about
  the audio — both rooms share the one BlackHole bus, so clawd would hear (and
  transcribe) both at once.
