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


# --- kit 2.2d, 3b/3c: one predicate for "corrupt", and a refusal that tells the truth about its copy ---------------

NOT_A_MAPPING = ["[]\n", "0\n", "false\n", "''\n", '""\n', "- a\n- b\n", "just a string\n", "42\n", "[1, 2]\n"]


@pytest.mark.parametrize("text", NOT_A_MAPPING)
def test_load_and_save_agree_that_a_file_that_is_not_a_mapping_is_corrupt(monkeypatch, tmp_path, text):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    monkeypatch.setattr(state, "_replace_corrupt_ok", None, raising=False)
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    assert state.save_would_refuse() is True
    state.load()
    backup = state.take_corrupt_notice()
    assert backup and pathlib.Path(backup).read_text(encoding="utf-8") == text, "load copied it aside and said so"
    with pytest.raises(state.StateCorrupt):
        state.save(state.SetupState())
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("text", ["", "\n", "# only a comment\n", "{}\n", "mode: local\n", "~\n"])
def test_an_empty_or_mapping_file_is_not_corrupt_for_load_or_save(monkeypatch, tmp_path, text):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    monkeypatch.setattr(state, "_replace_corrupt_ok", None, raising=False)
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    assert state.save_would_refuse() is False
    state.load()
    assert state.take_corrupt_notice() is None
    state.save(state.SetupState())
    assert "last_run" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["[]\n", "0\n", "false\n", "''\n"])
def test_an_interactive_setup_asks_before_replacing_every_file_that_save_refuses(monkeypatch, tmp_path, text):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    monkeypatch.setattr(state, "_replace_corrupt_ok", None, raising=False)
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    lines, asked, args = _run_wizard(monkeypatch, non_interactive=False, answer=False)
    assert wizard._run(args) == 1
    assert len(asked) == 1 and "replace the file" in asked[0]
    assert path.read_text(encoding="utf-8") == text


def test_the_replace_question_defaults_to_no(monkeypatch, corrupt_file):
    lines, _asked, args = _run_wizard(monkeypatch, non_interactive=False, answer=False)
    defaults: list[bool] = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=True: defaults.append(default) or False)
    assert wizard._run(args) == 1
    assert defaults == [False], "Enter must not replace a state file the operator could not read"


def _copies(path: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in path.parent.iterdir() if ".corrupt" in p.name)


def test_every_refusing_verb_leaves_the_copy_its_sentence_promises(corrupt_file, capsys):
    from ai_resources import openclaw_host as host
    assert _copies(corrupt_file) == [], "the verbs below refuse before any load(): the copy is made by the refusal"
    assert host.cmd_apply_pending(argparse.Namespace()) == 1
    said = capsys.readouterr().out
    [copy] = _copies(corrupt_file)
    assert copy.read_bytes() == corrupt_file.read_bytes() == CORRUPT.encode()
    assert copy.name in said and "a copy was made beside it as" in said
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert host.cmd_offbox_identity(argparse.Namespace(dry_run=False)) == 1
    again = capsys.readouterr().out
    assert _copies(corrupt_file) == [copy], "one copy per distinct content"
    assert copy.name in again
    assert corrupt_file.read_text(encoding="utf-8") == CORRUPT


def test_save_raises_with_a_copy_beside_the_file(corrupt_file):
    with pytest.raises(state.StateCorrupt) as e:
        state.save(state.SetupState())
    [copy] = _copies(corrupt_file)
    assert copy.read_bytes() == CORRUPT.encode() and copy.name in str(e.value)


def test_a_refusal_that_could_not_copy_does_not_claim_a_copy(corrupt_file, monkeypatch):
    monkeypatch.setattr(state, "_backup_corrupt", lambda path: None)
    text = state.refusal_text()
    assert "a copy was made" not in text and "no copy" in text.lower()


def test_the_state_lock_messages_are_exactly_the_sentences_the_operator_is_told(monkeypatch, tmp_path):
    """2.2d (4): `<lock> is a symlink` for a link; `<lock>: <ErrorName>` for a directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    lock = state.state_path().with_name(state.state_path().name + ".lock")
    lock.parent.mkdir(parents=True)
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    lock.symlink_to(real)
    with pytest.raises(state.StateLockUnusable) as e:
        with state.exclusive():
            pass
    assert str(e.value) == f"{lock} is a symlink"
    lock.unlink()
    lock.mkdir()
    with pytest.raises(state.StateLockUnusable) as e2:
        with state.exclusive():
            pass
    assert str(e2.value) == f"{lock}: IsADirectoryError"
