"""The retired-components reconciler: OpenRouter, LiteLLM and Telegram artifacts leave when nothing uses them."""
from __future__ import annotations

import json

import pytest

from ai_resources import retired
from ai_resources.setup import state


def _doc(**extra):
    doc = {
        "auth": {"profiles": {"anthropic:default": {"provider": "anthropic"},
                              "openrouter:default": {"provider": "openrouter", "mode": "api_key"}}},
        "plugins": {"entries": {"openrouter": {"enabled": True}, "litellm": {"enabled": True}, "slack": {"enabled": True}}},
        "channels": {"slack": {"enabled": True}, "telegram": {"enabled": True}},
        "commands": {"ownerAllowFrom": ["slack:U1", "telegram:42"]},
        "bindings": [
            {"agentId": "main", "comment": "catch-all", "match": {"channel": "slack", "accountId": "*"}},
            {"agentId": "a", "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-1"}}},
            {"agentId": "b", "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C1"}}},
        ],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".openclaw").mkdir()
    (tmp_path / ".openclaw" / "telegram.token").write_text("123:SECRET\n")
    (tmp_path / ".openclaw" / "kit-host.env").write_text(
        "OPENCLAW_BACKUP_DIR=/srv/b\nOPENCLAW_OWNER_TELEGRAM_ID=42\n")
    (tmp_path / ".config" / "ai-resources").mkdir(parents=True)
    (tmp_path / ".config" / "ai-resources" / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-SECRET\nGEMINI_API_KEY=keep\nLITELLM_MASTER_KEY=sk-lite\n")
    return tmp_path


def _s(**kw):
    s = state.SetupState()
    s.mode, s.cli_team = "single-model", True
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class Patcher:
    def __init__(self, doc):
        self.doc, self.calls = doc, []

    def __call__(self, patch, *, dry_run=False, replace_paths=None):
        self.calls.append((patch, dry_run, replace_paths))
        if not dry_run:
            from ai_resources.models import apply_in_memory
            self.doc = apply_in_memory(self.doc, patch)
        return True, "ok"


def test_plan_lists_every_artifact_of_the_three_components(home):
    p = retired.plan(_doc(), _s(), home=home)
    kinds = {(a.component, a.kind) for a in p.actions}
    assert ("openrouter", "config") in kinds and ("litellm", "config") in kinds and ("telegram", "config") in kinds
    assert ("telegram", "bindings") in kinds and ("telegram", "owner") in kinds and ("telegram", "file") in kinds
    assert ("openrouter", "env") in kinds and ("litellm", "env") in kinds and ("telegram", "env") in kinds
    assert ("litellm", "state") in kinds
    assert p.patch["channels"] == {"telegram": None}
    assert p.patch["auth"]["profiles"]["openrouter:default"] is None
    assert p.patch["commands"]["ownerAllowFrom"] == ["slack:U1"]
    assert [b["agentId"] for b in p.patch["bindings"]] == ["b", "main"]       # telegram route gone, catch-all LAST
    assert sorted(p.replace_paths) == ["bindings", "commands.ownerAllowFrom"]


def test_dry_run_writes_nothing(home):
    s, before = _s(), (home / ".config" / "ai-resources" / ".env").read_text()
    res = retired.apply(retired.plan(_doc(), s, home=home), s, apply_patch=Patcher(_doc()), dry_run=True, home=home)
    assert res.dry_run and res.removed
    assert (home / ".config" / "ai-resources" / ".env").read_text() == before
    assert (home / ".openclaw" / "telegram.token").exists() and s.backend == "litellm"
    assert not (home / ".config" / "ai-resources" / "backups").exists()


def test_apply_backs_up_removes_and_second_pass_is_empty(home):
    doc, s = _doc(), _s()
    patcher = Patcher(doc)
    res = retired.apply(retired.plan(doc, s, home=home), s, apply_patch=patcher, home=home, stamp="t1")
    assert not res.failed and not res.pending
    backup = home / ".config" / "ai-resources" / "backups" / "retired-t1"
    removed = json.loads((backup / "openclaw-removed.json").read_text())
    assert "channels.telegram" in removed and len(removed["bindings"]) == 1
    assert (backup / "files" / "telegram.token").read_text().startswith("123:")
    assert oct((backup / "files" / "telegram.token").stat().st_mode & 0o777) == "0o600"
    assert not (home / ".openclaw" / "telegram.token").exists()
    assert (home / ".config" / "ai-resources" / ".env").read_text() == "GEMINI_API_KEY=keep\n"
    assert "SECRET" not in "".join(p.read_text() for p in backup.rglob("*.removed-keys"))
    host_env = (home / ".openclaw" / "kit-host.env").read_text()
    assert "OPENCLAW_OWNER_TELEGRAM_ID" not in host_env and "OPENCLAW_BACKUP_DIR=/srv/b" in host_env
    assert s.backend == "cli"
    assert "telegram" not in patcher.doc["channels"] and "openrouter:default" not in patcher.doc["auth"]["profiles"]
    assert retired.plan(patcher.doc, s, home=home).empty


def test_components_in_use_are_kept(home):
    doc = _doc(channels={"telegram": {"enabled": True, "botToken": "x"}},
               agents={"entries": {"a": {"model": {"primary": "openrouter/x/y"}}}})
    s = _s(mode="multi-model", backend="litellm")
    p = retired.plan(doc, s, home=home)
    assert set(p.kept) == {"openrouter", "litellm", "telegram"}
    assert not any(a.kind in ("config", "env", "file", "state") for a in p.actions)


def test_a_telegram_token_in_the_gateway_env_keeps_the_stub(home):
    (home / ".openclaw" / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\n")
    assert "telegram" in retired.plan(_doc(), _s(), home=home).kept


def test_misordered_catch_all_is_moved_last_even_with_nothing_retired(home):
    doc = {"bindings": [{"agentId": "main", "match": {"channel": "slack"}},
                        {"agentId": "b", "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C"}}}]}
    p = retired.plan(doc, _s(), home=home)
    assert [a.kind for a in p.actions if a.component == "bindings"] == ["reorder"]
    assert [b["agentId"] for b in p.patch["bindings"]] == ["b", "main"]


def test_guard_refuses_anything_outside_the_list():
    for patch, rp in (({"channels": {"slack": None}}, []), ({"plugins": {"entries": {"slack": None}}}, []),
                      ({"gateway": {"bind": "lan"}}, []), ({"agents": {}}, ["agents"]),
                      ({"bindings": [{"match": {"channel": "slack"}}, {"match": {"peer": {"id": "1"}}}]}, ["bindings"])):
        with pytest.raises(ValueError):
            retired.assert_patch_safe(patch, rp)
    retired.assert_patch_safe({"channels": {"telegram": None}}, [])


def test_a_restart_required_leaf_is_pending_not_failed(home):
    class NeedsRestart(RuntimeError):
        pass

    def patcher(patch, *, dry_run=False, replace_paths=None):
        if not dry_run:
            raise NeedsRestart("channels.telegram")
        return True, "ok"
    s = _s()
    res = retired.apply(retired.plan(_doc(), s, home=home), s, apply_patch=patcher, home=home, stamp="t2",
                        restart_error=NeedsRestart)
    assert res.pending and not res.failed
    assert any("telegram" in line for line in retired.report(res))
