# EARBUD-MODE — clawd in your ear (plan, 2026-07-19)

**Goal:** a full phone loop with the agent through a single AirPod — mic and
speaker both in the ear, no wake word, walk-around conversation. "The agent in
my ear."

**Status: PLANNED, not built.** Nothing in this doc is implemented beyond what
already exists (phone mode, LAN HTTPS, the STT-upgrade path). This is the
roadmap for when we pick it up.

## Why we're ~90% there already

The conversational loop this needs is **phone-call mode (Shift+P)** — continuous
listen → silence-gap turn-taking → TTS reply → barge-in (see
`phone-call-mode` memory + `index.html` ~L3940). The remaining work is *which
device hosts the browser*, because that sets the constraints:

| Option | Where | Effort | What you get | Hard limit |
|---|---|---|---|---|
| A | Mac + AirPods | zero code, 10 min | validate the loop today | tethered to Bluetooth range of the Mac |
| B | iPhone + LAN HTTPS page | ~a day | whole-house coverage | iOS kills the mic on screen lock — screen must stay on |
| C | real phone call (Twilio/LiveKit bridge) | new component, 10× B | anywhere, screen locked, cellular | latency + cost + a streaming-STT rebuild |

**Recommended order: A (validate) → B (build) → C (only if off-LAN is actually wanted).**

## Option A — AirPods paired to the Mac (validation step)

1. Pair AirPods to the Mac; set system default **input and output** to them.
2. Run `server.py` alone — **do NOT run `slop-bridge.sh`**: its 2s watcher
   re-pins both defaults to BlackHole 2ch and will fight you.
3. Open `http://127.0.0.1:7900`, hit **Shift+P**.

webkitSpeechRecognition follows the system default input, so no code changes.
Echo is a non-issue vs the speaker rig: TTS is sealed in the ear canal and
AirPods do hardware echo cancellation in headset mode — barge-in should work
*better* than the desk rig. Known cosmetic wart: macOS drops AirPods to the
HFP codec while the mic is open (slightly muffled TTS).

## Option B — iPhone + LAN HTTPS page (the real build)

Open `https://atgsilver-4.local:7901/?k=<token>` on the phone, AirPods paired
to the phone, phone mode on. Infra already exists: HTTPS :7901 / wss :7852 /
token gate (`lan-https-access` memory). The key that makes iOS viable is the
**`?stt=openai` upgrade path** (`index.html` ~L4850, `/api/stt` in
`server.py`): mic segments recorded via getUserMedia, transcribed server-side —
so we don't depend on iOS Safari's flaky continuous webkitSpeechRecognition.

Work items:

1. **Trust the mkcert CA on the phone** — one-time profile install (same as any
   LAN HTTPS client).
2. **Make the STT-upgrade path truly SR-independent.** Today webkit SR still
   runs alongside it with its finals suppressed (`index.html:4229`). Confirm
   segmentation/silence detection comes from the mic analyser, not SR events —
   or make it so. Also confirm `/api/stt` accepts the `audio/mp4` blobs iOS
   MediaRecorder produces (desktop Chrome sends webm; OpenAI accepts m4a/mp4,
   but check our content-type handling).
3. **iOS lifecycle polish:** a tap-to-start gesture to unlock audio playback,
   and a screen Wake Lock (`navigator.wakeLock`) so the page survives idle.
   **Honest limit: iOS kills the mic when the screen locks.** In-hand or
   desk-propped works; locked-in-pocket does not — no web API fixes that
   (that's Option C's reason to exist).
4. **Frictionless boot (design decision, leaning yes):** when the page detects
   iOS (or via a `?phone=1` boot param), default into phone mode + OpenAI STT
   automatically, so phone bring-up is "open the bookmark, tap once" — no
   Shift+P (no keyboard!), no toggle fiddling. The 📞 badge is already
   tappable, but it shouldn't be a required step.
5. **Don't run concurrently with the desk rig** — one fluid brain session, and
   the LAN page misbehaves opened on two machines at once (`lan-https-access`
   memory).

## Option C — a real phone call (future, only if off-LAN wanted)

Dial clawd: a Twilio number (or LiveKit/WebRTC room) whose media stream bridges
to a small server doing streaming STT → cc-bridge brain (`:7861`) → TTS back
into the call. The only option that delivers "phone locked in pocket,
anywhere, cellular". Genuinely new component: telephony bridge, *streaming*
(not segment-based) STT, latency tuning. Treat as a separate project; don't
start it until B's screen-on limitation actually bites.
