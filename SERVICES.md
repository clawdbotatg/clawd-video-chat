# Background services — what's always running, and how to stop it

This rig leaves several **always-on daemons** running on **leftclaw**
(`atgsilver-3.local`). They're managed by `launchd`, which restarts them if they
die (`KeepAlive`) and starts them at login (`RunAtLoad`). This file exists so that
when you've moved on and forgotten the details, you can still answer "what's running
on this box, and how do I turn it off?" without archaeology.

> ⚠️ **launchd watches *liveness*, not *usefulness*.** It only knows whether a
> process is alive — not whether it's still correct or wanted. If you redesign the
> call system later (change the bridge protocol, the `sessionKey`, ports), these keep
> running on the **old assumptions**. Worst case a stale daemon keeps poking the
> brain. So: if you tear down or rework the call rig, **also stop the services below**
> — they will not stop themselves.

## The ports at a glance

Four ports carry the whole call. The mental model: **`:7900` senses it · `:7861`
thinks it · `:8787` does the coding · `:7851` is the private channel into the brain.**
Only `:7861` runs *clawd himself* (a `claude -p`); the rest are the surfaces and
tools around him.

## The services

| Label (launchd) | What it is | Port | Log |
|---|---|---|---|
| call page (`server.py`) | clawd's **eyes/ears/mouth** — serves the browser UI on the call: speech recognition ("okay clawd" wake word), TTS, the avatar, the backchannel input box, and writes the STT transcript log. Started by `slop-bridge.sh`, **not** launchd. | `7900` | `/tmp/clawd-vchat-7900.log` |
| `com.clawd.cc-bridge` | The **brain** — `claude -p` WS bridge that backs the call; spawns a `claude -p` (cwd=`clawd-agent`) per message. Drop-in for the old openclaw gateway. | `7861` | `~/.cache/clawd/cc-bridge.log` |
| `com.clawd.cc-watcher` | The **heartbeat** — watches coding workers and pings the brain when one blocks/finishes (see below). | — (WS client) | `~/.cache/clawd/cc-watcher.log` + `/tmp/cc-watcher.log` |
| clawd-harness | The **worker engine** (clawd's hands) — spawns/observes coding `claude` sessions per repo. (Its own launchd label is `com.clawd.harness`.) | `8787` | harness logs |
| clawd-backchannel proxy | The **private ear** — lets the browser reach the bridge through the Ed25519 handshake; carries Austin's PRIVATE messages. Started by `slop-bridge.sh`, not launchd. | `7851` | `/tmp/clawd-backchannel.log` |

## Is it healthy? / what's running?

```bash
launchctl list | grep -E 'cc-bridge|cc-watcher|clawd.harness'   # alive? (col 1 = PID, col 2 = last exit code)
lsof -nP -iTCP:7900 -sTCP:LISTEN     # call page (eyes/ears/mouth)
lsof -nP -iTCP:7861 -sTCP:LISTEN     # bridge (brain)
lsof -nP -iTCP:8787 -sTCP:LISTEN     # harness (hands)
lsof -nP -iTCP:7851 -sTCP:LISTEN     # backchannel proxy (private ear)
tail -f ~/.cache/clawd/cc-watcher.log
```

## Turning a service off

```bash
# pause (stays gone until you reload or next login... actually KeepAlive: unload to stop)
launchctl unload ~/Library/LaunchAgents/com.clawd.cc-watcher.plist
launchctl unload ~/Library/LaunchAgents/com.clawd.cc-bridge.plist

# remove permanently (also delete the plist so it won't come back at login)
launchctl unload ~/Library/LaunchAgents/com.clawd.cc-watcher.plist && rm ~/Library/LaunchAgents/com.clawd.cc-watcher.plist

# restart after editing the code
launchctl kickstart -k gui/$(id -u)/com.clawd.cc-watcher
```

The plists are version-controlled in `deploy/` here; the live copies are in
`~/Library/LaunchAgents/`. To (re)install: `cp deploy/<label>.plist
~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/<label>.plist`.

## What cc-watcher actually does (the part most likely to surprise future-you)

The brain is **reactive** — it only runs when a voice/backchannel message arrives, so
it can't watch a long-running task on its own. `cc-watcher.py` is the loop it lacks:

1. It follows the harness (`:8787`) and notices when a **coding worker** clawd
   started goes **blocked** (needs an answer) or **idle** (a turn finished).
2. It then injects a `[PRIVATE] [auto-watch] …` message to the bridge (`:7861`,
   `sessionKey=agent:clawd:main`) telling clawd what to do — which wakes him in a
   normal backchannel turn.

So **if you see clawd getting `[auto-watch]` backchannel messages "on his own," this
is why** — it's the watcher, not a ghost. It only tracks the **worker sessions clawd
actually started** — the `code` helper records each one in `.code-workers.json` and
the watcher reads that registry, so it never nags about your own interactive sessions
even when they live in an eligible project. When no such workers are active it does
nothing
(measured 0% CPU). It's idle-gated so it never wakes the brain mid-reply. Set
`CC_WATCHER_DRYRUN=1` to make it log intended wakes instead of sending them.

Related: the coding orchestrator itself (`~/clawd/clawd-harness/projects/clawd-agent/code`) and the worker
contract / loop live in the call-brain repo, not here.
