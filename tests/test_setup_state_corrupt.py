"""A setup-state file that is not valid YAML is copied aside once and never silently replaced by an empty
record (kit 2.2c, 3d): unattended writers refuse, an interactive `setup` says so."""
from __future__ import annotations

import argparse
import pathlib
import stat
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import state, ui, wizard  # noqa: E402

CORRUPT = "profile: [unclosed\n  name: {oops\n"


@pytest.fixture
def corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text(CORRUPT, encoding="utf-8")
    return path


def test_load_copies_the_corrupt_file_aside_once_with_owner_only_mode(corrupt_file):
    assert state.load().mode == state.SetupState().mode           # read as empty
    backup = pathlib.Path(state.take_corrupt_notice())
    assert backup == corrupt_file.with_name(corrupt_file.name + ".corrupt")
    assert backup.read_text(encoding="utf-8") == CORRUPT
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert corrupt_file.read_text(encoding="utf-8") == CORRUPT, "the file itself is not touched by a load"
    state.load()
    state.load()
    assert sorted(p.name for p in corrupt_file.parent.iterdir() if ".corrupt" in p.name) == [backup.name]
    assert state.take_corrupt_notice() is not None and state.take_corrupt_notice() is None


def test_a_different_corrupt_content_gets_its_own_copy_and_a_symlink_is_never_followed(corrupt_file, tmp_path):
    state.load()
    corrupt_file.write_text("other: [broken\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    state.load()
    names = sorted(p.name for p in corrupt_file.parent.iterdir() if ".corrupt" in p.name)
    assert names == [corrupt_file.name + ".corrupt", corrupt_file.name + ".corrupt-1"]
    planted = corrupt_file.with_name(corrupt_file.name + ".corrupt-2")
    corrupt_file.write_text("third: [broken\n", encoding="utf-8")
    planted.symlink_to(victim)
    state.load()
    assert victim.read_text(encoding="utf-8") == "keep"


def test_save_refuses_to_replace_a_corrupt_file(corrupt_file):
    s = state.load()
    with pytest.raises(state.StateCorrupt):
        state.save(s)
    assert corrupt_file.read_text(encoding="utf-8") == CORRUPT


def test_the_watch_and_other_unattended_writers_do_not_write_over_it(corrupt_file):
    wrote = []
    assert state.update(lambda fresh: wrote.append(1)) is False
    assert wrote == [] and corrupt_file.read_text(encoding="utf-8") == CORRUPT


def test_a_save_is_allowed_once_the_operator_was_told(corrupt_file):
    s = state.load()
    state.allow_replacing_corrupt()
    state.save(s)
    assert "last_run" in corrupt_file.read_text(encoding="utf-8")
    assert state.load().last_run


def test_a_valid_or_missing_file_is_unaffected(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg2"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    s = state.load()
    state.save(s)
    assert state.take_corrupt_notice() is None
    assert state.update(lambda fresh: None) is True
    state.state_path().write_text("", encoding="utf-8")            # an empty file is an empty record, not corrupt
    assert state.load().mode == state.SetupState().mode and state.take_corrupt_notice() is None


def _run_wizard(monkeypatch, *, non_interactive: bool, answer: bool = False):
    lines: list[str] = []
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: non_interactive)
    monkeypatch.setattr(ui, "set_non_interactive", lambda v: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "error", lambda msg, *a, **k: lines.append(str(msg)))
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda *a, **k: None)
    asked: list[str] = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=True: asked.append(msg) or answer)
    stop = RuntimeError("went on past the state check")
    monkeypatch.setattr(wizard, "_preflight_selection", lambda args, s: (_ for _ in ()).throw(stop))
    args = argparse.Namespace(dry_run=False, non_interactive=non_interactive, profile="", yes=False)
    return lines, asked, args


def test_a_non_interactive_setup_stops_and_says_where_the_copy_is(corrupt_file, monkeypatch):
    lines, asked, args = _run_wizard(monkeypatch, non_interactive=True)
    assert wizard._run(args) == 1
    assert asked == [] and any(".corrupt" in ln and "not valid YAML" in ln for ln in lines)
    assert corrupt_file.read_text(encoding="utf-8") == CORRUPT


def test_an_interactive_setup_says_so_and_asks_before_going_on(corrupt_file, monkeypatch):
    lines, asked, args = _run_wizard(monkeypatch, non_interactive=False, answer=False)
    assert wizard._run(args) == 1
    assert len(asked) == 1 and "replace the file" in asked[0]
    assert any("not valid YAML" in ln for ln in lines)
    lines, asked, args = _run_wizard(monkeypatch, non_interactive=False, answer=True)
    state.load()                                          # the notice was taken by the first run
    with pytest.raises(RuntimeError, match="went on past the state check"):
        wizard._run(args)
    s = state.SetupState()
    state.save(s)                                         # allowed now: the operator said yes
    assert "last_run" in corrupt_file.read_text(encoding="utf-8")


def test_a_symlinked_state_lock_says_it_is_a_symlink(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    lock = state.state_path().with_name(state.state_path().name + ".lock")
    lock.parent.mkdir(parents=True)
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    lock.symlink_to(real)
    with pytest.raises(state.StateLockUnusable) as e:
        with state.exclusive():
            pass
    assert "is a symlink" in str(e.value)
