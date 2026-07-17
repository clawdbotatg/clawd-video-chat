# Group-call mode — design (not built yet)

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
