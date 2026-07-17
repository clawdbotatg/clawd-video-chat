# clawd on Google Meet — silent note-taker (default) or talking participant

clawd can sit in a Google Meet **signed in as himself** (camera ON = his avatar via
the OBS virtual cam, **mic muted**) and keep a **per-meeting transcript**. He's a
visible, silent participant by default: he watches and listens.

He can also **talk on the call**: `meet talk` unmutes his Meet mic and flips the
:7900 page into **phone-call mode** (no wake word; the Haiku gate decides each
turn) — see "Talk mode" below. `meet hush` returns him to silent note-taking.

You drive it all from the **backchannel**: paste a Meet link, say "join and take
notes" (silent) or "join and talk" / "put yourself in phone mode" (voice), and his
brain runs `tools/local/meet join <url>` / `meet talk`.

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
| `meet` helper | `claude-p-agent/tools/local/meet` | `join` / `talk` / `hush` / `status` / `leave` / `summary` / `transcript` / `list`. The verb clawd's brain calls. |
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
   - set the **microphone device** to **BlackHole 16ch** (even though he joins
     muted — this is the TTS cable that carries his voice into the room when
     `meet talk` unmutes; 2ch is the wrong side of the split-cable rig — it
     carries remote voices, so picking it would broadcast the room back at
     itself and drop clawd's own voice),
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

## Talk mode — clawd speaks on the call (`meet talk` / `meet hush`)

`meet talk` does two things (must already be IN the call — admitted, not lobby):
1. **CDP-clicks "Turn on microphone"** in the Meet tab → his TTS (already routed
   to BlackHole 16ch, the split-cable rig's TTS cable) is now broadcast to the
   meeting.
2. **POSTs `:7900/trigger-phone {"on":true}`** → SSE `phone-on` → the voice page
   enters **phone-call mode**: no wake word, continuous turns, the per-turn Haiku
   gate (`/api/should-respond`) decides "is it my turn?" so he doesn't answer
   every sentence in a multi-person meeting, and he only speaks `[SAY]` text.

`meet hush` reverses both (mute + phone mode off); **`meet leave` also drops
phone mode automatically** so he never keeps conversing after the call. The
deterministic `{"on":true|false}` body (vs the backchannel button's blind toggle)
exists because an agent can't see the page state. The 📞 button on the
backchannel page still toggles, and Shift+P on the voice page still works —
`meet status` shows `mic LIVE|MUTED` so you can tell where you are.

You can drive it entirely from the backchannel: "talk on the call" → brain runs
`meet talk`; "go quiet" → `meet hush`.

## Day-to-day flow
1. Bridge up as usual (`./slop-bridge.sh`) — gives you OBS virtual cam + BlackHole.
2. On the backchannel: paste the Meet link, "join and take notes."
3. clawd → `meet join <url>` → he appears (avatar), muted, transcript running.
4. During: ask him things on the backchannel; he answers from `meet summary` /
   `stt ask` without speaking on the call.
5. Want him talking? "put yourself in phone mode" → `meet talk`. "go quiet" →
   `meet hush`.
6. End: "leave" → `meet leave` → he drops (phone mode off) + sends you the recap.

## Gotchas
- **Host admission:** if the meeting needs admitting, `meet join` reports `LOBBY`
  and the transcript only fills once a host lets him in. Admit "clawd" like any guest.
- **Meet DOM drift:** join/mute/leave use text + aria-label selectors (`Join now`,
  `Ask to join`, `Leave call`, `Turn off microphone`) — resilient, but if Google
  reworks Meet and a selector misses, `meet` says so and leaves the page open to
  finish by hand. Update the `JS_*` snippets in `tools/local/meet`.
- **Don't run a slop call and a Meet at the same time** unless you've thought about
  the audio — both rooms play into the same remote-voices cable (BlackHole 2ch)
  and both capture the same TTS cable (BlackHole 16ch), so clawd would hear (and
  transcribe) both rooms at once and speak into both at once.
- **BlackHole 16ch mute trap:** macOS remembers per-device volume/mute. If the
  16ch cable is muted, `meet talk` unmutes Meet but the room hears silence.
  `slop-bridge.sh` unmutes it at bring-up; see CLAUDE.md "Per-device mute trap"
  for the shell test.
