"""`ai-resources openclaw dedupe-agents-md`: the hand-written copy of the kit's long-running section (2.1.1, D5).

Never silent: a dry run shows the diff, `--apply` asks first and writes a backup, a copy that differs is only shown.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import ui  # noqa: E402
from ai_resources.setup.cockpits import _shared  # noqa: E402

HEAD = _shared.OPENCLAW_LONG_RUNNING_HEADING
KIT = _shared.OPENCLAW_LONG_RUNNING_MD


def _block() -> str:
    return f"{_shared.MANAGED_BEGIN}\n# ai-resources\n{KIT}{_shared.MANAGED_END}\n"


def _file(tmp_path, *, before="# Mine\n\nHand notes.\n\n", copy=KIT, after="\n## Other section\n\nKeep me.\n",
          block=True) -> pathlib.Path:
    p = tmp_path / "AGENTS.md"
    p.write_text(before + (copy.lstrip("\n") if copy else "") + after + (_block() if block else ""), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _a_terminal_by_default(monkeypatch):
    """The confirm needs a real terminal (T2); the tests below that simulate an answer have one."""
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)


def _args(*paths, apply=False):
    return argparse.Namespace(paths=[str(p) for p in paths], apply=apply)


def test_an_identical_copy_is_found_and_the_dry_run_writes_nothing(tmp_path, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()
    assert host.cmd_dedupe_agents_md(_args(p)) == 0
    out = capsys.readouterr().out
    assert "would be removed" in out and "dry run" in out
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


def test_apply_with_a_confirmation_writes_a_backup_then_removes_only_the_section(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_text(encoding="utf-8")
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 0
    [backup] = list(tmp_path.glob("AGENTS.md.bak-*"))
    assert backup.read_text(encoding="utf-8") == before
    after = p.read_text(encoding="utf-8")
    assert HEAD not in after.split(_shared.MANAGED_BEGIN)[0]
    assert "# Mine\n\nHand notes.\n" in after and "## Other section\n\nKeep me.\n" in after
    assert after.count(HEAD) == 1, "the kit's own managed copy is untouched"
    assert after.split(_shared.MANAGED_BEGIN, 1)[1] == before.split(_shared.MANAGED_BEGIN, 1)[1]


def test_apply_is_never_unattended(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


def test_a_declined_confirmation_changes_nothing(tmp_path, monkeypatch):
    p = _file(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


def test_a_copy_that_differs_is_refused_with_a_diff_even_with_apply(tmp_path, monkeypatch, capsys):
    edited = KIT.replace("about 2 minutes", "about 10 minutes")
    assert edited != KIT
    p = _file(tmp_path, copy=edited)
    before = p.read_bytes()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    out = capsys.readouterr().out
    assert "differs" in out and "10 minutes" in out
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


def test_whitespace_only_differences_still_count_as_identical(tmp_path):
    reflowed = KIT.replace("; then systemd", ";\nthen systemd").replace("\n- ", "\n\n- ")
    p = _file(tmp_path, copy=reflowed)
    found = _shared.long_running_duplicate(p.read_text(encoding="utf-8"))
    assert found is not None and found[2] is True


def test_a_file_with_only_the_managed_block_has_nothing_to_dedupe(tmp_path, capsys):
    p = tmp_path / "AGENTS.md"
    p.write_text("# Mine\n\n" + _block(), encoding="utf-8")
    assert host.cmd_dedupe_agents_md(_args(p)) == 0
    assert "no hand-written copy" in capsys.readouterr().out


def test_the_verb_is_registered_and_verify_names_it():
    root = argparse.ArgumentParser()
    host.add_subparser(root.add_subparsers())
    ns = root.parse_args(["openclaw", "dedupe-agents-md", "--apply", "x.md"])
    assert ns.func is host.cmd_dedupe_agents_md and ns.apply is True and ns.paths == ["x.md"]
    assert "dedupe-agents-md" in (REPO / "scripts/ai_resources/setup/cockpits/openclaw.py").read_text(encoding="utf-8")


def test_a_symlink_planted_at_the_backup_name_is_not_followed(tmp_path):
    p = _file(tmp_path)
    before = p.read_bytes()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (tmp_path / "AGENTS.md.bak-20260101T000000Z").symlink_to(victim)
    with pytest.raises(OSError):
        _shared.dedupe_long_running(p, apply=True, now="20260101T000000Z")
    assert victim.read_text(encoding="utf-8") == "precious"
    assert p.read_bytes() == before, "the file is not rewritten when the backup could not be made"


def test_the_verb_fails_safely_when_the_backup_cannot_be_created(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    real = _shared.dedupe_long_running

    def planted(path, *, apply, now=None):
        if apply:
            (tmp_path / "AGENTS.md.bak-20260101T000000Z").symlink_to(victim)
            now = "20260101T000000Z"
        return real(path, apply=apply, now=now)

    monkeypatch.setattr(_shared, "dedupe_long_running", planted)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    assert victim.read_text(encoding="utf-8") == "precious" and p.read_bytes() == before
    assert "nothing written" in capsys.readouterr().out


def test_a_file_that_changed_between_the_two_checks_is_reported_not_claimed_removed(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    edited = p.read_text(encoding="utf-8").replace("about 2 minutes", "about 10 minutes")
    assert edited != p.read_text(encoding="utf-8")
    real = _shared.dedupe_long_running

    def racing(path, *, apply, now=None):
        if apply:
            path.write_text(edited, encoding="utf-8")
        return real(path, apply=apply, now=now)

    monkeypatch.setattr(_shared, "dedupe_long_running", racing)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    out = capsys.readouterr().out
    assert "file changed, nothing written" in out and "removed; backup" not in out
    assert p.read_text(encoding="utf-8") == edited and list(tmp_path.glob("*.bak-*")) == []


def _no_terminal(monkeypatch):
    """A confirm on a stdin that is not a terminal: questionary ends in EOFError."""
    def eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", eof)


def test_apply_without_a_terminal_says_so_in_one_line_and_changes_nothing(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()
    _no_terminal(monkeypatch)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    out = capsys.readouterr().out
    assert "would be removed" in out, "the dry run is printed first"
    assert out.rstrip().splitlines()[-1] == "nothing changed (--apply needs an interactive confirm)"
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


def test_the_diff_of_a_file_without_a_final_newline_ends_with_one(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# Mine\n\n" + KIT.lstrip("\n").rstrip("\n"), encoding="utf-8")      # no trailing newline
    res = _shared.dedupe_long_running(p, apply=False)
    assert res["status"] == "would-remove" and res["diff"].endswith("\n")


# --- 2.1.1 pass 3, T2: a piped answer is not a confirmation ---------------------------------------------------------

def test_a_piped_y_on_a_stdin_that_is_not_a_terminal_does_not_confirm(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()
    asked: list[str] = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append("asked") or True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    out = capsys.readouterr().out
    assert asked == [], "the confirm is never even asked"
    assert "would be removed" in out and out.rstrip().splitlines()[-1] == \
        "nothing changed (--apply needs an interactive confirm)"
    assert p.read_bytes() == before and list(tmp_path.glob("*.bak-*")) == []


# --- 2.1.1 pass 3, L3: the rewrite never goes through a symlink and is atomic ---------------------------------------

def test_a_symlinked_agents_md_is_refused_and_left_untouched(tmp_path, monkeypatch, capsys):
    (tmp_path / "real").mkdir()
    real = _file(tmp_path / "real")
    link = tmp_path / "ws" / "AGENTS.md"
    link.parent.mkdir()
    link.symlink_to(real)
    before = real.read_bytes()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(link, apply=True)) == 1
    out = capsys.readouterr().out
    assert "symlink" in out and str(real) in out
    assert real.read_bytes() == before and link.is_symlink()
    assert list(real.parent.glob("*.bak-*")) == [] and list(link.parent.glob("*.bak-*")) == []


def test_a_failed_write_leaves_the_original_and_names_the_backup(tmp_path, monkeypatch, capsys):
    p = _file(tmp_path)
    before = p.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(_shared.os, "replace", boom)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 1
    out = capsys.readouterr().out
    [backup] = list(tmp_path.glob("AGENTS.md.bak-*"))
    assert f"could not write AGENTS.md; the backup is at {backup}" in out
    assert "could not write the backup" not in out
    assert p.read_bytes() == before, "the original is intact"
    assert [x.name for x in tmp_path.iterdir() if x.name.startswith(".AGENTS.md")] == [], "no temp file is left"


def test_the_rewrite_keeps_the_file_mode(tmp_path, monkeypatch):
    p = _file(tmp_path)
    p.chmod(0o640)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert host.cmd_dedupe_agents_md(_args(p, apply=True)) == 0
    assert (p.stat().st_mode & 0o777) == 0o640
    assert HEAD not in p.read_text(encoding="utf-8").split(_shared.MANAGED_BEGIN)[0]


def test_the_polling_guidance_is_in_the_openclaw_block_exactly_once_and_short():
    md = _shared.openclaw_agent_kit_md("/kit")
    assert md.count(_shared.OPENCLAW_POLLING_HEADING) == 1
    assert len(_shared.OPENCLAW_POLLING_MD.strip().splitlines()) < 10
    assert "sessions_yield" in _shared.OPENCLAW_POLLING_MD and "60-120 s" in _shared.OPENCLAW_POLLING_MD
