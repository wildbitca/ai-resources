"""Installing the orchestrator plugin never restarts a live gateway (ADR-0003, ADR-0015 S13, acceptance 15)."""
from __future__ import annotations

import copy
import json

import pytest

from ai_resources import openclaw_host as host
from ai_resources.setup import cockpit_auth, state, ui
from ai_resources.setup.cockpits import _openclaw_host as section
from ai_resources.setup.cockpits import openclaw as oc


class Fake:
    def __init__(self, doc=None):
        self.doc = doc or {}
        self.cli: list[list[str]] = []
        self.patches: list[dict] = []
        self.restarts = 0

    def openclaw(self, args, stdin=None, timeout=120):
        self.cli.append(list(args))
        return 0, "ok"

    def apply_hot(self, patch, dry_run=False, replace_paths=None):
        if not dry_run:
            self.patches.append(copy.deepcopy(patch))
            cfg = patch["plugins"]["entries"][oc.PLUGIN_ID]["config"]
            self.doc.setdefault("plugins", {}).setdefault("entries", {}).setdefault(oc.PLUGIN_ID, {}).setdefault("config", {}).update(cfg)
        return True, "ok"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake()
    monkeypatch.setattr(oc, "_openclaw", f.openclaw)
    monkeypatch.setattr(oc, "_apply_hot", f.apply_hot)
    monkeypatch.setattr(oc.openclaw_host, "change_records", lambda *a, **k: [])
    monkeypatch.setattr(oc, "restart_gateway", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a restart was issued")))
    monkeypatch.setattr(host, "restart_gateway", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a restart was issued")),
                        raising=False)
    kit = tmp_path / "kit"
    (kit / "openclaw-plugin" / "ai-resources").mkdir(parents=True)
    (kit / "openclaw-plugin" / "ai-resources" / "package.json").write_text(json.dumps({"version": "1.1.0"}))
    f.kit = str(kit)
    return f


def run(s, fake, *, dry_run=False):
    from pathlib import Path
    return oc._configure_orchestrator_plugin({}, s, fake.doc, Path(fake.kit) / "openclaw.json", fake.kit, dry_run=dry_run,
                                             run_changes=[])


def chosen():
    s = state.SetupState()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    return s


def test_the_plugin_version_is_read_from_package_json(fake):
    assert oc.plugin_version(fake.kit) == "1.1.0"
    assert oc.plugin_version("/nonexistent") == ""


def test_a_new_install_links_the_plugin_fills_its_config_and_records_exactly_one_pending_entry(fake):
    s = chosen()
    assert run(s, fake) is True
    assert fake.cli == [["plugins", "install", "--link", "--force", "--accept-capabilities",
                         fake.kit + "/openclaw-plugin/ai-resources"]]
    cfg = fake.doc["plugins"]["entries"][oc.PLUGIN_ID]["config"]
    assert cfg["enableOrchestrator"] is True and cfg["maxTeams"] == 2 and cfg["kitRoot"] == fake.kit
    pending = [e for e in s.openclaw.host_restart_pending if e["op"] == "plugin"]
    assert len(pending) == 1 and pending[0]["version"] == "1.1.0" and pending[0]["path"] == "plugins.entries.ai-resources"
    assert s.openclaw.plugin_version_seen == "1.1.0" and s.openclaw.plugin_linked is True
    assert not any("restart" in " ".join(c) for c in fake.cli)                    # no gateway call of any kind


def test_a_second_run_adds_no_entry_and_changes_nothing(fake):
    s = chosen()
    run(s, fake)
    before = (copy.deepcopy(s.openclaw.host_restart_pending), len(fake.cli), len(fake.patches))
    assert run(s, fake) is False
    assert (s.openclaw.host_restart_pending, len(fake.cli), len(fake.patches)) == before


def test_a_new_plugin_version_replaces_the_entry_instead_of_stacking(fake):
    s = chosen()
    run(s, fake)
    (__import__("pathlib").Path(fake.kit) / "openclaw-plugin" / "ai-resources" / "package.json").write_text(
        json.dumps({"version": "1.2.0"}))
    assert run(s, fake) is True
    pending = [e for e in s.openclaw.host_restart_pending if e["op"] == "plugin"]
    assert len(pending) == 1 and pending[0]["version"] == "1.2.0"
    assert len(fake.cli) == 1                                                       # already linked: no second install


def test_operator_config_values_are_kept(fake):
    fake.doc = {"plugins": {"entries": {oc.PLUGIN_ID: {"config": {"maxTeams": 5, "kitRoot": "/mine"}}}}}
    s = chosen()
    run(s, fake)
    cfg = fake.doc["plugins"]["entries"][oc.PLUGIN_ID]["config"]
    assert cfg["maxTeams"] == 5 and cfg["kitRoot"] == "/mine" and cfg["enableOrchestrator"] is True


def test_without_a_provider_answer_nothing_is_installed(fake):
    assert run(state.SetupState(), fake) is False
    assert fake.cli == [] and fake.patches == []


def test_a_dry_run_writes_nothing(fake):
    s = chosen()
    assert run(s, fake, dry_run=True) is False
    assert fake.cli == [] and fake.patches == [] and s.openclaw.host_restart_pending == []


def test_an_antigravity_engine_keeps_its_own_guarded_path_and_only_remembers_the_version(fake):
    s = chosen()
    s.openclaw.antigravity_applied = True
    assert run(s, fake) is False
    assert fake.cli == [] and s.openclaw.host_restart_pending == [] and s.openclaw.plugin_version_seen == "1.1.0"


def test_a_failed_link_is_reported_and_records_nothing(fake, monkeypatch):
    monkeypatch.setattr(oc, "_openclaw", lambda *a, **k: (1, "boom"))
    s = chosen()
    assert run(s, fake) is False
    assert s.openclaw.host_restart_pending == [] and s.openclaw.plugin_linked is False


def test_the_state_record_round_trips_and_is_additive():
    s = state.SetupState()
    assert "plugin_version_seen" not in state._to_dict(s)["openclaw"]
    s.openclaw.plugin_version_seen = "1.1.0"
    assert state._to_dict(s)["openclaw"]["plugin_version_seen"] == "1.1.0"


def test_apply_pending_reloads_the_plugin_in_one_drained_window_and_clears_the_entry(fake, monkeypatch):
    s = chosen()
    run(s, fake)
    windows = []
    monkeypatch.setattr(section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(oc, "read_config", lambda p: fake.doc)
    monkeypatch.setattr(oc, "apply_patch", lambda patch, dry_run=False, replace_paths=None, allow_restart=False: (True, "ok"))
    monkeypatch.setattr(section, "_drained_apply", lambda patch, rp, ap: windows.append((patch, rp)) or (True, True))
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    assert section.apply_pending(s, assume_yes=True) == 0
    assert windows == [({}, [])]                                                    # one window, no config patch
    assert s.openclaw.host_restart_pending == []


def test_apply_pending_refuses_while_runs_are_in_flight_and_keeps_the_entry(fake, monkeypatch):
    s = chosen()
    run(s, fake)
    monkeypatch.setattr(section, "_in_flight", lambda: (2, "2"))
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(ui, "warn", lambda *a, **k: None)
    assert section.apply_pending(s, assume_yes=True) == 1
    assert len([e for e in s.openclaw.host_restart_pending if e["op"] == "plugin"]) == 1
