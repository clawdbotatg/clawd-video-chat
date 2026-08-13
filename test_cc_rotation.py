#!/usr/bin/env python3
"""Tests for cc_accounts (the cc-bridge subscription rotation).

No network, no keychain: probing is stubbed with canned snapshots. Run:
    python3 test_cc_rotation.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cc_accounts  # noqa: E402
from cc_accounts import _Router, link_transcript, looks_like_limit  # noqa: E402


def snap(name, worst=10.0, fable=True, creds=True, usage_ok=True, resets=0.0):
    return {"name": name, "cfg": f"/fake/{name}", "creds": creds,
            "usage_ok": usage_ok, "worst": worst, "five_hour": worst,
            "fable": fable, "resets_epoch": resets}


def router_with(*snaps):
    r = _Router()
    r._snaps = {s["name"]: s for s in snaps}
    r._probed_at = time.time()          # cache is fresh → refresh() won't probe
    return r


def test_pick_coolest_fable():
    r = router_with(snap("a", worst=50), snap("b", worst=10), snap("c", worst=30))
    assert r.pick()["name"] == "b"


def test_pick_skips_fable_less_even_when_coolest():
    r = router_with(snap("cool-nofable", worst=5, fable=False),
                    snap("warm-fable", worst=60, fable=True))
    c = r.pick()
    assert c["name"] == "warm-fable" and c["fable_capable"]


def test_fable_less_pool_is_last_resort_with_model_fallback_flag():
    r = router_with(snap("nofable", worst=5, fable=False),
                    snap("fable-hot", worst=99, fable=True))
    c = r.pick()
    # the only cool pool is fable-less → picked, flagged so the bridge drops --model
    assert c["name"] == "nofable" and not c["fable_capable"]


def test_unknown_usage_never_convicts_on_fable():
    r = router_with(snap("mystery", usage_ok=False))
    c = r.pick()
    assert c["name"] == "mystery" and c["fable_capable"]


def test_walled_pool_excluded_then_returns_after_reset():
    r = router_with(snap("a", worst=10), snap("b", worst=20))
    r.note_limit("a")
    assert r.pick()["name"] == "b"
    r._walled["a"] = time.time() - 1        # wall expired
    assert r.pick()["name"] == "a"


def test_note_limit_prefers_exact_reset_epoch():
    # the CLI's rate_limit_event carries resetsAt — that exact time wins over
    # the probe's resets_at and the default wall duration
    exact = time.time() + 7777
    r = router_with(snap("a", worst=10, resets=time.time() + 99999))
    r.note_limit("a", until_epoch=exact)
    assert abs(r._walled["a"] - exact) < 1
    # a stale/zero epoch falls back to the probed reset, then the default
    r2 = router_with(snap("b", worst=10, resets=0.0))
    r2.note_limit("b", until_epoch=0.0)
    assert r2._walled["b"] > time.time()
    # the MODULE-LEVEL wrapper must accept the same two args cc-bridge passes
    # (the drill caught it lagging the method signature: TypeError mid-hop)
    old = cc_accounts.ROUTER
    cc_accounts.ROUTER = r2
    try:
        cc_accounts.note_limit("b", time.time() + 60)
    finally:
        cc_accounts.ROUTER = old


def test_exclude_param():
    r = router_with(snap("a", worst=10), snap("b", worst=20))
    assert r.pick(exclude=("a",))["name"] == "b"


def test_all_walled_still_returns_something():
    # Every pool benched: the router must NOT strand the call — it un-benches
    # the soonest-to-reset pool rather than returning nothing.
    now = time.time()
    r = router_with(snap("a", worst=100, resets=now + 100),
                    snap("b", worst=100, resets=now + 5000))
    r.note_limit("a")
    r.note_limit("b")
    assert r.pick()["name"] == "a"


def test_no_creds_no_rotation():
    r = router_with(snap("dead", creds=False))
    assert r.pick() is None


def test_manual_overrides():
    r = router_with(snap("liar", worst=5, fable=True))
    cc_accounts.NO_FABLE.add("liar")
    try:
        assert not r.pick()["fable_capable"]
    finally:
        cc_accounts.NO_FABLE.discard("liar")


def test_limit_detection():
    # the CLI banner (short) → limit, whichever channel it arrives on
    banner = "You’ve hit your session limit · resets 2:50pm (America/Denver)"
    assert looks_like_limit(banner, "")
    assert looks_like_limit("", f"claude exited 1: {banner}")
    assert looks_like_limit("Claude AI usage limit reached|1755111000", "")
    # a LONG reply merely quoting the phrase = conversation, not a wall (echo trap)
    chatty = ("So earlier today the funny thing was the screen said " + banner
              + ", and then Austin fixed it live. " + "More context. " * 40)
    assert len(chatty) > cc_accounts.LIMIT_TEXT_MAX
    assert not looks_like_limit(chatty, "")
    # ordinary replies / empty turns don't trip
    assert not looks_like_limit("The deploy is green, want me to merge?", "")
    assert not looks_like_limit("", "")
    assert not looks_like_limit("", "claude exited 1: some transient network error")


def test_link_transcript(tmp_base):
    src_cfg = tmp_base / "acct-src"
    dst_cfg = tmp_base / "acct-dst"
    proj = src_cfg / "projects" / "-Users-x-repo"
    proj.mkdir(parents=True)
    sid = "abc123-def"
    (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n')
    (proj / sid).mkdir()                       # subagents dir
    old_roster = cc_accounts._Router.roster
    old_dir = cc_accounts.ACCOUNTS_DIR
    cc_accounts.ACCOUNTS_DIR = str(tmp_base)
    cc_accounts._Router.roster = lambda self: ["acct-src", "acct-dst"]
    try:
        link_transcript(sid, str(dst_cfg))
    finally:
        cc_accounts._Router.roster = old_roster
        cc_accounts.ACCOUNTS_DIR = old_dir
    linked = dst_cfg / "projects" / "-Users-x-repo" / f"{sid}.jsonl"
    assert linked.is_symlink() and linked.read_text().startswith('{"type":"user"}')
    assert (dst_cfg / "projects" / "-Users-x-repo" / sid).is_symlink()
    # idempotent: second call must not raise or clobber
    link_transcript(sid, str(dst_cfg))


def test_bridge_compiles_and_wires_rotation():
    import py_compile
    py_compile.compile(os.path.join(HERE, "cc-bridge.py"), doraise=True)
    src = Path(HERE, "cc-bridge.py").read_text()
    for needle in ("cc_accounts.ensure_for_turn", "cc_accounts.looks_like_limit",
                   "cc_accounts.note_limit", "cc_accounts.LIMIT_RETRIES",
                   "rate_limit_event",        # structured limit signal handled
                   "is_api_error_message"):   # synthetic banner never streamed
        assert needle in src, f"cc-bridge.py lost its rotation wiring: {needle}"


def main():
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        tests = [(n, f) for n, f in sorted(globals().items())
                 if n.startswith("test_") and callable(f)]
        for name, fn in tests:
            try:
                fn(Path(td)) if "tmp_base" in fn.__code__.co_varnames else fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
