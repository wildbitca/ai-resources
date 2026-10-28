"""3.2.8 item 5(a) and 5(c): the stale Telegram routing warning and the "Restore the engine" prompt are not recurring noise."""
from __future__ import annotations

from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import openclaw as oc
from ai_resources.verify import Finding as F
from wizard_fakes import Script

STALE = "- Telegram group -5217865131 is the ops room\n"


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Mine\n" + STALE + "keep this line\n", encoding="utf-8")
    return ws, {"agents": {"defaults": {"workspace": str(ws)}}}


def _fresh_ui(monkeypatch):
    ui.clear_skipped()
    store: dict = {}
    ui.bind_decisions(store)
    return store


def test_verify_does_not_warn_about_telegram_ids_when_telegram_is_not_configured(tmp_path):
    ws, doc = _ws(tmp_path)
    out = oc._verify_workspace_blocks(doc, state.OpenClawState(), F)
    assert not [f for f in out if "documents routing" in f.message]


def test_verify_still_warns_when_telegram_is_configured_with_other_groups(tmp_path):
    ws, doc = _ws(tmp_path)
    doc["channels"] = {"telegram": {"enabled": True, "groups": {"-100999999999": {}}}}
    out = oc._verify_workspace_blocks(doc, state.OpenClawState(), F)
    assert [f for f in out if "documents routing" in f.message and "-5217865131" in f.message]


def test_auto_leaves_operator_lines_and_lists_the_skip(monkeypatch, tmp_path):
    _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    Script().install(monkeypatch, non_interactive=True)
    s = state.SetupState()
    assert oc._offer_stale_telegram_cleanup(s, doc, dry_run=False) is False
    assert STALE in (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert any("Telegram" in what for what, _, _ in ui.skipped())


def test_ask_removes_the_lines_with_a_backup_and_remembers(monkeypatch, tmp_path):
    store = _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    script = Script(**{"documents": True})
    script.install(monkeypatch)
    s = state.SetupState()
    assert oc._offer_stale_telegram_cleanup(s, doc, dry_run=False) is True
    text = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "-5217865131" not in text and "keep this line" in text
    assert list(ws.glob("AGENTS.md.ai-resources-backup-*"))
    assert store
    assert oc._offer_stale_telegram_cleanup(s, doc, dry_run=False) is False      # nothing left, nothing asked


def test_a_declined_cleanup_is_not_asked_again(monkeypatch, tmp_path):
    store = _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda m, **k: asked.append(m) or False)
    s = state.SetupState()
    oc._offer_stale_telegram_cleanup(s, doc, dry_run=False)
    oc._offer_stale_telegram_cleanup(s, doc, dry_run=False)
    assert len(asked) == 1 and STALE in (ws / "AGENTS.md").read_text(encoding="utf-8")


def test_the_kit_checkout_is_never_cleaned(monkeypatch, tmp_path):
    _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    monkeypatch.setattr(oc._shared, "kit_checkout_root", lambda p: p)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert oc._offer_stale_telegram_cleanup(state.SetupState(), doc, dry_run=False) is False


def test_a_symlinked_agents_md_is_never_cleaned(monkeypatch, tmp_path):
    _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    real = tmp_path / "elsewhere.md"
    real.write_text("# Mine\n" + STALE, encoding="utf-8")
    (ws / "AGENTS.md").unlink()
    (ws / "AGENTS.md").symlink_to(real)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert oc._offer_stale_telegram_cleanup(state.SetupState(), doc, dry_run=False) is False
    assert (ws / "AGENTS.md").is_symlink() and STALE in real.read_text(encoding="utf-8")


def test_a_file_that_resolves_into_a_kit_checkout_is_never_cleaned(monkeypatch, tmp_path):
    _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    seen = []
    monkeypatch.setattr(oc._shared, "kit_checkout_root",
                        lambda p: seen.append(p) or (p if str(p).endswith("AGENTS.md") else None))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert oc._offer_stale_telegram_cleanup(state.SetupState(), doc, dry_run=False) is False
    assert any(str(p).endswith("AGENTS.md") for p in seen)


def test_the_cleanup_keeps_the_mode_and_writes_atomically(monkeypatch, tmp_path):
    _fresh_ui(monkeypatch)
    ws, doc = _ws(tmp_path)
    md = ws / "AGENTS.md"
    md.chmod(0o640)
    Script(**{"documents": True}).install(monkeypatch)
    assert oc._offer_stale_telegram_cleanup(state.SetupState(), doc, dry_run=False) is True
    assert (md.stat().st_mode & 0o777) == 0o640
    [bak] = ws.glob("AGENTS.md.ai-resources-backup-*")
    assert (bak.stat().st_mode & 0o777) == 0o640
    assert not list(ws.glob("*.tmp*")) and not list(ws.glob(".AGENTS.md.*"))


# --- 5(c) -------------------------------------------------------------------------------------------------------------

def _applied_state():
    s = state.SetupState()
    s.openclaw.applied = True
    s.openclaw.previous = {"model": "x"}
    return s


def test_restore_engine_is_asked_once_and_a_no_is_remembered(monkeypatch):
    _fresh_ui(monkeypatch)
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda m, **k: asked.append(m) or False)
    s = _applied_state()
    assert oc._ask_restore_engine(s) is False
    assert oc._ask_restore_engine(s) is False
    assert len(asked) == 1


def test_restore_engine_is_asked_again_when_what_it_replaced_changes(monkeypatch):
    _fresh_ui(monkeypatch)
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda m, **k: asked.append(m) or False)
    s = _applied_state()
    oc._ask_restore_engine(s)
    s.openclaw.previous = {"model": "y"}
    oc._ask_restore_engine(s)
    assert len(asked) == 2


def test_restore_engine_auto_never_asks_and_records_the_skip(monkeypatch):
    _fresh_ui(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert oc._ask_restore_engine(_applied_state()) is False
    assert any("restore the OpenClaw engine" in what for what, _, _ in ui.skipped())
