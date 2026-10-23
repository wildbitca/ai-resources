"""ADR-0016 D4 (AC-14): one operator-rules file, rendered identically into CLAUDE.md, every OpenClaw agent AGENTS.md and
GEMINI.md; seeded once from CLAUDE.md after a confirm; fill-only and idempotent."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from ai_resources.setup import claude_md_migrate as cmm
from ai_resources.setup import deferred, hygiene, live_auth, operator_rules as rules, state, ui, wizard
from ai_resources.setup.cockpits import _shared, claude, gemini
from ai_resources.setup.cockpits import openclaw as oc

KEY_LIKE = "sk-ant-api03-" + "A" * 40


def section(title, body=None, n=200):
    text = body or (f"Regla de {title.lower()}: seguir siempre, con acentos y eñes. " * 12)
    return f"## {title}\n\n{text}\n\n"


FIVE = (section("IDIOMA — REGLA GLOBAL, SIN EXCEPCIONES") + section("COMMITS — REGLA GLOBAL")
        + section("CLICKUP — REGLAS GLOBALES") + section("PREGUNTAS AL USUARIO — REGLA GLOBAL")
        + section("CLUSTERS — SOLO WILDBIT"))
OTHER = section("Notas de proyecto")


# --- the path, the categories, the seed ---------------------------------------------------------------------------------

def test_path_is_xdg_aware_and_resolved_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "x"))
    assert rules.default_path() == tmp_path / "x" / "ai-resources" / "operator-rules.md"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert rules.default_path() == Path.home() / ".config" / "ai-resources" / "operator-rules.md"
    # the suite redirects `path()`, so no test can reach the operator's real file
    assert "operator-rules.md" in str(rules.path()) and not str(rules.path()).startswith(str(Path.home() / ".config"))


def test_the_five_categories_are_recognised_in_spanish_and_english_headings():
    got = {rules.category_of(h) for h in ("IDIOMA — REGLA GLOBAL", "Language", "COMMITS — REGLA GLOBAL",
                                          "PREGUNTAS AL USUARIO", "Questions", "CLICKUP — REGLAS", "CLUSTERS — SOLO WILDBIT")}
    assert got == {"language", "commits", "questions", "clickup", "clusters"}
    assert rules.category_of("Notas de proyecto") == "" and rules.category_of("Token cost") == ""


def test_the_seed_takes_the_five_blocks_in_file_order_and_nothing_else():
    seed = rules.seed_from_raw(OTHER + FIVE)
    assert [b.category for b in seed] == ["language", "commits", "clickup", "questions", "clusters"]
    text = rules.compose(seed)
    assert text.startswith("# Operator rules") and "Notas de proyecto" not in text
    assert text == rules.compose(rules.seed_from_raw(OTHER + FIVE))                  # deterministic


def test_the_kit_block_is_never_a_seed_source():
    kit = ("<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->\n"
           "## IDIOMA kit\n\ninside the kit block\n<!-- END ai-resources -->\n")
    assert rules.seed_from_raw(kit + FIVE)[0].heading.startswith("IDIOMA —")
    assert all("kit block" not in b.text for b in rules.seed_from_raw(kit + FIVE))


def test_secret_findings_flag_a_key_a_token_or_a_password_line():
    assert rules.secret_findings("## Rules\n\nspeak English\n") == []
    assert rules.secret_findings(f"use {KEY_LIKE} for it") == [1]
    assert rules.secret_findings("ok\napi_key = abcdef123456\n") == [2]
    assert rules.secret_findings("password: hunter2hunter2") == [1]


# --- create / append ---------------------------------------------------------------------------------------------------

def test_create_writes_only_when_absent_and_never_overwrites():
    text = rules.compose(rules.seed_from_raw(FIVE))
    assert rules.create(text) is True and rules.path().read_text(encoding="utf-8") == text
    assert rules.create("something else") is False
    assert rules.path().read_text(encoding="utf-8") == text


def test_append_section_is_fill_only_and_refuses_a_secret():
    rules.create(rules.compose(rules.seed_from_raw(FIVE)))
    before = rules.read()
    assert rules.append_section("IDIOMA — REGLA GLOBAL, SIN EXCEPCIONES", "## IDIOMA\n\nagain\n") is False
    assert rules.read() == before
    assert rules.append_section("Extra rule", "## Extra rule\n\nbe brief\n") is True
    assert rules.read().endswith("## Extra rule\n\nbe brief\n")
    assert rules.append_section("Leaky", f"## Leaky\n\n{KEY_LIKE}\n") is False


def test_render_is_empty_without_a_file_and_identical_text_otherwise():
    assert rules.block() == "" and rules.append_to("md\n") == "md\n"
    rules.create(rules.compose(rules.seed_from_raw(FIVE)))
    body = rules.block()
    assert body.startswith("## Operator rules\n\n") and "# Operator rules\n" not in body.split("\n", 1)[1][:30]
    assert "<!--" not in body                                       # the file's header comment is not rendered
    assert rules.append_to("md\n") == "md\n\n" + body
    assert rules.append_to("md\n") == rules.append_to("md\n")


# --- the wizard step ---------------------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    md = home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(OTHER + FIVE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", md)
    monkeypatch.setattr(claude, "CONFIG_ROOT", md.parent)
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    return types.SimpleNamespace(home=home, md=md, tmp=tmp_path)


def mk_state(*tools):
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": list(tools or ("claude",))}}
    return s


def test_the_step_shows_the_blocks_asks_once_and_writes_nothing_before_apply(host, monkeypatch):
    asked, shown = [], []
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append((m, default)) or True)
    for name in ("info", "detail", "warn"):
        monkeypatch.setattr(ui, name, lambda m, *a, **k: shown.append(str(m)))
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    s = mk_state("claude", "openclaw", "agy")
    wizard._step_operator_rules(s)
    assert not rules.exists() and len(asked) == 1 and asked[0][1] is True
    assert sum(1 for m in shown if m.startswith(("language:", "commits:", "clickup:", "questions:", "clusters:"))) == 5
    keys = [a.key for a in deferred.pending(s)]
    assert keys == ["operator_rules_file", "operator_rules_blocks"]
    assert "CLAUDE.md" in deferred.review_labels(s)[1] and "OpenClaw" in deferred.review_labels(s)[1] \
        and "GEMINI.md" in deferred.review_labels(s)[1]
    deferred.run_all(s)
    assert rules.exists() and rules.read().count("# Operator rules") == 1


def test_a_declined_seed_queues_nothing_and_a_second_run_with_the_file_is_silent_but_queues_the_refresh(host, monkeypatch):
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: False)
    s = mk_state()
    wizard._step_operator_rules(s)
    assert deferred.pending(s) == [] and not rules.exists()
    rules.create(rules.compose(rules.seed_from_claude_md()))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("asked with the file present"))
    s2 = mk_state()
    wizard._step_operator_rules(s2)
    assert [a.key for a in deferred.pending(s2)] == ["operator_rules_blocks"]


def test_a_secret_in_the_blocks_stops_the_seed(host, monkeypatch):
    host.md.write_text(OTHER + FIVE + section("CLUSTERS secrets", f"token {KEY_LIKE}"), encoding="utf-8")
    warned = []
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda *a, **k: None)
    monkeypatch.setattr(ui, "warn", warned.append)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("offered a seed with a secret"))
    s = mk_state()
    wizard._step_operator_rules(s)
    assert deferred.pending(s) == [] and any("look like a key" in m for m in warned)


def test_nothing_to_seed_and_headless_runs_do_nothing(host, monkeypatch):
    host.md.write_text(OTHER, encoding="utf-8")
    s = mk_state()
    wizard._step_operator_rules(s)
    assert deferred.pending(s) == []
    host.md.write_text(OTHER + FIVE, encoding="utf-8")
    monkeypatch.setattr(hygiene, "is_headless", lambda: True)
    wizard._step_operator_rules(s)
    assert deferred.pending(s) == [] and not rules.exists()


def test_a_dry_run_asks_and_queues_nothing(host, monkeypatch):
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("asked in a dry run"))
    s = mk_state()
    wizard._step_operator_rules(s, dry_run=True)
    assert deferred.pending(s) == []


# --- the migration backstop --------------------------------------------------------------------------------------------

def test_a_moved_rule_is_offered_to_operator_rules_and_reaches_the_file_at_apply(host, monkeypatch):
    big = section("IDIOMA — REGLA GLOBAL", n=600, body="regla importante: no traducir. " * 150)
    host.md.write_text(big, encoding="utf-8")
    asked = []
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda *a, **k: None)
    monkeypatch.setattr(ui, "ok", lambda *a, **k: None)
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, **k: "skill")
    monkeypatch.setattr(ui, "text", lambda m, default="", validate=None: "Language rule")
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append(m) or True)
    s = mk_state()
    decisions = cmm.plan(s)
    assert len(decisions) == 1 and decisions[0].copy_to_rules is True and "standing rules" in asked[0]
    assert not rules.exists()                                      # planning writes nothing
    cmm.apply(s, decisions)
    assert rules.exists() and "IDIOMA — REGLA GLOBAL" in rules.read()
    assert "Moved out of this file" in host.md.read_text(encoding="utf-8")


def test_a_rule_already_in_the_file_is_not_offered_again(host, monkeypatch):
    rules.create(rules.compose(rules.seed_from_raw(FIVE)))
    host.md.write_text(section("IDIOMA — REGLA GLOBAL, SIN EXCEPCIONES", body="regla importante. " * 200), encoding="utf-8")
    asked = []
    for name in ("section", "info", "detail"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, **k: "skill")
    monkeypatch.setattr(ui, "text", lambda m, default="", validate=None: "Language rule")
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append(m) or True)
    decisions = cmm.plan(mk_state())
    assert decisions and decisions[0].copy_to_rules is False and asked == []


# --- AC-14 end to end: three writers, identical text, a second run changes nothing ---------------------------------------

def block_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    assert begin is not None and end is not None, f"no managed block in {path}"
    return "".join(lines[begin:end + 1])


def rules_part(path: Path) -> str:
    text = block_text(path)
    i = text.index("## Operator rules")
    return text[i:text.rindex("<!-- END")]


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def world(host, monkeypatch):
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    ws = {name: host.home / ".openclaw" / f"workspace-{name}" for name in ("main", "ai")}
    for w in ws.values():
        w.mkdir(parents=True)
        (w / "AGENTS.md").write_text("# mine\n\nkeep this line\n", encoding="utf-8")
    doc = {"agents": {"defaults": {"workspace": str(ws["main"])},
                      "entries": {"main": {"workspace": str(ws["main"])}, "ai": {"workspace": str(ws["ai"])}}}}
    return types.SimpleNamespace(host=host, ws=ws, doc=doc)


def render_all(world, s):
    from ai_resources.setup import profiles
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})
    gemini.configure({"state": s, "gateway_url": ""})
    oc._configure_workspace_blocks(s, world.doc, "/kit", "", [], dry_run=False, force_default=True)


def test_three_cockpits_get_the_same_rules_and_a_second_run_changes_nothing(world, monkeypatch):
    for name in ("section", "info", "detail", "ok"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    s = mk_state("claude", "openclaw", "agy")
    wizard._step_operator_rules(s)
    deferred.run_all(s)
    assert rules.exists()
    render_all(world, s)
    texts = {"claude": rules_part(claude.CLAUDE_MD_PATH), "gemini": rules_part(gemini.GEMINI_MD_PATH),
             "main": rules_part(world.ws["main"] / "AGENTS.md"), "ai": rules_part(world.ws["ai"] / "AGENTS.md")}
    assert len(set(texts.values())) == 1, "the rendered rules differ between cockpits"
    only = next(iter(texts.values()))
    for heading in ("IDIOMA", "COMMITS", "CLICKUP", "PREGUNTAS", "CLUSTERS"):
        assert heading in only
    assert "keep this line" in (world.ws["main"] / "AGENTS.md").read_text(encoding="utf-8")   # outside the block: untouched

    snapshot = tree(world.host.home)
    snapshot_rules = rules.path().read_bytes()
    s2 = mk_state("claude", "openclaw", "agy")
    wizard._step_operator_rules(s2)
    deferred.run_all(s2)
    render_all(world, s2)
    assert tree(world.host.home) == snapshot and rules.path().read_bytes() == snapshot_rules


def test_a_block_moved_to_a_skill_still_reaches_agents_and_gemini(world, monkeypatch):
    for name in ("section", "info", "detail", "ok"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    big = section("IDIOMA — REGLA GLOBAL", body="regla importante: no traducir. " * 200)
    world.host.md.write_text(big + OTHER, encoding="utf-8")
    s = mk_state("claude", "openclaw", "agy")
    wizard._step_operator_rules(s)                       # runs BEFORE the migration: the block is captured first
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, **k: "skill")
    monkeypatch.setattr(ui, "text", lambda m, default="", validate=None: "Language rule")
    wizard._step_claude_md(s)
    deferred.run_all(s)
    assert "Moved out of this file" in world.host.md.read_text(encoding="utf-8")
    render_all(world, s)
    for path in (world.ws["main"] / "AGENTS.md", world.ws["ai"] / "AGENTS.md", gemini.GEMINI_MD_PATH):
        assert "IDIOMA — REGLA GLOBAL" in rules_part(path), path
    assert "IDIOMA — REGLA GLOBAL" in rules_part(claude.CLAUDE_MD_PATH)


def test_no_rules_file_means_no_operator_rules_section_anywhere(world):
    s = mk_state("claude", "openclaw", "agy")
    render_all(world, s)
    for path in (claude.CLAUDE_MD_PATH, gemini.GEMINI_MD_PATH, world.ws["main"] / "AGENTS.md"):
        assert "## Operator rules" not in path.read_text(encoding="utf-8"), path


def test_the_state_never_holds_the_rules_text(world, monkeypatch):
    for name in ("section", "info", "detail", "ok"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    s = mk_state("claude")
    wizard._step_operator_rules(s)
    dumped = json.dumps(state._to_dict(s))
    assert "IDIOMA" not in dumped and "Regla de" not in dumped
