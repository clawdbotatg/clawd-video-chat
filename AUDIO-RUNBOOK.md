# AUDIO-RUNBOOK.md — the macOS audio rig, every knob that can silently break it, and the one-shot fixes

Handoff doc for whoever (agent or human) next touches audio on this rig. Everything
here was learned by breaking it live on a call. Read this **before** editing
`slop-bridge.sh`, and read it **first** when anyone says "clawd can't hear me",
"I can't hear clawd", or "why is my Mac blasting sounds".

The single most important idea: **macOS has three independent audio device slots,
and it remembers volume + mute per device, forever.** Almost every incident in this
repo's history is one of those slots or per-device levels being wrong while every
daemon, meter, and websocket looked green.

---

## 0. Transcript dead? Run this first

Since 2026-09-01 the pipeline tests **itself** every 20 min (launchd
`com.clawd.stt-selftest`) and self-heals — most outages should now end within
20 min, or you get a macOS notification that both heals failed. So when the
transcript looks dead:

```bash
tail -5 /tmp/stt-selftest.log     # did it already catch + heal it? (empty/old = it's been passing)
tail -20 ~/.cache/clawd/voice-debug.log   # the page's own story: recognizer restarts/errors, watchdog kicks, PTT holds
cd ~/clawd/clawd-harness/projects/clawd-video-chat && ./stt-selftest.sh; echo "exit=$?"
```

**"Hold-to-talk does nothing"** is the same pipeline: PTT captures whatever
the recognizer hears during the hold. Check `stt-log.jsonl` for the
`ev: ptt-down` / `ptt-up` rows — `heardChars: 0` with `srAgeS` large means
the ear was silent (recognizer stalled, or your voice isn't on the 2ch cable
— the MIC meter must move while you talk). The page now restarts a stalled
recognizer during the hold and posts the reason on the backchannel feed.

`exit=0` → the pipeline works **right now** (the run itself heals a stale
page). If words still don't show up in the backchannel after that, the
transcript pipeline isn't the problem — look downstream (backchannel page,
cc-bridge, the 🎬 roll-log counter) or at the slop room mic
([slop-room-audio-preflight]).

`exit=1` → it already tried both heals (page self-reload, tab reload). Now
escalate in this order — each step is something a heal-reload can't fix:

1. **Look at the page** (screenshot the window if remote:
   `screencapture -l <CGWindow id>`). An overlay or banner tells you the
   cause directly: "🎙 click to start" stuck = autoplay blocked (click it,
   once); **⚠ SR DEAF** = wrong default input (§4 row 2); routing line wrong
   = §4.
2. **Cable test** (§4 below) — distinguishes dead cable / volume-0 from a
   dead browser.
3. **Restart Chrome** (browser-wide audio wedge survives tab reloads;
   also applies the pending update) → re-run `./slop-bridge.sh` to rebind OBS.
4. Still dead → it's a new failure mode: diagnose, then **add it to §4, the
   incident log, and a memory** like every entry before it.

Or just tell an agent "run the transcript selftest" — it's one script.

## 1. The three slots (and what each must be during a call)

| Slot | macOS UI name | CLI read | CLI set | Must be | Why |
|---|---|---|---|---|---|
| **Default output** | Sound → Output | `SwitchAudioSource -c -t output` | `SwitchAudioSource -t output -s "…"` | **BlackHole 2ch** | The slop tab in Chrome Canary plays remote voices to the *default output*. 2ch is clawd's **ear**. |
| **Default input** | Sound → Input | `SwitchAudioSource -c -t input` | `SwitchAudioSource -t input -s "…"` | **BlackHole 2ch** | `webkitSpeechRecognition` (wake word + ambient transcript) follows the *OS default mic* and cannot be re-pointed from JS. |
| **Sound effects** | Sound → "Play sound effects through" | `SwitchAudioSource -c -t system` | `SwitchAudioSource -t system -s "…"` | **BlackHole 2ch** | Notification dings (Messages, Mail, Calendar…) play here. It does **NOT** follow the default output. Left on the built-in speakers it blasts the room mid-call (2026-08-20). |

And the one device that is deliberately **not** a system default:

| Cable | Role | Who picks it |
|---|---|---|
| **BlackHole 16ch** | clawd's **voice** → the call | The clawd page's OUT picker (`AudioContext.setSinkId`) sends TTS into it; the slop tab / Zoom / Meet pick it as *their own* mic in *their own* settings. |

**Never** make 16ch a system default of any slot:
- default **input** = 16ch → SR listens to clawd's own TTS → deaf to the room + self-barge (2026-08-05 incident, see §4).
- default **output** = 16ch → remote voices go into the call's mic → everyone hears themselves echoed.
- **sound effects** = 16ch → every text-message ding plays *into the call*.

Mnemonic: **2ch = ear (all three system slots). 16ch = mouth (apps pick it themselves).**

## 2. Per-device volume and mute — the invisible killers

macOS stores volume and mute **per device** and never resets them. `osascript`'s
`get/set volume` only touches the *current default* device, so a level on a
non-default device is invisible to every casual check.

Known ways this has deafened or silenced clawd with everything "green":

| Device | State found | Symptom | Found |
|---|---|---|---|
| 16ch | volume 8 + **muted** | Nobody on the call can hear clawd; OUT meter moves; no error anywhere | 2026-07-17 |
| 2ch | output volume **0** (not muted — `unmute` doesn't help) | Clawd hears nothing; MIC meter flat; all daemons up | 2026-08-18 |
| 2ch | input volume **0** | Same as above — input gain is its own knob | 2026-08-18 |
| Speakers | alert volume 100 on the sound-effects slot | Text dings blast the room | 2026-08-20 |

**Why volume-down deafens clawd:** while 2ch is the default output, the keyboard
volume keys adjust *2ch's* per-device level — i.e. how loud the remote voices are
inside clawd's ear cable. Volume 0 = silence into SR. macOS then remembers that 0 on
2ch forever. The bridge's watcher now floors any level < 50 back to 100 every 2s, so
the keys are effectively disabled during a call. **Don't "fix" a loud room with the
volume keys** — the room is loud because of the sound-effects slot (§1), not the
output volume.

`alert volume` (the "Alert volume" slider) is a separate, global level for the
sound-effects slot. Currently left at 100 on purpose: routed into 2ch it is a faint
blip in clawd's ear, harmless to SR. If fully silent dings are wanted during calls,
add `osascript -e 'set volume alert volume 0'` to bring-up and restore it at stop.

## 3. What the scripts guarantee (so you know what *not* to re-implement)

`slop-bridge.sh` (bring-up), in order:
1. Snapshots `PREV_OUT`, `PREV_IN`, `PREV_SYS` → `~/.cache/clawd/slop-bridge.state`.
   *Caveat:* if a prior run already left everything on BlackHole, the snapshot
   captures BlackHole and teardown "restores" to BlackHole. Fix by hand after stop
   (`SwitchAudioSource -t output -s "MacBook Pro Speakers"` etc.).
2. Hops the default output onto **16ch**, unmutes it, volume 100 (the only way to
   reach a non-default device's level via osascript), then pins output → **2ch**.
3. Unmutes 2ch; floors output volume < 50 → 100; pins input → **2ch**; floors input
   volume < 50 → 100.
4. Pins **sound effects → 2ch** (`-t system`); warns instead of dying if the
   installed `SwitchAudioSource` predates `-t system`.
5. Spawns the **2s watcher** (`~/.cache/clawd/slop-bridge-watch.pid`, log
   `/tmp/slop-bridge-watch.log`) that re-asserts all three slots → 2ch, re-floors 2ch
   output/input volume, and un-mutes. It exists because macOS auto-switches defaults
   when a device is plugged in (headphones, USB mic, AirPods).

`slop-bridge-stop.sh`: kills the watcher **first** (otherwise it re-pins within 2s),
then restores output, input, and sound effects from the state file (`PREV_SYS`
falls back to `PREV_OUT` for state files written before 2026-08-20).

**Because the watcher re-pins every 2s, any manual audio change during a call must
start with killing it:** `kill $(cat ~/.cache/clawd/slop-bridge-watch.pid)`.

## 4. Symptom → cause → fix

Run the full read first; it answers most of these in one shot:

```bash
for t in output input system; do printf "%-7s " $t; SwitchAudioSource -c -t $t; done
osascript -e 'get volume settings'          # levels of the CURRENT default device only
ps -p "$(cat ~/.cache/clawd/slop-bridge-watch.pid 2>/dev/null)" -o pid=,etime= 2>/dev/null || echo "watcher: not running"
```

| Symptom | Most likely cause | Fix (live, no tab reload) |
|---|---|---|
| Clawd deaf; MIC meter **flat**; page says `IN ← BlackHole 2ch` | 2ch output or input volume at 0 | `osascript -e 'set volume output volume 100' -e 'set volume input volume 100' -e 'set volume without output muted'` (with 2ch as current default). Confirm with the ffmpeg loopback test below. |
| Clawd deaf; MIC meter flat; page shows **⚠ SR DEAF** banner / `IN ←` ≠ 2ch | Default input moved (classic: 16ch, or AirPods auto-switched) | kill watcher → `SwitchAudioSource -t input -s "BlackHole 2ch"` → restart bridge later to get the watcher back |
| Clawd deaf; MIC meter **moves**; SR meter dark; transcript empty | Zombie recognizer (Chrome, after sleep/long uptime) | The SR flatline watchdog should self-heal (restart → reload). If not: reload the tab with `?stt=off`. See `CLAUDE.md` "SR FLATLINE". |
| Clawd deaf; page shows **"🎙 click to start clawd"** overlay | Autoplay blocked on this load, so the silent-WAV auto-unlock failed (ears should still be live since `1d70e99` — if the overlay is up AND he's deaf, the auto-start regressed) | Click the overlay once. If this recurs, check `bcLog srwd` for "autoplay blocked" and fix the auto-start path in `index.html`. |
| Transcript dead but **all of the above check out** | Chrome's browser-wide audio service wedged (survives tab reloads) | Quit + relaunch **Chrome** (not just the tab), re-run `slop-bridge.sh` to rebind OBS. Cable test (§4) first to prove it's the browser. |
| SR bar blazing **while clawd speaks**; phone mode interrupts himself | Both directions on one cable: page OUT → 2ch, or 16ch missing | Page OUT picker → BlackHole 16ch; `brew install blackhole-16ch` if absent; verify `routing: OUT → BlackHole 16ch` |
| Call can't hear clawd; OUT meter moves | 16ch muted / volume 0, **or** slop/Zoom mic not set to 16ch | Re-run `slop-bridge.sh` (unmutes 16ch) — then check the app's own mic picker. ffmpeg test on 16ch below. |
| **Mac speakers blast notification sounds mid-call** | Sound-effects slot on the speakers | `SwitchAudioSource -t system -s "BlackHole 2ch"` (bridge now does this; a pre-fix watcher won't undo it) |
| Turning volume **down** makes clawd deaf | Expected — keys adjust 2ch's level. Watcher floors it back. | Don't touch the keys; fix whatever was actually loud (usually the row above) |
| Everyone on the call hears themselves echoed | Default output = 16ch | kill watcher → output → 2ch |
| Text dings audible **to the call** | Sound effects = 16ch | `SwitchAudioSource -t system -s "BlackHole 2ch"` |
| After teardown the Mac makes no sound at all | State file captured BlackHole as "previous" | `SwitchAudioSource -t output -s "MacBook Pro Speakers"; SwitchAudioSource -t system -s "MacBook Pro Speakers"` |

### Cable loopback test (proves a cable end-to-end from the shell)

```bash
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -i blackhole   # find the index
ffmpeg -y -f avfoundation -i ":<idx>" -t 3 /tmp/x.wav & say -a "BlackHole 2ch" testing; wait
ffmpeg -i /tmp/x.wav -af astats -f null - 2>&1 | grep -i "RMS level"       # -inf dB = dead cable
```

Swap the device name/index to test 16ch. This is the one test that distinguishes
"level is 0" from "nothing is playing into it" without trusting any meter.

## 5. Rules for editing `slop-bridge.sh`

1. **Commit audio changes.** The 2026-08-05 deafness was an *uncommitted* edit that
   pinned the default input to 16ch; nobody could see it in `git log`. Uncommitted
   edits to this script are a live hazard, not a draft.
2. Never point any of the three system slots at 16ch, whatever app "needs to hear
   clawd". That app picks 16ch in its own mic settings. Full reasoning lives in the
   `SYS_INPUT_DEVICE` comment in the script.
3. Anything you pin at bring-up must also be (a) re-asserted by the watcher and
   (b) restored by `slop-bridge-stop.sh` from the state file. Three places, always.
4. Test with the watcher **stopped**; otherwise it silently reverts your experiment
   within 2s and you conclude the wrong thing.
5. When you add a trap here, add a row to §4 and a memory entry — the symptoms all
   look identical ("everything's green, he's deaf"), so the table is the diagnostic.

## 6. Incident log (newest first)

- **2026-09-03** — PTT hold captured nothing on a live show; selftest had
  been FAILing every 20–40 min for two days (34 times), one heal-reload
  mid-call. Root cause: the 09-01 prophylactic kick + Chrome's LATE `onend`
  for an aborted recognizer → perpetual abort/restart chain (each recognizer
  lived ~250ms — zombie by construction; only a reload broke it). Fixes:
  identity-guarded `onend` + `test_sr_lifecycle.mjs`; PTT restarts a
  stale/stalled recognizer and reports an empty hold; selftest trusts a
  flowing transcript (no marker over a busy room, 15s wait); `/api/debug`
  persisted to `~/.cache/clawd/voice-debug.log`; SR error backoff.
- **2026-09-01** — The recurring "transcript dead" finally root-caused: the
  audio-unlock overlay gated SR behind a human click, so every *unattended*
  heal-reload (including the 08-10 watchdog's own stage-2) parked the page
  deaf on the overlay. Also found: the watchdog was blind when sleep killed
  the metering path with the recognizer (suspended AudioContext / muted
  track), and Chrome's browser-wide audio service can wedge across tab
  reloads. Fixes: ears auto-start on every load + autoplay-probe overlay
  dismissal, watchdog blind-spot patches, and the `com.clawd.stt-selftest`
  end-to-end prober daemon (§0). Commits `97c4197`, `1d70e99`. Contract:
  **EXPECTATIONS.md**.
- **2026-08-20** — Text-message dings blasting the speakers mid-call; user lowered
  volume, which deafened clawd (2ch level → 0). Root cause: sound-effects slot on
  MacBook Pro Speakers. Fix: pin `-t system` → 2ch in bring-up + watcher + stop
  (commit `6575714`).
- **2026-08-18** — Clawd deaf, all daemons green. 2ch output *and* input volume at 0
  (not muted). Fix: floor levels < 50 → 100 at bring-up and in the watcher
  (commit `27a3363`).
- **2026-08-10** — Daily "zombie recognizer" transcript death. Fix: SR flatline
  watchdog in `index.html` (commit `768202d`).
- **2026-08-05** — Clawd deaf on a live call. Uncommitted `slop-bridge.sh` edit had
  set default input → 16ch. Fix: revert + **⚠ SR DEAF** banner.
- **2026-07-17** — Nobody could hear clawd. 16ch at volume 8 + muted. Fix: unmute
  16ch at every bring-up. Likely why the rig had earlier collapsed to one cable.
- **2026-07** — Phone mode interrupting himself. Both directions on one 2ch cable.
  Fix: split-cable rig (2ch ear / 16ch mouth).

Related: `CLAUDE.md` (Audio topology, debugging aids), `SERVICES.md` (daemons),
`INPUTS-AND-CHANNELS.md` (what SR feeds).
