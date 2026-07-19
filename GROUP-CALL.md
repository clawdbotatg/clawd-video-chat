# Group-call mode — design + v1

**Status: v1 is BUILT (2026-07-17).** Shift+G on the voice page (or POST
`/trigger-phone {"mode":"group","on":true}`) turns it on — the badge reads
"👥 group call". Server side is the `mode:"group"` branch of
`/api/should-respond` (`_group_gate` in server.py); page side is
`submitGroupTurn` + `groupGate` in index.html. **:7900 must be restarted once
to load the server half** (page half arrives on reload). Watch every verdict
live in the backchannel debug feed (`👥🤫 quiet` / `👥📝 noted` /
`👥💬 interject` / `👥⏳ held by cooldown` / `👥 addressed`) — that feed is the
tuning loop. v1.1 (lull timer) and v1.2 (note → research runs) are not built.

The third listening mode, after wake-word and phone. Clawd sits on a
multi-person call, hears everything, and *can* speak — but almost never does.
He behaves like a competent, slightly shy human participant: quietly listening,
maybe researching what people mention, and only talking when (a) someone
addresses him, (b) he's asked something only he can answer, or (c) the group is
visibly stuck, the floor is open, and he actually has the answer.

Design premise (from Austin): **Claude thinks he's far more important to a
conversation than he is.** Left to a prompt alone he will interject constantly.
So restraint here is enforced by *code* (defaults, budgets, cooldowns), with the
prompt only shaping judgment inside those hard limits.

## The north star, and the subtle tensions under it

**The goal is presence, not silence.** Clawd should feel like part of the
conversation — a colleague who's clearly *in the room* — without answering
everything. Mute-recorder is failure just like chatterbox is failure. The whole
feature lives in the narrow band between them, and every design choice below is
a trade along one of these tensions. None of them fully resolve; they get
*tuned*, on real calls, over time.

**1. There is no algorithm for when humans talk.** (Austin's core pushback.)
People read a gestalt — who's talking, energy, whether a thought landed,
whether the floor is open. You cannot write that as rules, and trying produces
something robotic. So the *judgment* lives in the gate model, holistically,
with as much room context as we can feed it. But —

**2. — the model's judgment is biased, so code must cap it.** Claude reliably
overestimates how much a group wants to hear from him. A prompt alone will not
hold. Resolution: the LLM decides *whether this is a moment*; deterministic
code decides *whether he's allowed another unprompted moment yet* (cooldown),
*which way failures fall* (quiet), and *what plays before the gate returns*
(nothing, unless addressed). The mechanics are a speed limiter, not a driving
algorithm — if the cooldown is doing the talking-decision work day-to-day, the
gate prompt is mistuned.

**3. Knowing the answer is not an invitation.** The strongest wrong instinct:
"they're discussing X, I know about X, therefore speak." A human expert sits
through whole meetings about their specialty. Value is necessary but not
sufficient — what makes an interjection welcome is value *plus an open floor*
(a question nobody's answering, a stuck moment, a lull). Content says "could
speak"; timing says "may speak." This is why the lull timer (v1.1) matters:
the beat of waiting IS the social skill.

**4. Quiet is not idle — readiness is a form of presence.** The `note` verdict
exists because listening should accumulate: research what they're stuck on,
remember what was decided, hold the answer. When someone finally says "clawd,
did you catch any of that?" and he has it *instantly and specifically*, that's
the moment he feels like he was part of the conversation all along — earned
during the silence, not the speaking.

**5. Fail-safe direction flips with the social contract — except when
addressed.** Phone mode fails LOUD (going silent mid-1:1 breaks the call);
group mode fails QUIET (an unwanted interruption costs more than a missed
chance). But a direct address is a 1:1 moment inside the group — "hey clawd,
takeaways?" answered with silence is the worst outcome the feature can produce.
Hence the deterministic Tier-0: addressed turns never touch the fallible path
at all.

**6. Talked *to*, talked *about*, and neither.** Being mentioned ("clawd could
probably look that up") isn't an address, but responding to it is deeply human
— you perk up at your name. It gets a lower bar (bypasses the cooldown, gate
leans in) without the guarantees of a direct address. With no diarization,
this whole distinction rests on wording alone — the gate's hardest reads live
here, and it will sometimes be wrong in both directions.

**7. An interjection must land in ITS moment.** Group conversation moves;
by the time a gate verdict + brain run + TTS arrives, the topic may have too.
A perfect answer to the thing from forty seconds ago reads as robotic — worse
than silence. Stale-verdict drops, short interjections ("one contribution,
then yield"), and yield-on-overlap all serve the same principle: match the
room's rhythm or stay out of it.

**8. This will be tuned, not solved.** The feel we're after is subtle and the
first prompt will be wrong in ways only real calls reveal. The instruments:
the backchannel debug feed (every verdict + one-line reason, live), the stt-log
(every real call becomes a replayable eval set for candidate prompts), and one
number — unsolicited interjections per hour (target < ~2). Iterate the gate
prompt against recordings; move a mechanic into code only when the model
proves it can't hold the line itself.

## Why phone mode can't just be reused

Phone mode (Shift+P) is built for a 1:1 call where clawd is one of the two
parties — every default points the wrong way for a group:

| Phone mode today | Group mode needs |
|---|---|
| Gate biased **YES** ("when unsure, lean YES"); any gate error → respond | Biased **QUIET**; gate error → stay silent (silence is the safe failure in a group) |
| `phoneOpener()` fires an instant "hmm" on *every* end-of-turn, before the gate returns | No opener unless directly addressed — he'd grunt at every sentence of a meeting |
| Gate context = brain chat history (`fillerHistorySnapshot`) — fine 1:1 because every approved turn reaches the brain | Most group speech never reaches the brain, so the gate must see the **room transcript** (stt-log) or it's blind |
| Binary respond / quiet | Three verdicts: **speak / quiet / note** (note = work silently, be ready) |
| No self-restraint needed (it's his call, he's supposed to talk) | Hard interjection budget + cooldown, yield-on-overlap |

## The gate, redesigned (three tiers)

**Tier 0 — deterministic, client-side, free.**
- **Addressed** (wake-phrase variants of `WAKE_RE`, or name + a question aimed
  at him): → respond, no LLM gate, opener allowed. "Hey clawd, takeaways from
  this meeting?" must never be lost to a flaky gate.
- **Bare name mention** ("clawd could probably do that") — deliberately NOT a
  wake in wake-mode (podcast problem), but in group mode it's exactly the
  "talking about him" signal → route to Tier 1 with `mentioned:true`.
- Everything else still goes to Tier 1 on end-of-turn (Haiku is cheap and the
  verdict isn't latency-visible — the default outcome is silence).

**Tier 1 — the group gate (server, Haiku).** Extend `/api/should-respond` with
`mode:"group"` (same endpoint, new prompt + context):
- **Context = the room, not the brain:** last ~20 lines of the STT firehose —
  the server already owns `stt-log.jsonl` and has `_read_stt_lines()` for
  `/api/stt/ask`, so the page doesn't need to ship history. Plus: when clawd
  last spoke, and whether he's inside his interjection cooldown.
- **Returns** `{verdict: "speak"|"quiet"|"note", reason}` instead of a bool.
- **Prompt inversion:** "You are a mostly-silent participant in a group call.
  QUIET is the correct answer the vast majority of the time. Someone mentioning
  a topic you know about is NOT a reason to speak. Speak only if directly
  addressed, asked something only you can answer, or the group is explicitly
  stuck with the floor open and you have the concrete answer. When unsure →
  QUIET."
- **Failsafe inversion:** any error → `quiet` (except Tier-0 addressed, which
  never reached the gate anyway).

**Tier 2 — the brain is the final gate, for free.** The SAY inversion already
means clawd only speaks `[SAY]`-wrapped text. For *unaddressed* speak verdicts,
the escalation prompt says "you may conclude there's nothing worth adding —
reply without [SAY] and you stay silent." A gate false-positive then costs
tokens, not room noise. Addressed turns keep the normal must-answer contract.

## Restraint mechanics (code, not vibes)

- **Interjection budget:** unaddressed speech at most once per
  `GROUP_INTERJECT_COOLDOWN_MS` (start ~4 min), enforced in `submitPhoneTurn`'s
  group branch — the gate isn't even asked for "speak" eligibility during
  cooldown (still asked for `note`). Addressed turns bypass entirely.
- **Tell the model its own state:** the gate prompt includes "you last spoke
  unprompted N minutes ago" so judgment and budget agree.
- **Short interjections:** unsolicited `[SAY]` is capped by prompt to a sentence
  or two — "make one contribution, then yield."
- **Yield on overlap:** if anyone talks while he's speaking an unsolicited line
  → immediate `ttsReset()`, no retry, no "as I was saying." (Addressed replies
  keep the normal barge behavior.)

## The "open floor" moment (the magic case)

"The group is stuck and it's the perfect time to interject" is a *timing*
pattern, not just content: a question to the room that nobody answers.
Mechanism: when a turn ends question-shaped and un-addressed, arm a
`GROUP_LULL_MS` (~4s) timer; any new human speech cancels it; if it fires,
re-run the gate with "that question has hung unanswered for 4 seconds — the
floor is open." Humans wait a beat before jumping in; the timer IS the beat.

## The "note" verdict — silent researcher

`note` = don't speak, but hand the moment to the brain as background work,
using the existing quiet-turn plumbing (`_quietTurn` / QUIET_ASK_RE path — no
TTS, no avatar, no process speech): "[GROUP-QUIET] They're discussing X and
seem unsure about Y — look it up and hold the answer." Findings accumulate in
the one fluid session, so when he's later addressed ("clawd, what did you
find?") or an open floor arrives, he already has it. Rate-limit notes too
(~1/min max) so a lively call doesn't melt the brain.

`stt ask` + the per-meeting transcript API already cover recall ("takeaways
from this meeting") — group mode inherits them.

## Honest hard parts

- **No diarization.** SR is one undifferentiated text stream — the gate can't
  tell two debaters from one monologue, and "are they talking to me or about
  me" rests entirely on wording. Accept for v1. (Meet-specific upgrade later:
  scrape Meet's own captions, which carry *speaker names* — that would be a
  step change for the gate.)
- **Crosstalk garbles STT.** Overlapping speakers → word salad; the gate prompt
  must tolerate garbage lines and never treat garble as an invitation.
- **Prompt tuning is the real work.** The backchannel debug feed (every gate
  verdict + reason, live on the phone) is the tuning instrument. Better: the
  stt-log of any real call is a replayable eval set — a small harness that
  re-runs recorded transcripts through candidate gate prompts and reports
  unsolicited-speak rate would turn tuning into a measurement. Target: < ~2
  unsolicited interjections/hour.

## Phasing

1. **v1:** `_groupMode` (Shift+G + badge + backchannel 👥 button +
   `/trigger-phone {mode:"group"}`), no opener unless addressed, Tier-0 regex,
   `mode:"group"` gate with stt-log context + inverted failsafe, interjection
   cooldown, yield-on-overlap.
2. **v1.1:** open-floor lull timer.
3. **v1.2:** `note` verdict → quiet-turn research runs.
4. **v2:** gate-eval harness over recorded stt-logs; Meet caption names.

Implementation note: keep phone mode's machinery (SR arming, silence timer,
stale-verdict drop) and branch on `_groupMode` inside `submitPhoneTurn` — the
turn-detection layer is identical; only gate/opener/budget behavior differs.
