"""The run store (ADR-0015 D3): atomic writes, safe ids and location, locks, corrupt files, resumable state."""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from ai_resources.orchestrator import state
from ai_resources.orchestrator.state import RunStore, StateError

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration


def run(rid="r-1", **kw):
    return {"id": rid, "status": "running", "events": [], **kw}


def test_save_and_load_round_trip_with_private_mode_and_no_temp_files(tmp_path):
    s = RunStore(tmp_path)
    s.save(run(goal="g"))
    assert s.load("r-1")["goal"] == "g" and s.exists("r-1")
    mode = stat.S_IMODE(os.stat(s.state_path("r-1")).st_mode)
    assert mode == 0o600
    assert [p.name for p in s.dir("r-1").iterdir() if p.name.startswith(".state")] == []


def test_an_interrupted_save_leaves_the_previous_file_whole(tmp_path, monkeypatch):
    s = RunStore(tmp_path)
    s.save(run(goal="old"))
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        s.save(run(goal="new"))
    monkeypatch.setattr(os, "replace", real_replace)
    assert s.load("r-1")["goal"] == "old"
    assert [p.name for p in s.dir("r-1").iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "-x", ".hidden", "a" * 200, "x..y", "r 1"])
def test_unsafe_run_ids_are_refused(tmp_path, bad):
    with pytest.raises(StateError):
        RunStore(tmp_path).dir(bad)


def test_the_runs_directory_never_lives_under_a_volatile_root(monkeypatch):
    for root in ("/tmp/xdg", "/dev/shm/x", "/var/tmp"):
        monkeypatch.setenv("XDG_STATE_HOME", root)
        with pytest.raises(StateError):
            state.runs_root()
    monkeypatch.setenv("XDG_STATE_HOME", "/home/someone/.local/state")
    assert str(state.runs_root()) == "/home/someone/.local/state/ai-resources/runs"


def test_a_corrupt_or_foreign_state_file_is_an_error_not_a_crash(tmp_path):
    s = RunStore(tmp_path)
    s.save(run())
    s.state_path("r-1").write_text("{ not json")
    with pytest.raises(StateError):
        s.load("r-1")
    s.state_path("r-1").write_text(json.dumps({"id": "other"}))
    with pytest.raises(StateError):
        s.load("r-1")
    with pytest.raises(StateError):
        s.load("never-existed")
    assert s.all() == []


def test_events_are_capped_and_the_ids_list_is_sorted_and_clean(tmp_path):
    s = RunStore(tmp_path)
    s.save(run("b", events=[{"t": i} for i in range(500)]))
    s.save(run("a"))
    assert len(s.load("b")["events"]) == state.MAX_EVENTS and s.load("b")["events"][-1] == {"t": 499}
    (tmp_path / "junk").mkdir()
    assert s.ids() == ["a", "b"]


def test_the_run_lock_serialises_two_writers(tmp_path):
    s = RunStore(tmp_path)
    s.save(run())
    order: list[str] = []
    entered = threading.Event()

    def first():
        with s.locked("r-1"):
            entered.set()
            time.sleep(0.2)
            order.append("first-done")

    def second():
        entered.wait()
        with s.locked("r-1"):
            order.append("second")
    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start(), b.start()
    a.join(), b.join()
    assert order == ["first-done", "second"]


def test_the_handoff_is_written_atomically_beside_the_state(tmp_path):
    s = RunStore(tmp_path)
    s.save(run())
    s.write_handoff("r-1", "# hi\n")
    assert s.handoff_path("r-1").read_text() == "# hi\n"
    assert stat.S_IMODE(os.stat(s.handoff_path("r-1")).st_mode) == 0o600


def test_run_ids_are_valid_unique_and_stamped():
    a, b = state.new_run_id("Feature Implementation!", 0), state.new_run_id("Feature Implementation!", 0)
    assert a != b and a.startswith("19700101-000000-feature-implementation-") and state.RUN_ID_RE.match(a)


def test_a_state_file_holds_no_prompt_body_or_reply_text(tmp_path):
    from orchestrator_fakes import OK, dispatches, finish, make
    e, wf, _ = make(tmp_path)
    r = e.start(wf, "g", "/w")
    [d] = dispatches(e.next(r["id"]))
    finish(e, r["id"], d, OK + "Plan_ref: p.md\nSECRET-REPLY-BODY-123\n")
    text = e.store.state_path(r["id"]).read_text()
    assert "SECRET-REPLY-BODY-123" not in text and d["prompt"] not in text
