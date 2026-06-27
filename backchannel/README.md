# 📟 clawd-backchannel

> **Folded into `clawd-video-chat` (2026-06-27).** This was a standalone repo
> (`~/clawd/clawd-backchannel`); it now lives at `clawd-video-chat/backchannel/`
> and runs under launchd `com.clawd.backchannel`
> (`deploy/com.clawd.backchannel.plist`). It no longer relays to the openclaw
> gateway directly — `OPENCLAW_WS_URL` in `.env` points at the `claude -p` bridge
> (`cc-bridge.py` `:7861`). The channel-policy persona prompt now lives in this
> repo's `prompts/backchannel.md` (passed by the bridge), not `clawd-md/`. The
> openclaw-protocol handshake below is retained because the bridge speaks it.
> See `../INPUTS-AND-CHANNELS.md` for the full mental model.

A private, mobile-friendly text chat that talks into the **same OpenClaw
session** as the voice front-end (`clawd-video-chat`). Use it to backchannel
clawd during a Zoom call: ask "what's a good question right now?", get a
private answer, then say "do it" — clawd speaks the question on the call.

The split is handled entirely by a `[PRIVATE]…[/PRIVATE]` tagging convention:

- Outgoing messages from this UI are prefixed with `[PRIVATE] ` automatically.
- clawd is taught (via `clawd-md/backchannel.md`) to wrap private replies in
  `[PRIVATE]…[/PRIVATE]`.
- The voice front-end (`clawd-video-chat`) strips `[PRIVATE]…[/PRIVATE]`
  from the TTS stream — so nothing inside ever reaches the call.
- Anything **outside** the tags is spoken on the call as normal.

## Setup

```bash
cd clawd-backchannel
python3 server.py
# it prints, on startup, the exact URLs WITH the required ?k=<token>:
#   laptop browser : http://127.0.0.1:7850/?k=<token>
#   other machine  : http://<laptop-lan-ip>:7850/?k=<token>
```

Requires the `websockets` and `cryptography` Python packages (for the WS
proxy + server-side auth). No build step. The openclaw gateway token is
auto-read from `~/.openclaw/openclaw.json`.

### Auto-start on boot (macOS)

A LaunchAgent at `~/Library/LaunchAgents/com.clawd.backchannel.plist`
(`RunAtLoad` + `KeepAlive`) starts the server at login and respawns it if it
dies. Manage it with:

```bash
launchctl load -w  ~/Library/LaunchAgents/com.clawd.backchannel.plist   # enable
launchctl unload   ~/Library/LaunchAgents/com.clawd.backchannel.plist   # disable
# logs: ~/.cache/clawd/backchannel.log  (the LAN URL + token print here at boot)
```

## Security — shared-token gate (required)

The page (7850) and WS proxy (7851) bind `0.0.0.0`, and the proxy authenticates
**every** connection to the gateway with operator-admin scope. Without a gate,
**anyone on the LAN who reached these ports would get full operator control of
your machine.** So a shared secret `?k=<TOKEN>` is required on the page,
`/config`, and the proxy — anything without it gets `403` / closed. The token:

- is generated once and persisted to `clawd-backchannel/.env` as
  `BACKCHANNEL_TOKEN=…` (stable across restarts/reboots; override via env var);
- must be in the URL you open (`…/?k=<token>`); bookmark that URL on each device;
- is the *only* thing protecting the gateway — treat the tokened URL as a secret,
  and rotate by deleting the `.env` line and restarting (then re-bookmark).

The gateway operator token from `openclaw.json` is **never** sent to LAN/proxied
clients (only the loopback no-proxy fallback receives it). `/health` is the one
un-gated endpoint (returns `{"status":"ok"}`, leaks nothing).

## Gateway handshake (openclaw protocol v4)

The proxy authenticates to the gateway server-side. This MUST match openclaw's
current device-auth scheme or the gateway rejects with `device nonce mismatch` /
`protocol mismatch`:

- Connect with `minProtocol/maxProtocol = 4`.
- The gateway sends an **unsolicited `connect.challenge`** (with a nonce) right
  after the socket opens — wait for it, do **not** send `connect` first.
- Sign the **v3** device payload (openclaw `buildDeviceAuthPayloadV3`):
  `["v3", deviceId, "openclaw-control-ui", "ui", "operator", scopes, signedAtMs,
  token, nonce, platform, deviceFamily].join("|")` — `platform="web"`,
  `deviceFamily=""`, both lowercased and matching `client.{platform,deviceFamily}`.
- Then send a single `connect` signed with the challenge nonce.

If a future openclaw update bumps the protocol again, re-derive the format from
`buildDeviceAuthPayloadV3` in openclaw's `dist/client-*.js`.

## How it connects (and why LAN just works)

The browser does **not** talk to the gateway directly. `server.py` runs a
WebSocket reverse-proxy (page on `7850`, proxy on `7851`, both bound
`0.0.0.0`) that relays each browser ↔ the loopback gateway and does the
gateway handshake **server-side**:

```
browser ──ws──► proxy (7851) ──ws──► gateway (127.0.0.1, loopback only)
```

This sidesteps two things that otherwise break LAN access:

- **Origin:** the gateway only trusts loopback origins. The proxy opens the
  gateway socket with a loopback `Origin`, so a LAN browser is accepted with
  no `gateway.controlUi.allowedOrigins` edit and no gateway restart.
- **Secure context:** the Ed25519 device handshake needs `crypto.subtle`,
  which browsers only expose over HTTPS or `localhost` — not a plain-HTTP LAN
  IP. So the **proxy** holds the Ed25519 identity and signs the handshake;
  the browser needs no WebCrypto and works over plain HTTP anywhere.

Each browser connection gets its own ephemeral device identity, and the
operator token in `openclaw.json` authorizes it — so **no `openclaw devices
approve` step** and no collisions between simultaneous clients.

## LAN access

`server.py` binds `0.0.0.0`, so open **`http://<laptop-lan-ip>:7850/?k=<token>`**
from any device on the same wifi — any browser, plain HTTP, no flags, no certs.
The only per-device step is bookmarking that tokened URL (see **Security**
above — the `?k=` is mandatory). The gateway stays loopback-only the whole time.
(`GATEWAY_ORIGIN` / `PROXY_PORT` / `BACKCHANNEL_TOKEN` can override defaults via env.)

## How the convention plays in practice

You (on phone, privately):
> what's a good question to ask them right now?

clawd (renders in the backchannel only, voice silent):
> [private] given they mentioned the migration just stalled — try asking
> what blocked it most: tooling, ownership, or timing.

You (privately):
> good. ask it.

clawd (private ack, then spoken on the call):
> [private] on it.
> [voice] hey — quick one. what's been blocking the migration most: the
> tooling, the ownership question, or just timing?

## Files

- `server.py` — serves `index.html` + `/config` on `7850`, and runs the
  WebSocket reverse-proxy on `7851` that relays to the loopback gateway and
  performs the protocol-v3 Ed25519 handshake server-side. Binds `0.0.0.0`.
  Text-only (no TTS proxy).
- `index.html` — single-page chat client. Connects to the proxy (URL from
  `/config`), waits for the proxy's `proxy.ready`, then sends app RPCs — no
  client-side crypto. Renders the full session transcript with voice turns and
  private turns visually distinguished. Each in-flight turn shows a live
  `working · Ns` indicator the moment you send, streams clawd's thinking into a
  collapsible 💭 card, and renders each tool call (name, args, ✓/✗ result) as it
  runs — so the UI never just sits silent waiting for the final reply.
- `README.md` — this file.

## Related

- `clawd-video-chat/` — voice front-end. Its `_sanitizeForTts` filters
  `[PRIVATE]…[/PRIVATE]` from the TTS chunker.
- `clawd-web-chat/` — typed front-end that this is structurally cloned from
  (minus tabs/settings/fillers/TTS).
- `clawd-md/backchannel.md` — the persona instructions that teach clawd the
  convention.
