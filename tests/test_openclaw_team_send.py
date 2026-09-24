"""The team-narration delivery queue: pacing, order, retries on a 429, and nothing lost silently.

Background (2026-09-24): a forum group and all its topics share one Telegram budget. The watchers, the
hook and the gateway's own streaming drafts fought for it; the gateway answered a 429 by waiting (up to
76 s), the watcher's 30 s timeout killed the call, and every failure was thrown away. These tests run
the real script against a stub `openclaw` driven by a plan file: nothing is ever sent. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SEND = REPO / "scripts" / "openclaw" / "openclaw-team-send.py"
CHAT = "-1001"

STUB = """#!{python}
import json, os, sys, time
calls = os.environ["STUB_CALLS"]
plan = json.load(open(os.environ["STUB_PLAN"])) if os.path.exists(os.environ["STUB_PLAN"]) else []
n = sum(1 for _ in open(calls)) if os.path.exists(calls) else 0
step = plan[n] if n < len(plan) else {{}}
open(calls, "a").write(json.dumps({{"t": time.time(), "argv": sys.argv[1:]}}) + "\\n")
time.sleep(step.get("sleep", 0))
print(step.get("out", "Message ID: 100"))
sys.exit(step.get("rc", 0))
"""


@pytest.fixture
def rig(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("openclaw_team_send_under_test", SEND)
    bus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bus)
    stub = tmp_path / "openclaw"
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("STUB_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("STUB_PLAN", str(tmp_path / "plan.json"))
    monkeypatch.setattr(bus, "OPENCLAW", str(stub))
    monkeypatch.setattr(bus, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(bus, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(bus, "INTERVAL", 0.3)
    monkeypatch.setattr(bus, "BACKOFF", (0.1, 0.1, 0.1, 0.1))

    class Rig:
        pass

    r = Rig()
    r.bus, r.tmp = bus, tmp_path

    def plan(*steps):
        (tmp_path / "plan.json").write_text(json.dumps(list(steps)), encoding="utf-8")

    def calls():
        try:
            return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        except FileNotFoundError:
            return []

    def events():
        try:
            return [json.loads(x) for x in (tmp_path / "logs" / "team-outbox.log").read_text(encoding="utf-8").splitlines()]
        except FileNotFoundError:
            return []

    def dead():
        d = tmp_path / "logs" / "team-outbox-dead"
        return sorted(d.iterdir()) if d.exists() else []

    r.plan, r.calls, r.events, r.dead = plan, calls, events, dead
    r.env = {**os.environ, "OPENCLAW_TEAM_HOOK_BIN": str(stub), "OPENCLAW_TEAM_HOOK_STATE": str(tmp_path / "state"),
             "OPENCLAW_TEAM_SEND_LOG_DIR": str(tmp_path / "logs"), "OPENCLAW_TEAM_SEND_INTERVAL": "0.3",
             "STUB_CALLS": str(tmp_path / "calls.jsonl"), "STUB_PLAN": str(tmp_path / "plan.json")}
    return r


def _texts(r):
    return [c["argv"][c["argv"].index("--message") + 1] for c in r.calls()]


# --- the happy path ----------------------------------------------------------------------------------------------------

def test_a_send_returns_the_message_id_and_leaves_no_trace(rig):
    ok, mid = rig.bus.deliver("send", CHAT, "8", text="hello")
    assert (ok, mid) == (True, "100")
    assert rig.events() == [] and rig.dead() == []
    argv = rig.calls()[0]["argv"]
    assert argv[:2] == ["message", "send"] and argv[argv.index("--thread-id") + 1] == "8"


def test_an_edit_carries_the_message_id(rig):
    ok, _ = rig.bus.deliver("edit", CHAT, "8", text="new", mid="77")
    assert ok
    argv = rig.calls()[0]["argv"]
    assert argv[:2] == ["message", "edit"] and argv[argv.index("--message-id") + 1] == "77"


def test_the_queue_leaves_no_ticket_behind(rig):
    rig.bus.deliver("send", CHAT, "8", text="a")
    assert list((rig.tmp / "state" / "outbox" / "-1001").glob("[0-9]*")) == []


# --- pacing ---------------------------------------------------------------------------------------------------------------

def test_two_calls_to_one_chat_are_spaced(rig):
    rig.bus.deliver("send", CHAT, "8", text="a")
    rig.bus.deliver("send", CHAT, "9", text="b")
    first, second = (c["t"] for c in rig.calls())
    assert second - first >= 0.28


def test_different_chats_do_not_wait_for_each_other(rig):
    rig.bus.deliver("send", "-1001", "8", text="a")
    rig.bus.deliver("send", "-1002", "8", text="b")
    first, second = (c["t"] for c in rig.calls())
    assert second - first < 0.25


def test_a_slow_call_widens_the_spacing_that_follows(rig, monkeypatch):
    monkeypatch.setattr(rig.bus, "SLOW_CALL", 0.2)
    monkeypatch.setattr(rig.bus, "SLOW_EXTRA", 0.6)
    rig.plan({"sleep": 0.4})
    rig.bus.deliver("send", CHAT, "8", text="a")
    started = time.time()
    rig.bus.deliver("send", CHAT, "8", text="b")
    assert rig.calls()[1]["t"] - started >= 0.55
    assert any(e["event"] == "slow" for e in rig.events())


# --- a 429 -----------------------------------------------------------------------------------------------------------------

def test_a_429_waits_the_time_telegram_asked_and_then_succeeds(rig):
    rig.plan({"rc": 1, "out": "Call to 'editMessageText' failed! (429: Too Many Requests: retry after 1)"})
    started = time.time()
    ok, _ = rig.bus.deliver("edit", CHAT, "8", text="x", mid="5")
    assert ok and len(rig.calls()) == 2
    assert rig.calls()[1]["t"] - started >= 1.9, "retry after 1 means wait 1 s plus a second of margin"
    rate = [e for e in rig.events() if e["event"] == "rate"]
    assert len(rate) == 1 and rate[0]["retry_after"] == 1


def test_a_429_is_never_silent(rig):
    rig.plan({"rc": 1, "out": "429 Too Many Requests: retry after 0"})
    rig.bus.deliver("send", CHAT, "8", text="x")
    assert [e["event"] for e in rig.events()] == ["rate"]


def test_a_429_that_names_no_time_is_treated_as_thirty_seconds(rig):
    assert rig.bus.classify(1, "Too Many Requests") == ("rate", 30)
    assert rig.bus.classify(1, "HTTP 429") == ("rate", 30)
    assert rig.bus.classify(1, '{"retry_after": 12}') == ("rate", 12)


def test_a_429_that_never_clears_keeps_a_send_as_a_dead_letter(rig, monkeypatch):
    monkeypatch.setattr(rig.bus, "RATE_HITS", 2)
    rig.plan(*[{"rc": 1, "out": "429: retry after 0"}] * 5)
    ok, _ = rig.bus.deliver("send", CHAT, "8", text="milestone")
    assert not ok and len(rig.calls()) == 2
    (letter,) = rig.dead()
    assert json.loads(letter.read_text(encoding="utf-8"))["text"] == "milestone"
    assert "failed" in [e["event"] for e in rig.events()]


# --- other failures ----------------------------------------------------------------------------------------------------

def test_a_transient_failure_is_retried_with_back_off(rig):
    rig.plan({"rc": 1, "out": "ECONNREFUSED: the gateway is restarting"}, {"rc": 1, "out": "socket hang up"})
    ok, mid = rig.bus.deliver("send", CHAT, "8", text="x")
    assert ok and mid == "100" and len(rig.calls()) == 3
    assert [e["event"] for e in rig.events()] == ["retry", "retry"]
    assert rig.dead() == []


def test_a_send_that_keeps_failing_is_kept_not_lost(rig):
    rig.plan(*[{"rc": 1, "out": "gateway unreachable"}] * 9)
    ok, _ = rig.bus.deliver("send", CHAT, "8", text="do not lose me")
    assert not ok and len(rig.calls()) == rig.bus.ATTEMPTS
    (letter,) = rig.dead()
    data = json.loads(letter.read_text(encoding="utf-8"))
    assert data["text"] == "do not lose me" and data["thread"] == "8" and "unreachable" in data["error"]


def test_a_failed_edit_is_logged_but_not_kept(rig):
    """The member's next edit supersedes it; keeping stale edits would only replay noise."""
    rig.plan(*[{"rc": 1, "out": "gateway unreachable"}] * 9)
    ok, _ = rig.bus.deliver("edit", CHAT, "8", text="x", mid="5")
    assert not ok and rig.dead() == []
    assert "failed" in [e["event"] for e in rig.events()]


def test_a_permanent_error_stops_at_once(rig):
    rig.plan({"rc": 1, "out": "Bad Request: message to edit not found"})
    ok, _ = rig.bus.deliver("send", CHAT, "8", text="x")
    assert not ok and len(rig.calls()) == 1
    assert len(rig.dead()) == 1 and rig.events()[0]["event"] == "permanent"


def test_message_is_not_modified_counts_as_delivered(rig):
    rig.plan({"rc": 1, "out": "Bad Request: message is not modified"})
    ok, _ = rig.bus.deliver("edit", CHAT, "8", text="same", mid="5")
    assert ok and rig.dead() == [] and rig.events() == []


def test_a_call_that_hangs_is_cut_and_retried_with_the_long_timeout_default(rig, monkeypatch):
    assert rig.bus.CALL_TIMEOUT >= 100, "the gateway waited up to 76 s on a 429; the old 30 s cut the call and lost the edit"
    monkeypatch.setattr(rig.bus, "CALL_TIMEOUT", 0.4)
    rig.plan({"sleep": 2})
    ok, _ = rig.bus.deliver("send", CHAT, "8", text="x")
    assert ok and len(rig.calls()) == 2
    assert rig.events()[0]["event"] == "retry"


# --- latest wins ----------------------------------------------------------------------------------------------------------

def test_an_edit_is_rendered_when_its_turn_comes_not_when_it_queued(rig):
    rig.bus.deliver("send", CHAT, "8", text="first")   # opens the spacing window
    rendered = []

    def state_now():
        rendered.append(time.time())
        return f"state at {len(rendered)}"

    rig.bus.deliver("edit", CHAT, "8", mid="5", text_fn=state_now)
    assert len(rendered) == 1
    assert rendered[0] >= rig.calls()[0]["t"] + 0.28, "rendered after the pacing wait, so it is the fresh state"
    assert _texts(rig)[-1] == "state at 1"


def test_a_retried_edit_is_rendered_again(rig):
    rig.plan({"rc": 1, "out": "429: retry after 0"})
    n = []
    rig.bus.deliver("edit", CHAT, "8", mid="5", text_fn=lambda: n.append(1) or f"v{len(n)}")
    assert _texts(rig) == ["v1", "v2"]


# --- order and concurrency: real processes ---------------------------------------------------------------------------

def _spawn(r, text, thread="8"):
    return subprocess.Popen([sys.executable, str(SEND), "send", "--channel", "telegram", "--target", CHAT,
                             "--thread-id", thread, "--message", text], env=r.env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_concurrent_senders_are_delivered_in_arrival_order_and_spaced(rig):
    procs = []
    for i in range(5):
        procs.append(_spawn(rig, f"m{i}"))
        time.sleep(0.08)  # arrival order
    for p in procs:
        p.communicate(timeout=30)
    assert _texts(rig) == [f"m{i}" for i in range(5)]
    stamps = [c["t"] for c in rig.calls()]
    assert all(b - a >= 0.25 for a, b in zip(stamps, stamps[1:])), "calls to one chat must be paced"


def test_the_command_line_prints_the_message_id_and_exits_zero(rig):
    p = _spawn(rig, "hi")
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 0 and out.strip() == "Message ID: 100"


def test_the_command_line_exits_zero_even_when_delivery_fails(rig):
    rig.plan(*[{"rc": 1, "out": "chat not found"}] * 3)
    p = _spawn(rig, "x")
    p.communicate(timeout=30)
    assert p.returncode == 0 and len(rig.dead()) == 1


def test_a_ticket_of_a_dead_process_does_not_block_the_queue(rig):
    d = rig.tmp / "state" / "outbox" / "-1001"
    d.mkdir(parents=True)
    ghost = subprocess.Popen([sys.executable, "-c", "pass"])
    ghost.wait()
    (d / f"{1:020d}-{ghost.pid}").write_text("", encoding="utf-8")
    started = time.time()
    p = _spawn(rig, "x")
    p.communicate(timeout=30)
    assert len(rig.calls()) == 1 and time.time() - started < 10
    assert not (d / f"{1:020d}-{ghost.pid}").exists()


def test_a_live_ticket_ahead_holds_the_queue_until_it_is_released(rig):
    d = rig.tmp / "state" / "outbox" / "-1001"
    d.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ticket = d / f"{1:020d}-{holder.pid}"
    ticket.write_text("", encoding="utf-8")
    try:
        p = _spawn(rig, "waits")
        time.sleep(1.0)
        assert p.poll() is None and rig.calls() == [], "it must wait behind a live ticket"
        ticket.unlink()
        p.communicate(timeout=30)
        assert _texts(rig) == ["waits"]
    finally:
        holder.kill()
        holder.wait()


def test_the_wait_for_a_turn_is_bounded(rig):
    d = rig.tmp / "state" / "outbox" / "-1001"
    d.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (d / f"{1:020d}-{holder.pid}").write_text("", encoding="utf-8")
    try:
        rig.env["OPENCLAW_TEAM_SEND_QUEUE_WAIT"] = "1"
        p = _spawn(rig, "not lost")
        p.communicate(timeout=30)
        assert _texts(rig) == ["not lost"]
        assert any(e["event"] == "queue_timeout" for e in rig.events())
    finally:
        holder.kill()
        holder.wait()


# --- replaying what could not be delivered ------------------------------------------------------------------------------

def _dead_letter(rig, text, age=0.0, chat=CHAT):
    d = rig.tmp / "logs" / "team-outbox-dead"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.time_ns():020d}-1.json"
    path.write_text(json.dumps({"kind": "send", "chat": chat, "thread": "8", "text": text, "mid": None,
                                "error": "x", "ts": time.time() - age}), encoding="utf-8")
    return path


def test_a_recent_dead_letter_is_replayed_after_the_next_success(rig):
    letter = _dead_letter(rig, "late but not lost")
    rig.bus.deliver("send", CHAT, "8", text="now")
    assert _texts(rig) == ["now", "late but not lost"]
    assert not letter.exists()
    assert "replayed" in [e["event"] for e in rig.events()]


def test_an_old_dead_letter_is_not_replayed_automatically(rig):
    letter = _dead_letter(rig, "stale", age=3600)
    rig.bus.deliver("send", CHAT, "8", text="now")
    assert _texts(rig) == ["now"] and letter.exists()


def test_a_dead_letter_of_another_chat_stays_put(rig):
    letter = _dead_letter(rig, "elsewhere", chat="-2002")
    rig.bus.deliver("send", CHAT, "8", text="now")
    assert _texts(rig) == ["now"] and letter.exists()


def test_replay_all_delivers_everything_including_the_old_ones(rig):
    _dead_letter(rig, "one", age=7200)
    _dead_letter(rig, "two", age=10)
    assert rig.bus.replay_all() == 2
    assert _texts(rig) == ["one", "two"] and rig.dead() == []


def test_replay_all_keeps_what_still_fails(rig):
    _dead_letter(rig, "still down")
    rig.plan(*[{"rc": 1, "out": "chat not found"}] * 3)
    assert rig.bus.replay_all() == 0
    assert len(rig.dead()) == 1


# --- the log ------------------------------------------------------------------------------------------------------------------------

def test_the_log_rotates(rig, monkeypatch):
    monkeypatch.setattr(rig.bus, "MAX_LOG", 200)
    for i in range(30):
        rig.bus.log("rate", n=i, pad="x" * 20)
    logs = rig.tmp / "logs"
    assert (logs / "team-outbox.log.1").exists() and (logs / "team-outbox.log").stat().st_size < 400


def test_logging_never_raises_when_the_directory_is_unwritable(rig, monkeypatch):
    monkeypatch.setattr(rig.bus, "LOG_DIR", "/proc/definitely/not/writable")
    rig.bus.log("rate", n=1)
