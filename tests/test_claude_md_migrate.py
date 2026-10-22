"""ADR-0014 D3.1 / AC-11: the guided migration of heavy blocks out of the global CLAUDE.md is confirmed block by block,
verbatim, backed up, hash-checked on restore, never touches the kit block and never runs headless."""
from __future__ import annotations

import json
import stat
import types
from datetime import datetime, timezone

import pytest

from ai_resources.setup import claude_md_migrate as cmm
from ai_resources.setup import hygiene, state, ui, wizard
from ai_resources.setup.cockpits import _shared, claude

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
KIT = ("<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->\n"
       "# ai-resources (Claude Code)\n\nKit root: `/kit`.\n\n## Delegation\n\n- Delegate wide searches.\n"
       "<!-- END ai-resources -->\n")


def _section(title, n, word="regla"):
    # ~n tokens of text (bytes / 4), non-ASCII on purpose: the text must move verbatim
    body = (f"{word} importante: no traducir, mantener acentos y eñes. " * 40)[: n * 4]
    return f"## {title}\n\n{body}\n\n"


BIG1 = _section("IDIOMA - regla global", 600)
BIG2 = _section("CLUSTERS - solo wildbit", 450)
SMALL = _section("Nota breve", 40)
OPERATOR = BIG1 + SMALL + BIG2


@pytest.fixture
def host(tmp_path, monkeypatch):
    md = tmp_path / "home" / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", md)
    monkeypatch.setattr(claude, "CONFIG_ROOT", md.parent)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return types.SimpleNamespace(md=md, root=md.parent, tmp=tmp_path)


def _write(host, text):
    host.md.write_text(text, encoding="utf-8", newline="")
    return text


def _state():
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    return s


def _answers(monkeypatch, plan):
    """plan: {heading fragment: ("skill", description) | ("rule", "globs") | "keep"}; records the questions asked."""
    asked = []

    def select(message, choices, default=None, instruction=""):
        asked.append(message)
        for frag, ans in plan.items():
            if frag in message:
                return ans if isinstance(ans, str) else ans[0]
        return default

    def text(message, default="", validate=None):
        asked.append(message)
        for frag, ans in plan.items():
            if frag in message or (isinstance(ans, tuple) and frag in str(current[0])):
                pass
        return current_text[0]

    current = [""]
    current_text = [""]
    monkeypatch.setattr(ui, "select", select)

    def text2(message, default="", validate=None):
        asked.append(message)
        for ans in plan.values():
            if isinstance(ans, tuple):
                if "description" in message and ans[0] == "skill":
                    return ans[1]
                if "globs" in message and ans[0] == "rule":
                    return ans[1]
        return default

    monkeypatch.setattr(ui, "text", text2)
    return asked


# --- parsing ---------------------------------------------------------------------------------------------------------

def test_blocks_are_the_operators_headed_sections_outside_the_kit_block():
    raw = "Preamble the operator wrote.\n\n" + KIT + "\n" + OPERATOR
    blocks, kit_hash = cmm.parse_blocks(raw)
    # the text before the kit block has no heading: one block, never split on a guess
    assert [b.heading for b in blocks] == ["Preamble the operator wrote.", "IDIOMA - regla global", "Nota breve",
                                           "CLUSTERS - solo wildbit"]
    assert kit_hash == cmm.sha(KIT)
    assert all("Delegation" not in b.text for b in blocks)                       # the kit block is never a block
    assert blocks[1].tokens >= 550 and blocks[2].tokens < cmm.MIN_TOKENS
    assert "".join(b.text for b in blocks) == "Preamble the operator wrote.\n\n" + OPERATOR   # the blocks tile the headed text outside the kit block


def test_a_heading_inside_a_code_fence_does_not_split():
    raw = "## A\n\ntext\n\n```md\n## not a heading\n```\n\n## B\n\nmore\n"
    assert [b.heading for b in cmm.parse_blocks(raw)[0]] == ["A", "B"]


def test_an_unclosed_fence_makes_one_unsplit_block():
    raw = "## A\n\n```\ncode never closed\n\n## B\n\nmore\n"
    blocks, _ = cmm.parse_blocks(raw)
    assert len(blocks) == 1 and blocks[0].text == raw


def test_no_headings_is_one_block_and_nothing_at_all_is_none():
    blocks, _ = cmm.parse_blocks("just a long note\nwith lines\n")
    assert len(blocks) == 1 and blocks[0].heading == "just a long note"
    assert cmm.parse_blocks("")[0] == [] and cmm.parse_blocks("\n\n")[0] == []


def test_a_single_title_over_sections_splits_at_the_sections():
    raw = "# My rules\n\n## One\n\na\n\n## Two\n\nb\n"
    assert [b.heading for b in cmm.parse_blocks(raw)[0]] == ["One", "Two"]


def test_slugs_are_ascii_and_bounded():
    assert cmm.slugify("IDIOMA - regla global") == "idioma-regla-global"
    assert cmm.slugify("Preguntas al usuario - Ñandú") == "preguntas-al-usuario-nandu"
    assert cmm.slugify("!!!") == "block" and len(cmm.slugify("x" * 200)) <= 40


# --- decline ---------------------------------------------------------------------------------------------------------

def test_decline_leaves_the_file_byte_identical_with_no_backup_and_no_record(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    asked = _answers(monkeypatch, {})                      # every question answered with its default: keep
    s = _state()
    assert cmm.run_step(s, now=NOW) == 0
    assert host.md.read_text(encoding="utf-8", newline="") == original
    assert len(asked) == 2 and not any("Nota breve" in a for a in asked)    # only the heavy blocks, one at a time
    assert asked[0].startswith("'IDIOMA") and "estimate" in asked[0]          # heaviest first
    assert s.tracking.claude_md_migrations == []
    assert list(host.root.glob("CLAUDE.md.ai-resources-backup*")) == [] and not (host.root / "skills").exists()


def test_a_dry_run_asks_nothing(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    cmm.run_step(_state(), dry_run=True, now=NOW)
    assert host.md.read_text(encoding="utf-8", newline="") == original


def test_nothing_heavy_means_no_question(host, monkeypatch):
    _write(host, KIT + "\n" + SMALL)
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    assert cmm.run_step(_state(), now=NOW) == 0


# --- accept ----------------------------------------------------------------------------------------------------------

def test_accept_to_a_skill_moves_the_block_verbatim_and_leaves_the_rest(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    _answers(monkeypatch, {"IDIOMA": ("skill", "Use when writing any artifact: language rules.")})
    s = _state()
    cmm.run_step(s, now=NOW)

    (rec,) = s.tracking.claude_md_migrations
    dest = host.root / "skills" / "operator-idioma-regla-global" / "SKILL.md"
    assert rec["destination"] == str(dest) and rec["kind"] == "skill" and rec["block_id"] == cmm.parse_blocks(original)[0][0].id
    text = dest.read_text(encoding="utf-8", newline="")
    assert text.startswith("---\nname: operator-idioma-regla-global\ndescription: \"Use when writing any artifact: language rules.\"\n---\n\n")
    assert BIG1.rstrip("\n") + "\n" in text and "acentos y eñes" in text           # verbatim, never translated
    assert rec["sha256"] == cmm.sha(text) and text[rec["body_offset"]:] == BIG1.rstrip("\n") + "\n"
    new = host.md.read_text(encoding="utf-8", newline="")
    assert rec["pointer"] in new and BIG1.rstrip("\n") not in new and "## IDIOMA" not in new
    assert KIT in new and SMALL in new and BIG2 in new                              # kit block, small block, kept block
    assert cmm.parse_blocks(new)[1] == cmm.sha(KIT)
    assert (rec["pointer"].count("\n") == 0) and "operator-idioma-regla-global" in rec["pointer"]
    backup = host.root / "CLAUDE.md.ai-resources-backup-20261022T120000Z"
    assert rec["backup"] == str(backup) and backup.read_text(encoding="utf-8", newline="") == original


def test_accept_to_a_rule_writes_the_operator_paths(host, monkeypatch):
    _write(host, KIT + "\n" + OPERATOR)
    _answers(monkeypatch, {"CLUSTERS": ("rule", "k8s/**/*.yaml, infra/**")})
    s = _state()
    cmm.run_step(s, now=NOW)
    (rec,) = s.tracking.claude_md_migrations
    dest = host.root / "rules" / "clusters-solo-wildbit.md"
    text = dest.read_text(encoding="utf-8", newline="")
    assert text.startswith('---\npaths:\n  - "k8s/**/*.yaml"\n  - "infra/**"\n---\n\n') and BIG2.rstrip("\n") in text
    assert rec["kind"] == "rule" and dest.is_file()


def test_a_rule_without_paths_stays_in_claude_md(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    _answers(monkeypatch, {"CLUSTERS": ("rule", "  ,  ")})
    s = _state()
    cmm.run_step(s, now=NOW)
    assert host.md.read_text(encoding="utf-8", newline="") == original and s.tracking.claude_md_migrations == []


def test_the_backup_is_made_once_per_run_and_before_the_first_edit(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    _answers(monkeypatch, {"IDIOMA": ("skill", "d1"), "CLUSTERS": ("skill", "d2")})
    s = _state()
    cmm.run_step(s, now=NOW)
    backups = list(host.root.glob("CLAUDE.md.ai-resources-backup*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8", newline="") == original
    assert len(s.tracking.claude_md_migrations) == 2 and len({r["backup"] for r in s.tracking.claude_md_migrations}) == 1


def test_an_existing_destination_is_never_overwritten(host, monkeypatch):
    _write(host, KIT + "\n" + OPERATOR)
    mine = host.root / "skills" / "operator-idioma-regla-global"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own skill")
    _answers(monkeypatch, {"IDIOMA": ("skill", "d")})
    s = _state()
    cmm.run_step(s, now=NOW)
    assert (mine / "SKILL.md").read_text() == "my own skill"
    assert s.tracking.claude_md_migrations[0]["destination"].endswith("operator-idioma-regla-global-2/SKILL.md")


def test_the_kit_block_is_never_edited_even_when_a_write_would_change_it(host, monkeypatch):
    original = _write(host, KIT + "\n" + OPERATOR)
    _answers(monkeypatch, {"IDIOMA": ("skill", "d")})
    real = cmm.migrate_block

    def evil(raw, block, kind, **kw):
        new, rec = real(raw, block, kind, **kw)
        return new.replace("Delegate wide searches.", "TAMPERED"), rec
    monkeypatch.setattr(cmm, "migrate_block", evil)
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    s = _state()
    cmm.run_step(s, now=NOW)
    assert host.md.read_text(encoding="utf-8", newline="") == original and s.tracking.claude_md_migrations == []
    assert any("kit block" in w for w in warned)


# --- restore ---------------------------------------------------------------------------------------------------------

def _migrated(host, monkeypatch, text=None, plan=None):
    original = _write(host, text if text is not None else KIT + "\n" + OPERATOR)
    _answers(monkeypatch, plan or {"IDIOMA": ("skill", "d"), "CLUSTERS": ("rule", "k8s/**")})
    s = _state()
    cmm.run_step(s, now=NOW)
    return original, s


def test_restore_round_trips_to_the_original_bytes(host, monkeypatch):
    original, s = _migrated(host, monkeypatch)
    assert host.md.read_text(encoding="utf-8", newline="") != original
    for rec in reversed(s.tracking.claude_md_migrations):
        assert cmm.restore_one(rec) == (True, "")
    assert host.md.read_text(encoding="utf-8", newline="") == original
    assert not (host.root / "skills" / "operator-idioma-regla-global").exists()
    assert not (host.root / "rules" / "clusters-solo-wildbit.md").exists()


def test_restore_refuses_when_the_destination_was_edited(host, monkeypatch):
    _original, s = _migrated(host, monkeypatch, plan={"IDIOMA": ("skill", "d")})
    (rec,) = s.tracking.claude_md_migrations
    dest = host.root / "skills" / "operator-idioma-regla-global" / "SKILL.md"
    dest.write_text(dest.read_text(encoding="utf-8") + "\nmy later edit\n", encoding="utf-8")
    after = host.md.read_text(encoding="utf-8", newline="")
    ok, why = cmm.restore_one(rec)
    assert ok is False and "edited" in why and rec["backup"] in why
    assert host.md.read_text(encoding="utf-8", newline="") == after and dest.is_file()


def test_restore_refuses_when_the_pointer_was_edited_or_duplicated(host, monkeypatch):
    _original, s = _migrated(host, monkeypatch, plan={"IDIOMA": ("skill", "d")})
    (rec,) = s.tracking.claude_md_migrations
    good = host.md.read_text(encoding="utf-8", newline="")
    host.md.write_text(good.replace(rec["pointer"], rec["pointer"] + " (mine)"), encoding="utf-8", newline="")
    assert cmm.restore_one(rec)[0] is False
    host.md.write_text(good + rec["pointer"] + "\n", encoding="utf-8", newline="")
    assert cmm.restore_one(rec)[0] is False


def test_restore_never_touches_a_pointer_text_inside_the_kit_block(host, monkeypatch):
    _original, s = _migrated(host, monkeypatch, plan={"IDIOMA": ("skill", "d")})
    (rec,) = s.tracking.claude_md_migrations
    text = host.md.read_text(encoding="utf-8", newline="")
    host.md.write_text(text.replace("- Delegate wide searches.", rec["pointer"]).replace(rec["pointer"] + "\n\n", "", 0),
                       encoding="utf-8", newline="")
    # the real pointer is still outside the block exactly once, so the restore still finds the right one
    ok, _why = cmm.restore_one(rec)
    assert ok is True and rec["pointer"] in host.md.read_text(encoding="utf-8", newline="")   # the kit block's copy stays


def test_crlf_files_and_a_block_at_the_end_without_a_newline_round_trip(host, monkeypatch):
    crlf = (KIT + "\n" + OPERATOR).replace("\n", "\r\n")
    original, s = _migrated(host, monkeypatch, text=crlf, plan={"IDIOMA": ("skill", "d")})
    assert "\r\n" in host.md.read_text(encoding="utf-8", newline="")
    assert cmm.restore_one(s.tracking.claude_md_migrations[0]) == (True, "")
    assert host.md.read_text(encoding="utf-8", newline="") == original

    no_eol = KIT + "\n" + SMALL + BIG1.rstrip("\n")
    original2, s2 = _migrated(host, monkeypatch, text=no_eol, plan={"IDIOMA": ("skill", "d")})
    assert s2.tracking.claude_md_migrations and not s2.tracking.claude_md_migrations[0]["ends_with_newline"]
    assert cmm.restore_one(s2.tracking.claude_md_migrations[0]) == (True, "")
    assert host.md.read_text(encoding="utf-8", newline="") == original2


def test_a_second_run_offers_nothing_new(host, monkeypatch):
    _original, s = _migrated(host, monkeypatch, plan={"IDIOMA": ("skill", "d"), "CLUSTERS": ("skill", "d")})
    before = host.md.read_text(encoding="utf-8", newline="")
    asked = _answers(monkeypatch, {})
    cmm.run_step(s, now=NOW)
    assert asked == [] and host.md.read_text(encoding="utf-8", newline="") == before and len(s.tracking.claude_md_migrations) == 2


def test_cmd_restore_restores_updates_the_state_and_keeps_what_it_cannot(host, monkeypatch):
    _original, s = _migrated(host, monkeypatch)
    state.save(s)
    dest = host.root / "rules" / "clusters-solo-wildbit.md"
    dest.write_text(dest.read_text(encoding="utf-8") + "edit", encoding="utf-8")
    said = []
    assert cmm.cmd_restore(say=said.append) == 1
    left = state.load().tracking.claude_md_migrations
    assert [r["kind"] for r in left] == ["rule"] and any("edited" in m for m in said)
    dest.unlink()
    assert cmm.cmd_restore(say=said.append) == 1                                    # destination missing: still refuses
    assert [r["kind"] for r in state.load().tracking.claude_md_migrations] == ["rule"]


def test_cmd_restore_with_nothing_recorded(host):
    said = []
    assert cmm.cmd_restore(say=said.append) == 0 and "Nothing to restore" in said[0]


def test_the_setup_flag_runs_the_restore_without_the_wizard(monkeypatch):
    from ai_resources import cli
    called = []
    monkeypatch.setattr(cmm, "cmd_restore", lambda **k: called.append(1) or 0)
    monkeypatch.setattr(wizard, "run", lambda a: (_ for _ in ()).throw(AssertionError("the wizard must not run")))
    args = cli.build_parser().parse_args(["setup", "--restore-claude-md"])
    assert args.func(args) == 0 and called == [1]


# --- headless --------------------------------------------------------------------------------------------------------

def test_never_headless(monkeypatch):
    s = _state()
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.delenv("OPENCLAW_CLI", raising=False)
    assert cmm.skip_reason(s) is None
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert cmm.skip_reason(s) == "headless"
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setenv("OPENCLAW_CLI", "1")
    assert cmm.skip_reason(s) == "headless"


def test_the_wizard_step_is_registered_after_hygiene_and_is_silent_when_skipped(monkeypatch):
    spec = next(sp for sp in wizard._build_steps(False) if sp.id == "claude_md")
    monkeypatch.setattr(cmm, "skip_reason", lambda s: "headless")
    assert spec.skip(types.SimpleNamespace(s=_state(), dry_run=False)) == ("Global CLAUDE.md", "")
    monkeypatch.setattr(cmm, "skip_reason", lambda s: None)
    assert spec.skip(types.SimpleNamespace(s=_state(), dry_run=False)) is None
