"""A gateway that started before the installed kit must be reported stale.

`brew upgrade` leaves the plugin link on the version-independent opt/ path, so the runtime root
and the expected plugin dir compare equal afterwards and the path rule alone can never notice
the gateway is still running the previous version's code (measured 2026-09-29, 1.10.2 -> 1.11.0).
The time rule compares the unit's start timestamp with the mtime of the resolved plugin root.

Everything runs against fake runners and trees under tmp_path: no real systemctl, no gateway,
nothing under ~/.openclaw. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import argparse
import calendar
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import doctor  # noqa: E402
from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import openclaw, _shared  # noqa: E402

# The two moments measured on this host: the gateway unit started, then the kit was upgraded.
STARTED_EPOCH = calendar.timegm((2026, 9, 28, 20, 49, 26))
GATEWAY_STARTED = f"@{STARTED_EPOCH}"   # `--timestamp=unix`: numeric, so no zone or DST to guess
KIT_INSTALLED_EPOCH = calendar.timegm((2026, 9, 29, 14, 41, 4))


def fake_show(value: str = GATEWAY_STARTED, rc: int = 0, seen: list | None = None):
    """A runner that answers `systemctl --user show <unit> -p ActiveEnterTimestamp --value --timestamp=unix`."""
    def runner(argv, *, env=None, timeout=None, **_kw):
        if seen is not None:
            seen.append((argv, env))
        assert argv[:3] == ["systemctl", "--user", "show"], argv
        return rc, value + "\n"
    return runner


def plugin_tree(tmp_path: pathlib.Path, mtime: float) -> pathlib.Path:
    """A kit whose ROOT was installed at `mtime`. The plugin dir and its files keep an old source
    mtime, as brew leaves them: only the install root carries the moment of installation."""
    root = tmp_path / "kit"
    plugin = root / "openclaw-plugin" / "ai-resources"
    plugin.mkdir(parents=True)
    (plugin / "index.js").write_text("//", encoding="utf-8")
    old = STARTED_EPOCH - 30 * 86400
    for d in (plugin / "index.js", plugin, plugin.parent):
        os.utime(d, (old, old))
    os.utime(root, (mtime, mtime))
    return plugin


def runtime_says(monkeypatch, root: pathlib.Path | str):
    monkeypatch.setattr(openclaw, "plugin_runtime", lambda: {"status": "loaded", "rootDir": str(root)})


# --- the probe --------------------------------------------------------------------------------

def test_gateway_started_at_reads_the_unit_start_time_through_the_shared_unit_and_env():
    seen: list = []
    assert host.gateway_started_at(fake_show(seen=seen)) == STARTED_EPOCH
    argv, env = seen[0]
    assert argv == ["systemctl", "--user", "show", host.GATEWAY_UNIT, "-p", "ActiveEnterTimestamp", "--value",
                    "--timestamp=unix"]
    assert env is not None and "XDG_RUNTIME_DIR" in env   # systemd_env(), like the busy probe


@pytest.mark.parametrize("value, rc", [("", 0), ("n/a", 0), ("garbage in here now", 0), ("@", 0), ("@notanumber", 0),
                                      (GATEWAY_STARTED, 1),
                                      # a systemd without `--timestamp=` fails; one that ignores it prints
                                      # the localised form, which is never parsed (DST-ambiguous)
                                      ("Invalid value: unix.", 1), ("Mon 2026-09-28 20:49:26 UTC", 0),
                                      ("Mon 2026-11-01 01:30:00 CET", 0)])
def test_gateway_started_at_is_none_when_the_answer_cannot_be_trusted(value, rc):
    assert host.gateway_started_at(fake_show(value, rc)) is None


def test_gateway_started_at_is_none_when_the_runner_raises():
    def boom(*_a, **_k):
        raise OSError("no systemctl")
    assert host.gateway_started_at(boom) is None


# --- the rule (AC 1-4) ------------------------------------------------------------------------

def test_ac1_a_gateway_started_before_the_installed_kit_is_stale_with_the_path_unchanged(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    runtime_says(monkeypatch, plugin)   # same path on both sides, exactly the post-`brew upgrade` shape
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is True


def test_ac2_a_gateway_started_after_the_installed_kit_is_not_stale(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is False


def test_ac3_the_path_mismatch_rule_still_reports_stale_even_when_the_gateway_is_newer(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
    old = tmp_path / "old" / "openclaw-plugin" / "ai-resources"
    old.mkdir(parents=True)
    runtime_says(monkeypatch, old)
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is True


@pytest.mark.parametrize("runner", [fake_show("", 0), fake_show(GATEWAY_STARTED, 1), fake_show("n/a")])
def test_ac4_an_unreadable_start_time_falls_back_to_the_path_rule(monkeypatch, tmp_path, runner):
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(plugin), runner=runner) is False


def test_ac4_an_unreadable_mtime_falls_back_to_the_path_rule(monkeypatch, tmp_path):
    missing = tmp_path / "gone" / "openclaw-plugin" / "ai-resources"
    runtime_says(monkeypatch, missing)
    assert openclaw.plugin_is_stale(str(missing), runner=fake_show()) is False


def test_the_time_rule_needs_a_runtime_root_at_all(monkeypatch, tmp_path):
    """No answer from `plugins inspect` means the gateway is not running the plugin: that is the
    other doctor branch's business, not staleness."""
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    monkeypatch.setattr(openclaw, "plugin_runtime", lambda: {})
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is False


def test_the_mtime_is_read_from_the_resolved_root_not_the_opt_symlink(monkeypatch, tmp_path):
    """The opt/ link itself is recreated by brew on every upgrade; only the target says how old the
    installed code is."""
    real = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    link = tmp_path / "opt-link"
    link.symlink_to(real)
    os.utime(link, (STARTED_EPOCH - 3600, STARTED_EPOCH - 3600), follow_symlinks=False)
    runtime_says(monkeypatch, real)
    assert openclaw.plugin_is_stale(str(link), runner=fake_show()) is True


# --- AC 5: the doctor warning fires -----------------------------------------------------------

class _FakeConsole:
    def print(self, *_a, **_k):
        pass

    def rule(self, *_a, **_k):
        pass


def test_ac5_the_doctor_tells_the_operator_to_restart_when_only_the_time_rule_trips(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    runtime_says(monkeypatch, plugin)
    monkeypatch.setattr(openclaw, "_gateway_started_at", lambda: STARTED_EPOCH)
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: plugin.parents[1])
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda _s: 0)
    s = state.SetupState()
    s.openclaw.antigravity_applied = True
    s.openclaw.plugin_linked = True   # the default answer gates on the link
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    monkeypatch.setattr(state, "load", lambda: s)
    lines: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: lines.append(msg))
    monkeypatch.setattr(ui, "detail", lambda msg: lines.append(msg))
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    assert "OpenClaw is running older ai-resources plugin code than the kit on disk (restart to load it)." in lines
    assert not any("older kit directory" in ln for ln in lines)
    assert "Run: openclaw gateway restart" in lines


def test_the_doctor_words_the_path_rule_as_an_older_kit_directory(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
    old = tmp_path / "old" / "openclaw-plugin" / "ai-resources"
    old.mkdir(parents=True)
    runtime_says(monkeypatch, old)
    monkeypatch.setattr(openclaw, "_gateway_started_at", lambda: STARTED_EPOCH)
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: plugin.parents[1])
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda _s: 0)
    s = state.SetupState()
    s.openclaw.antigravity_applied = True
    s.openclaw.plugin_linked = True   # the default answer gates on the link
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    monkeypatch.setattr(state, "load", lambda: s)
    lines: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: lines.append(msg))
    monkeypatch.setattr(ui, "detail", lambda msg: lines.append(msg))
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    assert "OpenClaw is running the ai-resources plugin from an older kit directory." in lines
    assert not any("kit on disk" in ln for ln in lines)


def test_stale_reason_names_the_rule_that_tripped(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    runtime_says(monkeypatch, plugin)
    assert openclaw.stale_reason(str(plugin), runner=fake_show()) == "time"
    old = tmp_path / "old" / "openclaw-plugin" / "ai-resources"
    old.mkdir(parents=True)
    runtime_says(monkeypatch, old)
    assert openclaw.stale_reason(str(plugin), runner=fake_show()) == "path"   # path wins when both hold
    runtime_says(monkeypatch, plugin)
    assert openclaw.stale_reason(str(plugin), runner=fake_show("", 0)) is None


def _verify_plugin_messages(monkeypatch, runtime_root, plugin) -> list[str]:
    """The plugin findings `verify` emits, with the config and host sections stubbed out."""
    from ai_resources.setup.cockpits import _openclaw_host
    monkeypatch.setattr(openclaw, "plugin_runtime", lambda: {"status": "loaded", "rootDir": str(runtime_root)})
    monkeypatch.setattr(openclaw, "read_config", lambda _p: {"agents": {}})
    monkeypatch.setattr(openclaw, "_verify_workspace_blocks", lambda *_a, **_k: [])
    monkeypatch.setattr(openclaw, "_verify_identity", lambda *_a, **_k: [])
    monkeypatch.setattr(_openclaw_host, "verify", lambda *_a, **_k: [])
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: plugin.parents[1])
    s = state.SetupState()
    s.openclaw.plugin_linked = True
    return [f.message for f in openclaw.verify({"state": s, "runner": fake_show()})]


def test_verify_words_the_time_rule_as_older_code_than_the_kit_on_disk(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
    msgs = _verify_plugin_messages(monkeypatch, plugin, plugin)
    assert "the gateway runs older kit plugin code than the kit on disk (restart to load it)" in msgs
    assert not any("older kit directory" in m for m in msgs)


def test_verify_words_the_path_rule_as_an_older_kit_directory(monkeypatch, tmp_path):
    plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
    old = tmp_path / "old" / "openclaw-plugin" / "ai-resources"
    old.mkdir(parents=True)
    msgs = _verify_plugin_messages(monkeypatch, old, plugin)
    assert "the gateway runs the kit plugin from an older kit directory" in msgs
    assert not any("kit on disk" in m for m in msgs)


# --- AC 6: teardown unlinks after a deferral --------------------------------------------------

def test_ac6_teardown_unlinks_the_plugin_when_a_deferral_left_nothing_applied(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda args, stdin=None, timeout=120: (calls.append(args), (0, "ok"))[1])
    s = state.SetupState()
    s.openclaw.plugin_linked = True
    s.openclaw.gateway_restart_pending = {"reason": "busy", "main_pid": "1"}
    removed = openclaw.teardown(s)
    assert ["plugins", "uninstall", openclaw.PLUGIN_ID] in calls
    assert removed == ["plugin:ai-resources"]
    assert s.openclaw.plugin_linked is False
    assert s.openclaw.gateway_restart_pending == {}


def test_ac6_teardown_with_nothing_applied_and_nothing_linked_touches_no_plugin(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda args, stdin=None, timeout=120: (calls.append(args), (0, "ok"))[1])
    assert openclaw.teardown(state.SetupState()) == []
    assert calls == []


# --- AC 7: the dry run shows what a real run writes -------------------------------------------

def test_ac7_the_dry_run_shows_the_stable_path_and_does_not_call_an_unchanged_value_an_update(monkeypatch, tmp_path):
    from ai_resources.setup import credentials, wizard
    from ai_resources.setup.cockpits import claude as claude_cockpit

    cellar = tmp_path / "Cellar" / "ai-resources" / "1.11.0" / "libexec"
    stable = tmp_path / "opt" / "ai-resources" / "libexec"
    cellar.mkdir(parents=True)
    stable.parent.mkdir(parents=True)
    stable.symlink_to(cellar.parent)   # opt/ai-resources -> Cellar/ai-resources/1.11.0
    import ai_resources
    monkeypatch.setattr(ai_resources, "repo_root", lambda: cellar)   # the wizard imports it lazily

    claude_cockpit.SETTINGS_PATH.write_text(
        '{"env": {"AGENT_SKILLS_ROOT": "%s/skills"}}' % stable, encoding="utf-8")
    monkeypatch.setattr(claude_cockpit, "_build_settings_patch",
                        lambda executors, key, url, ak_path, mode, backend="litellm":
                        {"env": {"AGENT_SKILLS_ROOT": f"{ak_path}/skills"}})
    monkeypatch.setattr(credentials, "get_key", lambda *_a, **_k: "fake-key")
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)

    printed: list[str] = []

    class Console(_FakeConsole):
        def print(self, *a, **_k):
            printed.append(" ".join(str(x) for x in a))

    monkeypatch.setattr(ui, "console", lambda: Console())
    s = state.SetupState()
    s.mode = "single-model"
    wizard._step9_dry_run(s)

    row = [ln for ln in printed if "AGENT_SKILLS_ROOT" in ln]
    assert len(row) == 1
    assert str(stable) in row[0] and "Cellar" not in row[0]
    assert "(unchanged)" in row[0] and "(update)" not in row[0]


def test_the_install_root_not_the_plugin_dir_marks_when_the_kit_landed_the_measured_brew_shape(monkeypatch, tmp_path):
    """Measured 2026-09-29: brew preserves source mtimes, so the plugin dir (2026-09-25) and index.js
    (2026-09-17) predate the gateway (2026-09-28) while the kit root and version dir carry the
    upgrade (2026-09-29 14:41). Comparing the plugin dir reports False on the very case the rule
    exists for."""
    version = tmp_path / "Cellar" / "ai-resources" / "1.11.0"
    plugin = version / "libexec" / "openclaw-plugin" / "ai-resources"
    plugin.mkdir(parents=True)
    (plugin / "index.js").write_text("//", encoding="utf-8")
    sep25 = calendar.timegm((2026, 9, 25, 19, 44, 0))
    sep17 = calendar.timegm((2026, 9, 17, 18, 50, 0))
    os.utime(plugin / "index.js", (sep17, sep17))
    os.utime(plugin, (sep25, sep25))
    os.utime(plugin.parent, (sep25, sep25))
    for d in (version / "libexec", version):
        os.utime(d, (KIT_INSTALLED_EPOCH, KIT_INSTALLED_EPOCH))
    assert plugin.stat().st_mtime < STARTED_EPOCH   # what a plugin-dir comparison would have seen
    (tmp_path / "opt" / "ai-resources").parent.mkdir()
    (tmp_path / "opt" / "ai-resources").symlink_to(version)
    stable = tmp_path / "opt" / "ai-resources" / "libexec" / "openclaw-plugin" / "ai-resources"
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(stable), runner=fake_show()) is True


def test_the_version_dir_counts_when_it_is_newer_than_the_kit_root(monkeypatch, tmp_path):
    """Guards the bottle case: tar restores directory mtimes, so libexec carries the build machine's
    time and only the version dir (where brew writes INSTALL_RECEIPT.json) is fresh. Not a duplicate
    of the test above: removing the version-dir branch fails this one alone."""
    version = tmp_path / "Cellar" / "ai-resources" / "1.11.0"
    plugin = version / "libexec" / "openclaw-plugin" / "ai-resources"
    plugin.mkdir(parents=True)
    os.utime(version / "libexec", (STARTED_EPOCH - 86400, STARTED_EPOCH - 86400))
    os.utime(version, (KIT_INSTALLED_EPOCH, KIT_INSTALLED_EPOCH))
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is True


def test_a_poured_bottle_shape_is_stale_libexec_old_from_the_build_machine_version_dir_new(monkeypatch, tmp_path):
    """The common install: everything in the tree, libexec included, carries the build machine's
    time (weeks before the gateway started); only `<version>/` is fresh, from the receipt."""
    version = tmp_path / "Cellar" / "ai-resources" / "1.11.0"
    plugin = version / "libexec" / "openclaw-plugin" / "ai-resources"
    plugin.mkdir(parents=True)
    built = STARTED_EPOCH - 20 * 86400
    for d in (plugin, plugin.parent, version / "libexec"):
        os.utime(d, (built, built))
    os.utime(version, (KIT_INSTALLED_EPOCH, KIT_INSTALLED_EPOCH))
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is True


def test_a_kit_outside_a_cellar_ignores_the_directory_that_contains_it(monkeypatch, tmp_path):
    """A repo checkout's parent directory changes for reasons unrelated to the kit: it must not count."""
    plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
    os.utime(tmp_path, (KIT_INSTALLED_EPOCH, KIT_INSTALLED_EPOCH))
    runtime_says(monkeypatch, plugin)
    assert openclaw.plugin_is_stale(str(plugin), runner=fake_show()) is False
