"""The state is written by several processes; none may overwrite what another wrote (2.2b section 4).

* the post-setup watch and `executors` regeneration hold a state object for a long time: they re-load it under
  the lock immediately before saving and write only the fields they own;
* lock files are opened with O_NOFOLLOW (a symlink is refused, never followed);
* `kit-host.env` behind a symlink is written at the target; a managed block keeps the file's mode.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from test_openclaw_config_watch import BIND, HOT, Rig  # noqa: E402
from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state, wizard  # noqa: E402
from ai_resources.setup.cockpits import _shared, claude, openclaw  # noqa: E402


@pytest.fixture
def xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return state.state_path()


def _lock_file() -> pathlib.Path:
    p = state.state_path()
    return p.with_name(p.name + ".lock")


# --- 4a: executors regeneration ---------------------------------------------------------------------------------

def test_regeneration_keeps_the_fields_another_run_saved_while_it_generated(xdg, monkeypatch):
    base = state.SetupState()
    base.openclaw.engine = "claude-code"
    state.save(base)

    def generate(executors, ak_path, mode, tracking=None, **kw):
        other = state.load()                     # a `setup` run saves while the regeneration is writing files
        other.openclaw.host_domain = "kept.example.org"
        other.openclaw.engine = "codex"
        state.save(other)
        tracking.subagent_files_installed = ["planner.md"]
        return ["planner"]

    monkeypatch.setattr(claude, "_generate_subagent_files", generate)
    assert claude.regenerate_agents({}, "multi-model") == ["planner"]
    final = state.load()
    assert final.openclaw.host_domain == "kept.example.org" and final.openclaw.engine == "codex"
    assert final.tracking.subagent_files_installed == ["planner.md"], "its own record is written"


def test_regeneration_does_not_write_the_state_while_another_run_holds_the_lock(xdg, monkeypatch):
    state.save(state.SetupState())
    _lock_file().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_file(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(claude, "_generate_subagent_files",
                        lambda e, a, m, t=None, **k: setattr(t, "subagent_files_installed", ["x.md"]) or ["x"])
    real_update = state.update
    monkeypatch.setattr(state, "update", lambda mut, **kw: real_update(mut, wait_s=0.0, **{
        k: v for k, v in kw.items() if k != "wait_s"}))
    warns: list[str] = []
    monkeypatch.setattr(claude.ui, "warn", lambda m: warns.append(m))
    try:
        assert claude.regenerate_agents({}, "multi-model") == ["x"]
    finally:
        os.close(fd)
    assert state.load().tracking.subagent_files_installed == [] and warns and "busy" in warns[0]


def test_update_inside_a_process_that_holds_the_lock_just_writes(xdg):
    state.save(state.SetupState())
    with state.exclusive():
        assert state.update(lambda fresh: setattr(fresh.openclaw, "host_domain", "in.example.org")) is True
    assert state.load().openclaw.host_domain == "in.example.org"


# --- 4a: the post-setup watch -------------------------------------------------------------------------------------

class DiskRig(Rig):
    """The watch with the DEFAULT load and save (the state file on disk), and another writer that saves while
    the revert runs."""

    on_write = None
    on_stop = None

    def __call__(self, argv, **kw):
        if self.on_stop and argv[:3] == ["systemctl", "--user", "stop"]:
            self.on_stop()
        return super().__call__(argv, **kw)

    def apply_patch(self, patch, *, dry_run=False, replace_paths=None, allow_restart=False, reload_mode=None):
        if not dry_run and self.on_write:
            self.on_write()
        return super().apply_patch(patch, dry_run=dry_run, replace_paths=replace_paths,
                                   allow_restart=allow_restart, reload_mode=reload_mode)

    def run_default(self, **kw):
        kw.setdefault("window", 600)
        kw.setdefault("interval", 30)
        return host.watch_config(self.s.openclaw.config_watch["run_id"], runner=self, sleep=self.sleep,
                                 apply_patch=self.apply_patch, notify=lambda m: self.notes.append(m) or True,
                                 marker=self.marker, reload_mode=lambda: None, out=self.out.append,
                                 drain_interval=1, health_interval=1, **kw)


def _other_run_saves():
    other = state.load()
    other.openclaw.host_domain = "other-run.example.org"
    other.openclaw.engine = "gemini-cli"
    state.save(other)


def test_the_watch_keeps_what_another_run_saved_during_its_revert(xdg, tmp_path):
    r = DiskRig(tmp_path, [HOT])
    state.save(r.s)
    r.on_write = _other_run_saves
    assert r.run_default() == 0
    final = state.load()
    assert final.openclaw.host_domain == "other-run.example.org" and final.openclaw.engine == "gemini-cli"
    cw = final.openclaw.config_watch
    assert cw["done"] is True and cw["result"]["outcome"] == "reverted"


def test_the_watch_does_not_resurrect_a_record_that_a_teardown_removed(xdg, tmp_path):
    r = DiskRig(tmp_path, [HOT])
    state.save(r.s)

    def teardown():
        other = state.load()
        other.openclaw.config_watch = None
        state.save(other)
    r.on_write = teardown
    assert r.run_default() == 0
    assert state.load().openclaw.config_watch is None


def test_the_restore_entries_the_watch_queues_are_merged_with_another_runs_pending_list(xdg, tmp_path):
    r = DiskRig(tmp_path, [BIND], drain_ok=False)        # the drained window never drains: the key stays pending
    state.save(r.s)

    def other_queues():
        other = state.load()
        other.openclaw.host_restart_pending.append(
            {"op": "profile", "path": "agents.defaults.timeoutSeconds", "action": "filled", "source": "setup",
             "at": "t"})
        state.save(other)
    r.on_stop = other_queues
    r.run_default(drain_timeout=3)                       # exits 1: the revert itself failed
    paths = sorted(e["path"] for e in state.load().openclaw.host_restart_pending)
    assert paths == ["agents.defaults.timeoutSeconds", "gateway.bind"]


# --- 4b: lock files are never followed -----------------------------------------------------------------------------

@pytest.fixture
def linked_lock(xdg, tmp_path):
    """The state lock name is a symlink to a file that exists: O_CREAT alone would follow it."""
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    _lock_file().parent.mkdir(parents=True, exist_ok=True)
    _lock_file().symlink_to(real)
    return real


def test_setup_and_apply_pending_refuse_a_symlinked_or_unusable_state_lock(linked_lock, capsys, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(wizard, "_run", lambda args: called.append("setup") or 0)
    assert wizard.run(argparse.Namespace(dry_run=False, yes=False)) == 1
    monkeypatch.setattr(host, "_cmd_apply_pending", lambda args: called.append("apply") or 0)
    assert host.cmd_apply_pending(argparse.Namespace(yes=True, discard_watch_restores=False)) == 1
    assert called == []


def test_a_directory_at_the_state_lock_name_is_refused_for_setup_and_apply_pending(xdg, monkeypatch):
    _lock_file().parent.mkdir(parents=True, exist_ok=True)
    _lock_file().mkdir()
    called: list[str] = []
    monkeypatch.setattr(wizard, "_run", lambda args: called.append("setup") or 0)
    monkeypatch.setattr(host, "_cmd_apply_pending", lambda args: called.append("apply") or 0)
    assert wizard.run(argparse.Namespace(dry_run=False, yes=False)) == 1
    assert host.cmd_apply_pending(argparse.Namespace(yes=True, discard_watch_restores=False)) == 1
    assert called == []


def test_the_restart_lock_is_not_followed_through_a_symlink(tmp_path):
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    host.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    host.LOCK_PATH.symlink_to(real)
    with pytest.raises(host.RestartLockUnusable):
        host.acquire_restart_lock()
    lines: list[str] = []
    stops: list = []
    code = host.drained_window(lambda: True, runner=lambda argv, **kw: stops.append(argv) or (0, ""),
                               marker=host.Marker(tmp_path / "w.off"), sleep=lambda s: None, out=lines.append,
                               openclaw_bin="openclaw", self_cgroup_path=tmp_path / "none")
    assert code == host.EXIT_REFUSED and stops == [] and any("cannot be used" in ln for ln in lines)
    res = host.graceful_restart(runner=lambda argv, **kw: (0, ""), out=lambda m: None, openclaw_bin="openclaw",
                                self_cgroup_path=tmp_path / "none", knobs=dict(host.RESTART_KNOB_DEFAULTS))
    assert res["exit_code"] == host.EXIT_LOCKED
    with pytest.raises(openclaw.RestartDeferred):
        openclaw.restart_gateway(probe=lambda: (0, []), interactive=False)
    assert real.read_text(encoding="utf-8") == "x"


# --- 4b: kit-host.env behind a symlink ----------------------------------------------------------------------------

def test_a_symlinked_kit_host_env_is_written_at_its_target_and_the_link_survives(tmp_path):
    target = tmp_path / "dotfiles" / "kit-host.env"
    target.parent.mkdir()
    target.write_text("# mine\nOPENCLAW_A=1\n", encoding="utf-8")
    os.chmod(target, 0o640)
    link = tmp_path / "kit-host.env"
    link.symlink_to(target)
    assert host.write_host_env({"OPENCLAW_B": "2"}, link) is True
    assert link.is_symlink() and os.readlink(link) == str(target)
    text = target.read_text(encoding="utf-8")
    assert "OPENCLAW_A=1" in text and "OPENCLAW_B=2" in text
    assert oct(target.stat().st_mode & 0o777) == "0o640"


def test_a_kit_host_env_link_into_a_missing_directory_is_left_alone_with_one_line(tmp_path, capsys):
    link = tmp_path / "kit-host.env"
    link.symlink_to(tmp_path / "gone" / "kit-host.env")
    assert host.write_host_env({"OPENCLAW_B": "2"}, link) is False
    out = capsys.readouterr().out
    assert link.is_symlink() and not (tmp_path / "gone").exists()
    assert out.count("\n") == 1 and "left alone" in out


# --- 4c: managed-block backup mode ------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,umask", [(0o600, 0o000), (0o600, 0o022), (0o640, 0o027)])
def test_a_managed_block_backup_keeps_the_mode_of_the_file(tmp_path, mode, umask):
    path = tmp_path / "AGENTS.md"
    path.write_text("# mine\nold kit section\n", encoding="utf-8")
    os.chmod(path, mode)
    old = os.umask(umask)
    try:
        backup = _shared._create_backup(path, "# mine\nold kit section\n", "20261012T000000Z")
    finally:
        os.umask(old)
    assert oct(backup.stat().st_mode & 0o777) == oct(mode & ~umask)
    assert backup.stat().st_mode & 0o077 == mode & ~umask & 0o077, "never more open than the original"


# --- 2.2c, 4a: the watch's merge-save does not queue an entry twice ------------------------------------------------

def test_a_restore_entry_another_run_already_queued_is_not_added_twice(xdg):
    entry = {"op": "profile", "path": "gateway.bind", "action": "filled", "source": "setup", "at": "t"}
    record = {"run_id": "run-1", "done": False}
    on_disk = state.SetupState()
    on_disk.openclaw.config_watch = dict(record)
    on_disk.openclaw.host_restart_pending = [dict(entry)]
    state.save(on_disk)

    mine = state.SetupState()
    mine.openclaw.config_watch = {**record, "done": True, "result": {"outcome": "reverted"}}
    mine._owned_pending = [dict(entry), {**entry, "path": "agents.defaults.timeoutSeconds"}]
    host._watch_save(mine)

    final = state.load().openclaw
    assert sorted(e["path"] for e in final.host_restart_pending) == ["agents.defaults.timeoutSeconds",
                                                                    "gateway.bind"]
    assert final.config_watch["done"] is True


# --- 2.2c, 6c: a watch that cannot record its result leaves a marker the operator is told about ---------------------

def test_a_watch_that_cannot_save_leaves_a_marker_status_and_verify_report_and_a_save_clears(xdg, tmp_path,
                                                                                            monkeypatch):
    import fcntl
    from ai_resources.setup.cockpits import _openclaw_host as section
    monkeypatch.setattr(host, "WATCH_SAVE_WAIT_S", 0.3)
    r = DiskRig(tmp_path, [HOT])
    state.save(r.s)
    lock = _lock_file()
    holder = open(lock, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert r.run_default() == 0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    marker = state.read_watch_marker()
    assert marker and marker["outcome"] == "reverted" and marker["run_id"] == r.s.openclaw.config_watch["run_id"]
    marker_path = state.watch_marker_path()
    assert marker_path.parent == state.state_path().parent
    assert oct(marker_path.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in marker_path.parent.iterdir() if p.name.endswith(".tmp")], "written atomically"
    # the record itself is still not done: that is the gap the marker reports
    assert state.load().openclaw.config_watch.get("done") is not True
    lines = host.pending_lines(host.recorded_pending())
    assert any("a post-setup watch finished and could not record its result: reverted" in ln for ln in lines)
    findings = section._pending_findings(state.load().openclaw)
    assert any("could not record its result: reverted" in f.message for f in findings)
    state.save(state.load())
    assert state.read_watch_marker() is None
    assert not any("could not record its result" in ln for ln in host.pending_lines(host.recorded_pending()))


def test_a_watch_that_saves_leaves_no_marker(xdg, tmp_path):
    r = DiskRig(tmp_path, [HOT])
    state.save(r.s)
    assert r.run_default() == 0
    assert state.read_watch_marker() is None


def test_a_hostile_or_broken_marker_is_ignored(xdg):
    path = state.watch_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("not json", "[1, 2]", "", "null"):
        path.write_text(text, encoding="utf-8")
        assert state.read_watch_marker() is None
    path.write_text('{"outcome": "reverted\\u001b[31m", "reason": 7, "extra": "x"}', encoding="utf-8")
    got = state.read_watch_marker()
    assert set(got) == {"run_id", "outcome", "reason", "at"} and got["reason"] == "7"
    assert "\x1b" not in got["outcome"] and got["outcome"].startswith("reverted")
