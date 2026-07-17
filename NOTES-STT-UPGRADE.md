# STT upgrade — server-proxied OpenAI transcription (feature-flagged, default OFF)

webkitSpeechRecognition mangles technical speech ("EIP", "multisig", "clawd
harness" → soup). This adds a higher-quality STT path using the **existing
`OPENAI_API_KEY`** already in `.env` for TTS — **no new credentials needed**.
It ships OFF; the live rig keeps running pure webkit SR until you flip it.

## How to flip it on / off

- **On:** open the voice page with `?stt=openai` (e.g.
  `http://127.0.0.1:7900/?stt=openai`). Sticky — persists in
  `localStorage["clawd.sttUpgrade"]` across reloads.
- **Off:** `?stt=off` (clears the sticky flag), or clear the localStorage key.
- The server endpoint (`POST /api/stt`) is always mounted but inert unless the
  page flag is on; it 503s if `OPENAI_API_KEY` is missing.

## What it does (architecture)

```
mic (BlackHole 2ch, meters.micStream)
  ├─ webkitSpeechRecognition — UNCHANGED, still running
  │    interims → wake arming, silence timers, barge detection (fast, sloppy)
  │    finals   → SUPPRESSED while the flag is on
  └─ MediaRecorder (opus/webm) + level-gated mini-VAD (analyser RMS)
       one blob per utterance → POST /api/stt
       → server → OpenAI /v1/audio/transcriptions (OPENAI_STT_MODEL,
         default gpt-4o-mini-transcribe, with a domain vocabulary prompt)
       → {"text"} → handleFinalChunk()  ← same entry point webkit finals used
```

Because the transcribed text enters through `handleFinalChunk()`, everything
downstream is identical: echo guard (`isLikelyEcho`), rolling room transcript,
`stt-log.jsonl`, wake-buffer growth. webkit interims still drive the *timing*
machinery, so wake responsiveness is unchanged; only the *recorded words* get
better. A bounded hold in `onSilenceElapsed` (≤4s) waits for an in-flight
transcription so the tail of an utterance isn't clipped off a submitted turn.

## Env knobs (server, all optional)

| Var | Default | Meaning |
|---|---|---|
| `OPENAI_STT_MODEL` | `gpt-4o-mini-transcribe` | `whisper-1` / `gpt-4o-transcribe` are drop-ins |
| `OPENAI_STT_PROMPT` | built-in domain prompt | vocabulary bias ("Ethereum, crypto wallets, AI agents, Claude…") |

Client tuning constants live next to `startSttUpgradeLoop()` in `index.html`:
`STT_SEG_START_PCT` (VAD open threshold, same scale as the MIC meter),
`STT_SEG_STOP_MS` (silence that closes a segment), `STT_SEG_MIN_MS`,
`STT_SEG_MAX_MS`.

## Tradeoffs / known limitations

- **Latency:** each utterance transcribes ~0.5–1.5s after it ends (HTTP round
  trip), vs webkit finals which land ~1–3s late anyway. Net wash in practice,
  but the text is not incremental — no word-by-word finals.
- **Onset clipping:** the recorder starts when the level gate opens, so the
  first ~100ms of a word can be missed (no pre-roll buffer). A ring-buffer
  pre-roll via AudioWorklet would fix this if it matters in testing.
- **Cost:** gpt-4o-mini-transcribe ≈ $0.003/min of audio. Segments are
  level-gated (silence is never sent), so a 1-hour call with 20 min of actual
  speech ≈ $0.06.
- **Wake detection still needs webkit SR.** Arming happens on SR interims; if
  SR is unavailable the wake word dies with or without this flag. The upgrade
  improves the *transcript*, it does not replace the recognizer's timing role.
- **VAD is level-based**, tuned for the BlackHole loopback cable (clean,
  no room noise). On a real open mic it will over-trigger; lower/raise
  `STT_SEG_START_PCT` accordingly.

## If we want true streaming STT later (the better endgame)

- **OpenAI Realtime transcription** (`gpt-4o-transcribe` over WebSocket):
  word-level partials at ~300ms, server-side VAD. Needs a WS proxy in
  server.py (browser can't hold the API key) — a bigger lift than this
  chunked path but strictly better latency. Same existing key.
- **Deepgram** (`nova-3`): the best latency/accuracy/price for streaming
  (~$0.0043/min, <300ms partials, keyword boosting for domain terms). Would
  need a `DEEPGRAM_API_KEY` and a small WS proxy; the mini-VAD and
  `handleFinalChunk` plumbing built here is reusable as-is.
