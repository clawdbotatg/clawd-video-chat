#!/usr/bin/env python3
"""cc_accounts.py — subscription account rotation for cc-bridge.

The call brain used to run on ONE account pinned via CLAUDE_CONFIG_DIR in the
installed LaunchAgent plist. When that account's 5h window maxed, clawd bounced
every turn with the CLI's limit banner — live, on air, with no auto-recovery
(2026-08-04 and again 2026-08-13). This module ends that: cc-bridge asks it for
the best account BEFORE each turn, and hops + retries when a turn bounces off a
wall anyway.

Self-contained (stdlib only, no harness import — same discipline as fleet/:
the bridge is a client of claude, not of server.py). The probe mechanism and
the fable heuristic are copied from the harness's proven code paths
(tools/usage_probe.py, server.py _link_transcript / _has_fable_window):

- An account = a config dir under ~/.clawd-accounts/<name> (CLAUDE_CONFIG_DIR
  isolates the credential store). Roster is discovered from disk; CC_ACCOUNTS
  restricts it by name if set.
- Usage comes from Claude's undocumented OAuth usage endpoint — always degrade:
  an unreadable account is skipped, a failed probe leaves the account usable as
  a last resort (unknown never convicts), and an empty roster disables rotation
  entirely (cc-bridge then behaves exactly as before: plist env wins).
- Fable gate: cc-bridge runs --model fable; a plan that carries fable
  advertises a fable-scoped window from 0% used, so a GOOD probe with no such
  window means the plan can't serve the model. Those pools are last-resort
  only, and a turn routed there drops to CC_FALLBACK_MODEL (opus) — a lesser
  model beats silence on a live call.
- Session continuity: claude stores transcripts under
  <config_dir>/projects/<cwd-slug>/<sid>.jsonl, so a --resume under a new
  account can't find a conversation recorded under the old one. Same fix the
  harness uses for handoffs: symlink the transcript (and its subagents dir)
  into the new account's tree before the turn.

Env knobs (all optional):
  CC_ROTATE=0            master off-switch (plist pin behaves as before)
  CC_ACCOUNTS=a,b,c      restrict roster to these names
  CC_ACCOUNTS_DIR        default ~/.clawd-accounts
  CC_HOT=97              worst-window % at/over which a pool is "hot"
  CC_PROBE_TTL=240       seconds a usage probe stays fresh
  CC_LIMIT_RETRIES=2     max account hops per turn after a limit bounce
  CC_FALLBACK_MODEL=opus model used when routed to a fable-less pool
  CC_NO_FABLE / CC_FABLE_OK   manual overrides, comma-separated names
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public client id
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

ROTATE = os.environ.get("CC_ROTATE", "1") != "0"
ACCOUNTS_DIR = os.path.expanduser(os.environ.get("CC_ACCOUNTS_DIR", "~/.clawd-accounts"))
ONLY = {n.strip() for n in os.environ.get("CC_ACCOUNTS", "").split(",") if n.strip()}
HOT = float(os.environ.get("CC_HOT", "97"))
PROBE_TTL = float(os.environ.get("CC_PROBE_TTL", "240"))
LIMIT_RETRIES = int(os.environ.get("CC_LIMIT_RETRIES", "2"))
FALLBACK_MODEL = os.environ.get("CC_FALLBACK_MODEL", "opus")
NO_FABLE = {n.strip() for n in os.environ.get("CC_NO_FABLE", "").split(",") if n.strip()}
FABLE_OK = {n.strip() for n in os.environ.get("CC_FABLE_OK", "").split(",") if n.strip()}
# A bounce with no known reset time walls the pool this long (a 5h window
# resets on its own schedule; a re-probe clears an unfairly walled pool sooner).
WALL_DEFAULT_S = float(os.environ.get("CC_WALL_DEFAULT_S", "1800"))
# Force the FIRST pick after boot onto this account (consumed once). Ops
# override ("make him start on pool X") and the hook the live hop drill uses:
# point the first turn at a walled pool and watch the retry land elsewhere.
FORCE_FIRST = os.environ.get("CC_FORCE_FIRST", "").strip()

# The CLI's limit language, same needles the harness PTY tripwire uses plus the
# -p mode phrasing. Deliberately narrow, and looks_like_limit() adds a length
# guard so a long reply merely QUOTING the banner (e.g. clawd reading this very
# file aloud) never triggers a hop — the echo trap from the 07-16 incident.
LIMIT_RE = re.compile(
    r"you.?ve hit your [a-z0-9 -]{0,24}limit"
    r"|stop and wait for limit to reset"
    r"|ask your admin for more usage"
    r"|usage limit reached", re.I)
LIMIT_TEXT_MAX = 300   # a genuine bounce reply is just the banner


def _keychain_service(config_dir):
    if not config_dir:
        return "Claude Code-credentials"
    h = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{h}"


def _read_credentials(config_dir):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _keychain_service(config_dir),
             "-a", os.environ.get("USER", ""), "-w"],
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip())
    except Exception:
        pass
    path = os.path.join(config_dir, ".credentials.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _http_json(url, headers, body=None):
    req = urllib.request.Request(
        url, headers=headers, data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def _iso_epoch(s):
    """resets_at ISO string → epoch seconds (0 on any parse failure)."""
    if not s:
        return 0.0
    try:
        s2 = re.sub(r"\.\d+", "", str(s)).replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(s2).timestamp()
    except Exception:
        return 0.0


def _digest_usage(usage):
    """Raw usage payload → {worst, five_hour, fable, resets_epoch}. Mirrors
    tools/usage_probe.py: top-level windows + model-scoped `limits` entries."""
    worst, five_hour, fable, resets = 0.0, None, False, 0.0
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        w = usage.get(key)
        util = w.get("utilization") if isinstance(w, dict) else w
        if isinstance(util, (int, float)):
            worst = max(worst, util)
            if key == "five_hour":
                five_hour = util
                resets = _iso_epoch(w.get("resets_at") if isinstance(w, dict) else "")
    for lim in usage.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        pct = lim.get("percent")
        model = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "")
        if isinstance(pct, (int, float)) and model:
            worst = max(worst, pct)
            if "fable" in model.lower():
                fable = True
    return {"worst": worst, "five_hour": five_hour, "fable": fable,
            "resets_epoch": resets}


class _Router:
    def __init__(self):
        self._lock = threading.Lock()
        self._snaps = {}       # name -> snapshot dict
        self._probed_at = 0.0
        self._walled = {}      # name -> until_epoch
        self.current = None    # name of the last account handed out
        self._force_first = FORCE_FIRST

    # -- roster ---------------------------------------------------------------

    def roster(self):
        names = []
        try:
            for n in sorted(os.listdir(ACCOUNTS_DIR)):
                if ONLY and n not in ONLY:
                    continue
                if os.path.isdir(os.path.join(ACCOUNTS_DIR, n)):
                    names.append(n)
        except OSError:
            pass
        return names

    def _cfg(self, name):
        return os.path.join(ACCOUNTS_DIR, name)

    # -- probing --------------------------------------------------------------

    def _probe_one(self, name):
        cfg = self._cfg(name)
        snap = {"name": name, "cfg": cfg, "creds": False, "usage_ok": False,
                "worst": 100.0, "five_hour": None, "fable": False,
                "resets_epoch": 0.0}
        creds = _read_credentials(cfg)
        oauth = (creds or {}).get("claudeAiOauth") or {}
        access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
        if not access:
            return snap
        snap["creds"] = True
        hdrs = {"Authorization": f"Bearer {access}",
                "anthropic-beta": OAUTH_BETA, "Content-Type": "application/json"}
        status, usage = _http_json(USAGE_URL, hdrs)
        if status == 401 and refresh:
            st, tok = _http_json(TOKEN_URL, {"Content-Type": "application/json"},
                                 {"grant_type": "refresh_token",
                                  "refresh_token": refresh,
                                  "client_id": OAUTH_CLIENT_ID})
            if st == 200 and tok and tok.get("access_token"):
                hdrs["Authorization"] = f"Bearer {tok['access_token']}"
                status, usage = _http_json(USAGE_URL, hdrs)
        if status == 200 and isinstance(usage, dict):
            snap["usage_ok"] = True
            snap.update(_digest_usage(usage))
        return snap

    def refresh(self, force=False):
        """(Re)probe the roster, in parallel. Cached PROBE_TTL seconds."""
        with self._lock:
            if not force and time.time() - self._probed_at < PROBE_TTL and self._snaps:
                return dict(self._snaps)
        names = self.roster()
        threads, results = [], {}

        def work(n):
            results[n] = self._probe_one(n)

        for n in names:
            t = threading.Thread(target=work, args=(n,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=20)
        with self._lock:
            # keep prior snapshot for accounts whose probe thread hung
            for n, s in results.items():
                self._snaps[n] = s
            self._probed_at = time.time()
            return dict(self._snaps)

    # -- walls ----------------------------------------------------------------

    def note_limit(self, name, until_epoch=0.0):
        """A turn on this account bounced off the limit wall — bench it until
        the wall lifts. Preference order for the lift time: the CLI's own
        rate_limit_event resetsAt (exact), the probed 5h resets_at, then
        WALL_DEFAULT_S from now."""
        now = time.time()
        until = until_epoch or 0.0
        if until <= now:
            until = (self._snaps.get(name) or {}).get("resets_epoch") or 0.0
        if until <= now:
            until = now + WALL_DEFAULT_S
        with self._lock:
            self._walled[name] = until

    def _is_walled(self, name, now=None):
        now = now or time.time()
        until = self._walled.get(name, 0.0)
        if until and until <= now:
            with self._lock:
                self._walled.pop(name, None)
            return False
        return bool(until)

    # -- selection ------------------------------------------------------------

    def _fable_ok(self, s):
        if s["name"] in NO_FABLE:
            return False
        if s["name"] in FABLE_OK:
            return True
        # unknown usage never convicts (harness _fable_state discipline)
        return s["fable"] or not s["usage_ok"]

    def pick(self, exclude=(), force_probe=False):
        """Best account snapshot, or None (rotation off / roster empty).
        Order: coolest fable-capable → coolest fable-less (caller drops the
        model) → unknown-usage creds-holder → least-bad hot pool. Something is
        ALWAYS returned if any account has credentials — a hot pool that might
        answer beats refusing to try."""
        if not ROTATE:
            return None
        if self._force_first:
            name, self._force_first = self._force_first, ""
            s = self.refresh().get(name)
            if s and s["creds"] and name not in exclude:
                choice = dict(s)
                choice["fable_capable"] = self._fable_ok(s)
                self.current = name
                return choice
        snaps = [s for s in self.refresh(force=force_probe).values()
                 if s["creds"] and s["name"] not in exclude
                 and not self._is_walled(s["name"])]
        if not snaps:
            # every pool excluded/walled: un-bench the one that resets soonest
            benched = [s for s in self.refresh().values()
                       if s["creds"] and s["name"] not in exclude]
            if not benched:
                return None
            snaps = [min(benched, key=lambda s: self._walled.get(s["name"], 0))]
        good = [s for s in snaps if s["usage_ok"]]
        unknown = [s for s in snaps if not s["usage_ok"]]
        fable_cool = [s for s in good if self._fable_ok(s) and s["worst"] < HOT]
        plain_cool = [s for s in good if not self._fable_ok(s) and s["worst"] < HOT]
        for pool in (fable_cool, plain_cool):
            if pool:
                choice = min(pool, key=lambda s: s["worst"])
                break
        else:
            choice = unknown[0] if unknown else min(good, key=lambda s: s["worst"])
        choice = dict(choice)
        # False only on a POSITIVE fable-less reading — tells the bridge to
        # drop --model fable for this turn (lesser model beats a failed turn).
        choice["fable_capable"] = self._fable_ok(choice)
        self.current = choice["name"]
        return choice


ROUTER = _Router()


# -- transcript continuity ----------------------------------------------------

def link_transcript(session_id, dst_cfg):
    """Make --resume under dst_cfg find a transcript recorded under any other
    account (or the default ~/.claude): symlink the .jsonl + its subagents dir
    into dst's projects tree. Same mechanism as the harness's _link_transcript;
    best-effort — a miss just means the conversation restarts fresh."""
    if not session_id or not dst_cfg:
        return
    dst_base = Path(dst_cfg)
    if glob.glob(f"{dst_base}/projects/*/{session_id}.jsonl"):
        return  # already resumable here
    sources = [os.path.join(ACCOUNTS_DIR, n) for n in ROUTER.roster()]
    sources.append(os.path.expanduser("~/.claude"))
    for src_cfg in sources:
        src_base = Path(src_cfg)
        if src_base == dst_base:
            continue
        for hit in glob.glob(f"{src_base}/projects/*/{session_id}.jsonl"):
            src = Path(hit)
            for extra in [src, src.with_suffix("")]:
                if not extra.exists():
                    continue
                dst = dst_base / extra.relative_to(src_base)
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not (dst.exists() or dst.is_symlink()):
                        dst.symlink_to(extra)
                except OSError:
                    pass
            return


# -- the two calls cc-bridge makes --------------------------------------------

def ensure_for_turn(session_id, exclude=(), force_probe=False):
    """Called before each turn spawn. Picks the best account, points
    CLAUDE_CONFIG_DIR at it, and links the conversation's transcript across if
    the account changed. Returns the snapshot (or None = rotation inactive,
    inherited env wins — exactly the old behavior)."""
    choice = pick_account(exclude=exclude, force_probe=force_probe)
    if choice is None:
        return None
    os.environ["CLAUDE_CONFIG_DIR"] = choice["cfg"]
    link_transcript(session_id, choice["cfg"])
    return choice


def pick_account(exclude=(), force_probe=False):
    return ROUTER.pick(exclude=exclude, force_probe=force_probe)


def note_limit(name, until_epoch=0.0):
    ROUTER.note_limit(name, until_epoch)


def looks_like_limit(final_text, err_text):
    """Did this turn bounce off a subscription wall? err (stderr/exit message)
    is checked as-is; the reply text only when SHORT — a genuine bounce reply
    is just the banner, while a long reply that merely quotes the phrase is
    conversation (the echo trap)."""
    if err_text and LIMIT_RE.search(err_text):
        return True
    t = (final_text or "").strip()
    return bool(t) and len(t) <= LIMIT_TEXT_MAX and bool(LIMIT_RE.search(t))


def roster_summary():
    """One-line-per-account state for boot logging / debugging."""
    lines = []
    for s in ROUTER.refresh().values():
        if not s["creds"]:
            lines.append(f"  {s['name']}: no credentials (skipped)")
        elif not s["usage_ok"]:
            lines.append(f"  {s['name']}: usage unknown (last resort)")
        else:
            lines.append(f"  {s['name']}: worst {s['worst']:.0f}% used"
                         f"{' fable' if s['fable'] else ' NO-fable'}"
                         f"{' WALLED' if ROUTER._is_walled(s['name']) else ''}")
    return "\n".join(lines) or "  (no accounts found — rotation inactive)"
