"""Team narration that survives long runs: the rate window, live-PID watcher markers, workflow
transcripts and the watcher lifecycle.

Background (2026-09-24, elinvo topic 67): a workflow member kept working for hours and nothing
reached Telegram. Causes, all covered here: a lifetime message cap that silenced long sessions;
a watcher that closed after 90 s of transcript silence while its marker kept claiming it was
alive; markers that counted against MAX_WATCHERS long after their process was gone; and workflow
members whose transcript lives under subagents/workflows/<id>/, which the hook never looked at.

The hook tests replace subprocess.Popen with a recorder, so nothing is ever sent. The watcher
tests run the real script as a subprocess against a throwaway state directory and a stub
`openclaw` that records its arguments. Run with:  pytest tests/ -q
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

_REAL_POPEN = subprocess.Popen  # the hook fixture replaces subprocess.Popen for the whole process

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_team_progress.py"
WATCH = REPO / "scripts" / "openclaw" / "openclaw-team-watch.py"
TARGET = ("-1001", "8")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stand_in(aid, tag="openclaw-team-watch.py"):
    """An idle process whose command line looks like a member's watcher."""
    return _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(120)", tag, "--agent-id", aid],
                       start_new_session=True)


class _Popens:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.procs: list = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv and argv[0] == "python3" and "--agent-id" in argv:
            proc = _stand_in(argv[argv.index("--agent-id") + 1])
            self.procs.append(proc)
            return proc
        return type("Proc", (), {"pid": 0})()

    def sends(self):
        return [c[c.index("--message") + 1] for c in self.calls if "send" in c and "--message" in c]

    def watchers(self):
        return [c for c in self.calls if c and c[0] == "python3"]

    def cleanup(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()


@pytest.fixture
def hook(tmp_path, monkeypatch, request):
    mod = _load(HOOK, "openclaw_team_progress_resilience")
    popen = _Popens()
    request.addfinalizer(popen.cleanup)
    watcher = tmp_path / "openclaw-team-watch.py"
    watcher.write_text("# stub\n", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mod, "LOG_PAYLOADS", str(tmp_path / "logs" / "team-hook.jsonl"))
    monkeypatch.setattr(mod, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mod, "OPENCLAW_JSON", str(tmp_path / "missing.json"))
    # KIT_HOST_ENV must be isolated too, or the hook reads the real
    # ~/.openclaw/kit-host.env and the assertions below depend on the operator's own
    # OPENCLAW_NARRATION_LANG: on a host set to `es` the rate notice comes out in Spanish and
    # every English-string assertion fails. Pointing it at a missing file pins the `en` default.
    monkeypatch.setattr(mod, "KIT_HOST_ENV", str(tmp_path / "missing-kit-host.env"))
    monkeypatch.setattr(mod, "watcher_path", lambda: str(watcher))
    monkeypatch.setattr(mod, "openclaw_bin", lambda: "/bin/openclaw")
    monkeypatch.setattr(mod, "sender_path", lambda: "")
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    mod.popen, mod.tmp = popen, tmp_path
    return mod


def _member(tmp_path, aid, wf=None, session="S1"):
    """A PreToolUse payload from inside a member, plus its transcript on disk."""
    transcript = tmp_path / "t" / f"{session}.jsonl"
    sub = tmp_path / "t" / session / "subagents" / ("workflows/" + wf if wf else "")
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{aid}.jsonl").write_text("", encoding="utf-8")
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_id": aid, "agent_type": "implementer",
            "cwd": str(tmp_path), "session_id": session, "transcript_path": str(transcript),
            "tool_input": {"command": "ls"}}


# --- C: the rate window replaces the lifetime cap -----------------------------------------------------

def test_non_milestone_messages_are_held_back_with_one_notice_per_window(hook):
    for i in range(hook.MESSAGES_PER_WINDOW + 15):
        hook.publish(TARGET, f"edit {i}", "S1")
    sends = hook.popen.sends()
    assert sum(t.startswith("edit ") for t in sends) == hook.MESSAGES_PER_WINDOW
    assert len([t for t in sends if "keep the pace" in t]) == 1


def test_milestones_bypass_a_full_window(hook):
    for i in range(hook.MESSAGES_PER_WINDOW + 5):
        hook.publish(TARGET, f"edit {i}", "S1")
    before = len(hook.popen.sends())
    for text in ("request", "team start", "member done", "workflow", "turn done"):
        hook.publish(TARGET, text, "S1", milestone=True)
    assert hook.popen.sends()[before:] == ["request", "team start", "member done", "workflow", "turn done"]


def test_a_session_already_over_the_old_cap_still_gets_its_milestones(hook):
    """The old hook had counted 115 messages for the elinvo session, so it dropped everything."""
    os.makedirs(hook.STATE_DIR)
    pathlib.Path(hook.STATE_DIR, "ses-S1").write_text("115", encoding="utf-8")
    hook.publish(TARGET, "member start", "S1", milestone=True)
    hook.publish(TARGET, "an edit", "S1")
    assert hook.popen.sends() == ["member start", "an edit"]


def test_the_window_recovers_on_its_own_and_the_notice_can_repeat(hook, monkeypatch):
    for i in range(hook.MESSAGES_PER_WINDOW + 3):
        hook.publish(TARGET, f"edit {i}", "S1")
    later = time.time() + hook.WINDOW_SECONDS + 5
    monkeypatch.setattr(hook.time, "time", lambda: later)
    hook.popen.calls.clear()
    hook.publish(TARGET, "after the window", "S1")
    assert hook.popen.sends() == ["after the window"]
    for i in range(hook.MESSAGES_PER_WINDOW + 3):
        hook.publish(TARGET, f"again {i}", "S1")
    assert len([t for t in hook.popen.sends() if "keep the pace" in t]) == 1


def test_sessions_do_not_share_a_window(hook):
    for i in range(hook.MESSAGES_PER_WINDOW + 3):
        hook.publish(TARGET, f"edit {i}", "S1")
    hook.popen.calls.clear()
    hook.publish(TARGET, "other session", "S2")
    assert hook.popen.sends() == ["other session"]


# --- A: markers reflect reality ------------------------------------------------------------------------------

def _dead_pid():
    proc = _REAL_POPEN([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _mark(hook, aid, content):
    os.makedirs(hook.STATE_DIR, exist_ok=True)
    pathlib.Path(hook.STATE_DIR, f"watch-{aid}").write_text(str(content), encoding="utf-8")


def test_the_marker_holds_the_watcher_pid(hook):
    assert hook.launch_watcher(_member(hook.tmp, "m1"), TARGET, "implementer") is True
    pid = pathlib.Path(hook.STATE_DIR, "watch-m1").read_text(encoding="utf-8")
    assert pid.isdigit() and int(pid) == hook.popen.procs[0].pid


def test_a_live_watcher_is_left_alone(hook):
    p = _member(hook.tmp, "m1")
    for _ in range(4):
        assert hook.launch_watcher(p, TARGET, "implementer") is True
    assert len(hook.popen.watchers()) == 1


def test_a_member_whose_watcher_died_gets_a_new_one_on_its_next_tool_call(hook):
    p = _member(hook.tmp, "m1")
    _mark(hook, "m1", _dead_pid())
    assert hook.launch_watcher(p, TARGET, "implementer") is True
    assert len(hook.popen.watchers()) == 1
    assert pathlib.Path(hook.STATE_DIR, "watch-m1").read_text(encoding="utf-8") == str(hook.popen.procs[0].pid)


def test_dead_markers_are_swept_and_do_not_count_against_the_pool(hook):
    _mark(hook, "dead1", _dead_pid())
    _mark(hook, "dead2", "")        # written by the old hook: nothing behind it
    _mark(hook, "dead3", "1")       # PID 1 is never a watcher
    assert hook.launch_watcher(_member(hook.tmp, "m2"), TARGET, "implementer") is True
    marks = sorted(f for f in os.listdir(hook.STATE_DIR) if f.startswith("watch-"))
    assert marks == ["watch-m2"]


def test_a_legacy_empty_marker_of_a_live_watcher_is_kept(hook):
    """Markers written by the old hook carry no PID; the process list is the only evidence."""
    stand_in = _stand_in("legacy1")
    try:
        _mark(hook, "legacy1", "")
        assert hook.watcher_alive("legacy1") is True
        assert hook.sweep_markers() == 1
        assert pathlib.Path(hook.STATE_DIR, "watch-legacy1").exists()
    finally:
        stand_in.kill()
        stand_in.wait()


def test_the_pool_counts_live_watchers_only_and_a_slot_frees_when_one_dies(hook):
    live = [_stand_in(f"L{i}") for i in range(hook.MAX_WATCHERS)]
    try:
        for i, proc in enumerate(live):
            _mark(hook, f"L{i}", proc.pid)
        p = _member(hook.tmp, "m4")
        assert hook.launch_watcher(p, TARGET, "implementer") is False, "pool of live watchers is full"
        assert not pathlib.Path(hook.STATE_DIR, "watch-m4").exists()
        live[0].kill()
        live[0].wait()
        assert hook.launch_watcher(p, TARGET, "implementer") is True
    finally:
        for proc in live:
            proc.kill()
            proc.wait()


def test_a_pid_reused_by_an_unrelated_process_is_not_a_watcher(hook):
    other = _stand_in("someone-else")  # right shape, wrong member
    try:
        _mark(hook, "m1", other.pid)
        assert hook.watcher_alive("m1") is False
    finally:
        other.kill()
        other.wait()


def test_stale_stop_markers_are_cleaned_up_but_recent_ones_stay(hook):
    os.makedirs(hook.STATE_DIR)
    old, new = pathlib.Path(hook.STATE_DIR, "stop-old"), pathlib.Path(hook.STATE_DIR, "stop-new")
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    hook.sweep_stops()
    assert not old.exists() and new.exists()


def test_a_member_whose_first_tool_is_an_edit_still_gets_a_watcher(hook, monkeypatch):
    p = _member(hook.tmp, "m1")
    p.update({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}})
    hook.launch_watcher(p, TARGET, "implementer")
    assert len(hook.popen.watchers()) == 1


# --- workflow members ----------------------------------------------------------------------------------------

def test_a_workflow_members_transcript_is_found_under_subagents_workflows(hook):
    p = _member(hook.tmp, "wfm", wf="wf_abc-1")
    expected = hook.tmp / "t" / "S1" / "subagents" / "workflows" / "wf_abc-1" / "agent-wfm.jsonl"
    assert hook.member_transcript(p) == str(expected)


def test_a_direct_members_transcript_keeps_its_own_layout(hook):
    p = _member(hook.tmp, "dir1")
    assert hook.member_transcript(p).endswith("/S1/subagents/agent-dir1.jsonl")


def test_a_transcript_that_does_not_exist_yet_resolves_to_the_direct_path(hook):
    p = {"transcript_path": str(hook.tmp / "t" / "S9.jsonl"), "agent_id": "later"}
    assert hook.member_transcript(p).endswith("/S9/subagents/agent-later.jsonl")


def test_the_watcher_argv_carries_the_workflow_transcript_and_the_claude_pid(hook):
    hook.launch_watcher(_member(hook.tmp, "wfm", wf="wf_abc-1"), TARGET, "implementer")
    (argv,) = hook.popen.watchers()
    flags = dict(zip(argv[2::2], argv[3::2]))
    assert flags["--transcript"].endswith("/subagents/workflows/wf_abc-1/agent-wfm.jsonl")
    assert flags["--claude-pid"].isdigit()


# --- D: the payload log rotates instead of going silent --------------------------------------------------------

def test_the_payload_log_rotates_at_the_cap_instead_of_stopping(hook):
    log = pathlib.Path(hook.LOG_PAYLOADS)
    log.parent.mkdir(parents=True)
    log.write_text("x" * (hook.MAX_LOG + 10), encoding="utf-8")
    hook.log_payload({"hook_event_name": "Stop", "cwd": "/tmp"})
    assert pathlib.Path(str(log) + ".1").stat().st_size == hook.MAX_LOG + 10
    assert 0 < log.stat().st_size < 1000


# --- the watcher: pure logic ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def watch_mod():
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("OPENCLAW_TEAM_")}
    try:
        return _load(WATCH, "openclaw_team_watch_defaults")
    finally:
        os.environ.update(saved)


def test_ninety_seconds_of_silence_no_longer_closes_the_watcher(watch_mod):
    now = 100000.0
    close = watch_mod.close_reason
    assert close(now, now - 500, now - 91, False, None) is None
    assert close(now, now - 500, now - 200, False, True) is None
    assert close(now, now - 4000, now - 1800, False, True) is None, "parent alive: 30 min of silence is fine"


def test_the_watcher_closes_for_the_documented_reasons_only(watch_mod):
    now = 100000.0
    close = watch_mod.close_reason
    assert close(now, now - 500, now - 1, True, True) == "stop"
    assert close(now, now - 500, now - 10, False, False) == "parent"
    assert close(now, now - 4000, now - 1600, False, None) == "silence", "parent unknown: 25 min of silence"
    assert close(now, now - 20000, now - 7300, False, True) == "silence", "hard ceiling with the parent alive"
    assert close(now, now - 22000, now - 1, False, True) == "lifetime"


def test_parent_alive_tells_alive_dead_and_unknown_apart(watch_mod, tmp_path):
    assert watch_mod.parent_alive(0) is None
    assert watch_mod.parent_alive(_dead_pid()) is False
    assert watch_mod.parent_alive(os.getpid()) is False, "a live process that is not claude is not the parent"
    claude = tmp_path / "claude"
    claude.symlink_to(sys.executable)
    proc = _REAL_POPEN([str(claude), "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.3)
        assert watch_mod.parent_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


def test_the_watcher_resolves_a_workflow_transcript(watch_mod, tmp_path):
    direct = tmp_path / "subagents" / "agent-w1.jsonl"
    assert watch_mod.resolve_transcript(str(direct), "w1") == str(direct)
    wf = tmp_path / "subagents" / "workflows" / "wf_1" / "agent-w1.jsonl"
    wf.parent.mkdir(parents=True)
    wf.write_text("", encoding="utf-8")
    assert watch_mod.resolve_transcript(str(direct), "w1") == str(wf)


# --- the watcher: the real process ------------------------------------------------------------------------------------

STUB = """#!{python}
import json, os, sys, time
open(os.environ["STUB_CALLS"], "a").write(json.dumps({{"t": time.time(), "argv": sys.argv[1:]}}) + "\\n")
plan_path = os.environ.get("STUB_PLAN", "")
plan = json.load(open(plan_path)) if plan_path and os.path.exists(plan_path) else []
n = sum(1 for _ in open(os.environ["STUB_CALLS"])) - 1
step = plan[n] if n < len(plan) else {{}}
if sys.argv[1:3] == ["message", "send"] and not step.get("rc"):
    print("Message ID: 100")
elif step.get("out"):
    print(step["out"])
sys.exit(step.get("rc", 0))
"""


class _Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.calls = tmp_path / "calls.jsonl"
        self.stub = tmp_path / "openclaw"
        self.stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        self.stub.chmod(0o755)
        self.transcript = tmp_path / "S1" / "subagents" / "agent-w1.jsonl"
        self.env = {**os.environ,
                    "OPENCLAW_TEAM_HOOK_STATE": str(self.state), "OPENCLAW_TEAM_HOOK_BIN": str(self.stub),
                    "STUB_CALLS": str(self.calls), "STUB_PLAN": str(tmp_path / "plan.json"),
                    "OPENCLAW_TEAM_WATCH_INTERVAL": "0.2",
                    "OPENCLAW_TEAM_WATCH_HEARTBEAT": "0.5", "OPENCLAW_TEAM_WATCH_TICK": "0.2",
                    "OPENCLAW_TEAM_WATCH_QUIET": "0.5", "OPENCLAW_TEAM_WATCH_WAIT": "6",
                    # the delivery queue the watcher goes through: no real pacing, and its log in tmp
                    "OPENCLAW_TEAM_SEND_INTERVAL": "0.05", "OPENCLAW_TEAM_SEND_LOG_DIR": str(tmp_path / "logs")}
        self.procs = []

    def write_transcript(self, path=None):
        path = path or self.transcript
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def start(self, *extra):
        proc = _REAL_POPEN([sys.executable, str(WATCH), "--agent-id", "w1", "--role", "implementer",
                            "--transcript", str(self.transcript), "--chat", "-1001", "--thread", "8", *extra],
                           env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def entries(self, kind):
        try:
            rows = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        except FileNotFoundError:
            return []
        return [r["argv"] for r in rows if r["argv"][:2] == ["message", kind]]

    def texts(self, kind):
        return [a[a.index("--message") + 1] for a in self.entries(kind)]

    def wait(self, cond, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        (self.state / "stop-w1").write_text("", encoding="utf-8")


@pytest.fixture
def rig(tmp_path):
    r = _Rig(tmp_path)
    yield r
    for proc in r.procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_quiet_member_keeps_its_live_message_edited_until_it_is_stopped(rig):
    rig.write_transcript()
    proc = rig.start()
    assert rig.wait(lambda: len(rig.texts("edit")) >= 4), "heartbeat edits never came"
    assert proc.poll() is None, "the watcher must outlive the silence"
    assert any("still running" in t and "`Bash`" in t and "since last activity" in t for t in rig.texts("edit"))
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None), "SubagentStop must close it"
    assert "finished" in rig.texts("edit")[-1]
    assert not (rig.state / "stop-w1").exists() and not (rig.state / "mid-w1").exists()


def test_the_watcher_removes_its_own_marker_when_it_exits(rig):
    rig.write_transcript()
    proc = rig.start()
    (rig.state / "watch-w1").write_text(str(proc.pid), encoding="utf-8")
    assert rig.wait(lambda: rig.entries("send"))
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)
    assert not (rig.state / "watch-w1").exists()


def test_a_marker_that_is_not_ours_is_left_alone_on_exit(rig):
    rig.write_transcript()
    proc = rig.start()
    (rig.state / "watch-w1").write_text("424242", encoding="utf-8")
    assert rig.wait(lambda: rig.entries("send"))
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)
    assert (rig.state / "watch-w1").read_text(encoding="utf-8") == "424242"


def test_a_relaunched_watcher_reuses_the_message_instead_of_opening_a_second(rig):
    rig.write_transcript()
    (rig.state / "mid-w1").write_text(f"77|{time.time() - 30}", encoding="utf-8")
    proc = rig.start()
    assert rig.wait(lambda: rig.entries("edit"))
    assert rig.entries("send") == []
    assert all(a[a.index("--message-id") + 1] == "77" for a in rig.entries("edit"))
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)


def test_the_first_message_is_recorded_so_a_relaunch_can_find_it(rig):
    rig.write_transcript()
    rig.start()
    assert rig.wait(lambda: (rig.state / "mid-w1").exists())
    assert (rig.state / "mid-w1").read_text(encoding="utf-8").startswith("100|")


def test_a_watcher_that_starts_before_a_workflow_transcript_exists_waits_and_finds_it(rig):
    proc = rig.start()
    time.sleep(0.8)
    assert rig.entries("send") == [], "nothing to mirror yet"
    rig.write_transcript(rig.tmp / "S1" / "subagents" / "workflows" / "wf_9" / "agent-w1.jsonl")
    assert rig.wait(lambda: rig.entries("send")), "the workflow transcript was never found"
    assert rig.wait(lambda: any("Bash" in t for t in rig.texts("edit")))
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)


def test_the_watcher_gives_up_when_the_transcript_never_appears(rig):
    rig.env["OPENCLAW_TEAM_WATCH_WAIT"] = "1"
    proc = rig.start()
    assert rig.wait(lambda: proc.poll() is not None, timeout=6)
    assert rig.entries("send") == []


def test_the_watcher_closes_when_its_parent_claude_is_gone(rig):
    rig.write_transcript()
    proc = rig.start("--claude-pid", str(_dead_pid()))
    assert rig.wait(lambda: proc.poll() is not None), "nobody is left to run the member"
    assert "no signal from the member" in rig.texts("edit")[-1]


# --- delivery goes through the queue (T33) ----------------------------------------------------------------------------

def test_the_hook_sends_through_the_queue_when_it_exists(hook, monkeypatch):
    queue = hook.tmp / "openclaw-team-send.py"
    queue.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(hook, "sender_path", lambda: str(queue))
    hook.publish(TARGET, "hello", "S1", milestone=True)
    (argv,) = hook.popen.calls
    assert argv[:3] == ["python3", str(queue), "send"]
    flags = dict(zip(argv[3::2], argv[4::2]))
    assert flags == {"--channel": "telegram", "--target": "-1001", "--thread-id": "8", "--message": "hello"}


def test_the_hook_calls_the_cli_directly_without_the_queue(hook):
    hook.publish(TARGET, "hello", "S1", milestone=True)
    (argv,) = hook.popen.calls
    assert argv[:3] == ["/bin/openclaw", "message", "send"]


def test_the_queue_resolves_inside_the_kit_before_the_legacy_location(tmp_path, monkeypatch):
    mod = _load(HOOK, "openclaw_team_progress_sender")
    kit = tmp_path / "kit"
    (kit / "scripts" / "openclaw").mkdir(parents=True)
    (kit / "scripts" / "openclaw" / "openclaw-team-send.py").write_text("#", encoding="utf-8")
    monkeypatch.setenv("AGENT_KIT", str(kit))
    assert mod.sender_path() == str(kit / "scripts" / "openclaw" / "openclaw-team-send.py")


def test_the_watcher_still_works_when_the_queue_script_is_missing(tmp_path, monkeypatch):
    """Copied alone, the watcher cannot load its queue and must fall back to the CLI, not crash."""
    lone = tmp_path / "openclaw-team-watch.py"
    lone.write_text(WATCH.read_text(encoding="utf-8"), encoding="utf-8")
    mod = _load(lone, "openclaw_team_watch_lone")
    assert mod.BUS is None
    monkeypatch.setattr(mod, "sh", lambda args, capture=False: "Message ID: 55" if capture else None)
    assert mod.send("-1001", "8", "x") == "55"
    mod.edit("-1001", "8", "55", lambda: "rendered late")  # a callable is resolved on the direct path too


def test_the_watcher_goes_through_the_queue_and_renders_edits_late(tmp_path):
    mod = _load(WATCH, "openclaw_team_watch_queue")
    assert mod.BUS is not None
    seen = []
    mod.BUS = type("Q", (), {"deliver": staticmethod(lambda kind, chat, thread, **kw: seen.append((kind, kw)) or (True, "9"))})
    assert mod.send("-1001", "8", "hello") == "9"
    mod.edit("-1001", "8", "9", lambda: "later")
    kinds = [k for k, _ in seen]
    assert kinds == ["send", "edit"] and callable(seen[1][1]["text_fn"]) and seen[1][1]["text"] is None


def test_a_429_on_the_first_message_no_longer_makes_the_watcher_give_up(rig):
    """The old watcher had a 30 s CLI timeout and no retry: a first send that met a rate limit left the
    member without a live message for good. Now it waits its turn, honours retry-after and delivers."""
    (rig.tmp / "plan.json").write_text(json.dumps([{"rc": 1, "out": "429: Too Many Requests: retry after 0"}]),
                                       encoding="utf-8")
    rig.write_transcript()
    proc = rig.start()
    assert rig.wait(lambda: (rig.state / "mid-w1").exists(), timeout=15), "the first message never got through"
    assert len(rig.entries("send")) == 2, "one refused with a 429, one delivered"
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)
    logged = (rig.tmp / "logs" / "team-outbox.log").read_text(encoding="utf-8")
    assert '"event": "rate"' in logged


# --- watcher language ------------------------------------------------------------------------------------------------------

def test_the_watcher_labels_exist_in_both_languages(watch_mod):
    assert set(watch_mod.LABELS["en"]) == set(watch_mod.LABELS["es"])


def test_the_watcher_renders_spanish_when_asked(watch_mod):
    running = watch_mod.render("implementer", "opus", [], 3, time.time() - 5, None, "Bash", 40, "es")
    assert "trabajando" in running and "herramientas" in running and "sigue trabajando" in running and "40s sin actividad" in running
    done = watch_mod.render("implementer", "opus", [], 3, time.time() - 5, "stop", lang="es")
    assert "terminó" in done
    silent = watch_mod.render("implementer", "opus", [], 3, time.time() - 5, "padre", lang="es")
    assert "sin señal del miembro" in silent


def test_the_watcher_renders_english_by_default_and_for_an_unknown_language(watch_mod):
    for lang in ("en", "fr", None):
        text = watch_mod.render("implementer", "opus", [], 3, time.time() - 5, None, "Bash", 40, lang or "zz")
        assert "working" in text and "tools" in text and "still running" in text


def test_a_spanish_watcher_shows_spanish_end_to_end(rig):
    rig.write_transcript()
    proc = rig.start("--lang", "es")
    assert rig.wait(lambda: any("trabajando" in t for t in rig.texts("edit")), timeout=10)
    rig.stop()
    assert rig.wait(lambda: proc.poll() is not None)
    assert "terminó" in rig.texts("edit")[-1]
