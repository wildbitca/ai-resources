"""R5/R7 host helpers: the PATH shim never answers for the install, MemoryHigh follows the host, the off-box
credential is regenerated 0600 and its uploader validated, stdio MCP servers are denied to the team roles."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_reload_rules as rr  # noqa: E402
from ai_resources.setup import detection  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_mcp as mcp  # noqa: E402


# --- the shim (R7-2) ---------------------------------------------------------------------------------------------

def _install(tmp_path):
    pkg = tmp_path / "brew" / "lib" / "node_modules" / "openclaw"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "index.js").write_text("")
    (pkg / "openclaw.mjs").write_text("")
    os.chmod(pkg / "openclaw.mjs", 0o755)
    (pkg / "package.json").write_text('{"name": "openclaw", "version": "2026.10.7"}')
    real_bin = tmp_path / "brew" / "bin"
    real_bin.mkdir()
    (real_bin / "openclaw").symlink_to(pkg / "openclaw.mjs")
    shim_dir = tmp_path / "home" / ".openclaw" / "tmp" / "agent-cli"
    shim_dir.mkdir(parents=True)
    shim = shim_dir / "openclaw"
    shim.write_text(f"#!/bin/sh\nset -eu\n\nexec /usr/bin/node --max-old-space-size=8192 {pkg}/dist/index.js \"$@\"\n")
    os.chmod(shim, 0o700)
    return pkg, real_bin, shim


def test_the_shim_is_recognised_by_where_it_lives(tmp_path):
    _, real_bin, shim = _install(tmp_path)
    assert host.is_openclaw_shim(shim) and not host.is_openclaw_shim(real_bin / "openclaw")


def test_the_shim_names_the_package_it_executes(tmp_path):
    pkg, _, shim = _install(tmp_path)
    assert host.shim_target_package(shim) == pkg
    shim.write_text("#!/bin/sh\necho nothing\n")
    assert host.shim_target_package(shim) is None


def test_real_candidates_skip_the_shim_even_when_it_is_first_on_path(tmp_path):
    _, real_bin, shim = _install(tmp_path)
    path = f"{shim.parent}{os.pathsep}{real_bin}"
    found = host.real_openclaw_candidates(path)
    assert str(real_bin / "openclaw") in found and str(shim) not in found


def test_a_unit_is_never_pointed_at_the_shim(tmp_path, monkeypatch):
    _, real_bin, shim = _install(tmp_path)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{real_bin}")
    assert host.resolve_openclaw_bin() == str(real_bin / "openclaw")


def test_reload_rules_are_located_through_the_shim(tmp_path, monkeypatch):
    pkg, real_bin, shim = _install(tmp_path)
    monkeypatch.setattr(detection, "_extra_bin_dirs", lambda: [])
    monkeypatch.setattr(detection, "_which_extra", lambda name: str(shim))
    assert rr.locate_package_json() == pkg / "package.json"


def test_with_the_shim_target_gone_the_real_install_on_path_answers(tmp_path, monkeypatch):
    pkg, real_bin, shim = _install(tmp_path)
    shim.write_text("#!/bin/sh\nexec /nonexistent/openclaw.mjs\n")
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{real_bin}")
    monkeypatch.setattr(detection, "_which_extra", lambda name: str(shim))
    assert rr.locate_package_json().resolve() == (pkg / "package.json").resolve()


def test_nothing_readable_falls_back_safely_and_says_why(tmp_path, monkeypatch):
    _, _, shim = _install(tmp_path)
    shim.write_text("#!/bin/sh\nexec /nonexistent/openclaw.mjs\n")
    monkeypatch.setenv("PATH", str(shim.parent))
    monkeypatch.delenv("HOMEBREW_PREFIX", raising=False)
    monkeypatch.setattr(rr, "BREW_PREFIXES", (str(tmp_path / "nowhere"),))
    monkeypatch.setattr(host, "BREW_OPENCLAW_FALLBACKS", ())
    monkeypatch.setattr(detection, "_which_extra", lambda name: str(shim))
    assert rr.locate_package_json() is None
    dist, why = rr._locate_dist(rr.locate_package_json())
    assert dist is None and "shim" in why         # every key is then restart-required, and the reason names the shim


# --- MemoryHigh follows the host (R7-6) ---------------------------------------------------------------------------

@pytest.mark.parametrize("gib,want", [(8, "12G"), (16, "12G"), (22, "12G"), (30, "16G"), (31.5, "17G"),
                                       (64, "24G"), (256, "24G")])
def test_recommended_memory_high_scales_with_ram_between_12_and_24(gib, want):
    assert host.recommended_memory_high(int(gib * 1024 ** 3)) == want


def test_an_unreadable_meminfo_keeps_the_old_12g():
    assert host.recommended_memory_high(0) == "12G"


def test_the_host_env_overrides_and_garbage_is_ignored():
    assert host.wanted_memory_high({"OPENCLAW_MEMORY_HIGH": "20g"}, 8 * 1024 ** 3) == "20G"
    assert host.wanted_memory_high({"OPENCLAW_MEMORY_HIGH": "lots"}, 30 * 1024 ** 3) == "16G"


def test_lane_headroom_is_explainable():
    assert host.lane_headroom("12G") == 4 and host.lane_headroom("16G") == 6 and host.lane_headroom("2G") == 0


# --- the off-box credential and its uploader (R7-1) ---------------------------------------------------------------

def _wif_source(tmp_path):
    src = tmp_path / "agent-wif" / "cred-config.json"
    src.parent.mkdir()
    src.write_text(json.dumps({"type": "external_account", "audience": "//iam/x", "token_url": "https://sts/x",
                               "subject_token_type": "urn:t", "credential_source": {"executable": {"command": "/x.sh"}},
                               "service_account_impersonation_url": "https://old"}))
    return src


def test_the_credential_is_regenerated_0600_from_the_host_federation(tmp_path):
    src, target = _wif_source(tmp_path), tmp_path / "cfg" / "offbox-wif-cred.json"
    sa = "k3s-backup@wildbit-iac.iam.gserviceaccount.com"
    assert host.offbox_wif_cred(sa, env={}, source=src, target=target, dry_run=True) == ("written", target)
    assert not target.exists()                               # a dry run writes nothing
    assert host.offbox_wif_cred(sa, env={}, source=src, target=target) == ("written", target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sa in json.loads(target.read_text())["service_account_impersonation_url"]
    assert host.offbox_wif_cred(sa, env={}, source=src, target=target) == ("same", target)     # idempotent


def test_no_federation_writes_nothing(tmp_path):
    assert host.offbox_wif_cred("a@b.iam.gserviceaccount.com", env={}, source=tmp_path / "none.json",
                                target=tmp_path / "t.json")[0] == "absent"


class UploaderRunner:
    def __init__(self, state_file, report=None, unit=True):
        self.calls, self.state_file, self.report, self.unit = [], state_file, report, unit

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "cat"]:
            return (0, "") if self.unit else (1, "no unit")
        if argv[:3] == ["systemctl", "--user", "start"] and self.report:
            self.state_file.write_text(json.dumps(self.report))
        return 0, ""


def _state(tmp_path, monkeypatch):
    f = tmp_path / ".openclaw" / "state" / "offbox-last.json"
    f.parent.mkdir(parents=True)
    monkeypatch.setattr(host, "read_host_env", lambda path=None: {})
    return f


def test_the_uploader_is_started_without_blocking_and_its_ok_report_is_read(tmp_path, monkeypatch):
    f = _state(tmp_path, monkeypatch)
    now = 1_800_000_000.0
    import datetime as dt
    at = dt.datetime.fromtimestamp(now + 1, dt.timezone.utc).isoformat()
    runner = UploaderRunner(f, {"outcome": "ok", "at": at, "newest_offbox_epoch": now, "identity": "wif"})
    status, detail = host.offbox_uploader_run(runner, home=tmp_path, sleep=lambda s: None, clock=lambda: now)
    assert status == "ok"
    assert ["systemctl", "--user", "start", "--no-block", host.OFFBOX_UPLOADER_UNIT] in runner.calls


def test_a_failed_upload_is_reported_as_failed(tmp_path, monkeypatch):
    f = _state(tmp_path, monkeypatch)
    now = 1_800_000_000.0
    import datetime as dt
    at = dt.datetime.fromtimestamp(now + 1, dt.timezone.utc).isoformat()
    runner = UploaderRunner(f, {"outcome": "fail", "at": at, "reason": "denied", "newest_offbox_epoch": now})
    assert host.offbox_uploader_run(runner, home=tmp_path, sleep=lambda s: None, clock=lambda: now)[0] == "failed"


def test_no_report_in_time_is_pending_not_a_claim(tmp_path, monkeypatch):
    f = _state(tmp_path, monkeypatch)
    ticks = iter(range(0, 10_000, 20))
    status, _ = host.offbox_uploader_run(UploaderRunner(f), home=tmp_path, sleep=lambda s: None,
                                         clock=lambda: float(next(ticks)))
    assert status == "pending"


def test_a_missing_uploader_unit_is_absent(tmp_path, monkeypatch):
    f = _state(tmp_path, monkeypatch)
    assert host.offbox_uploader_run(UploaderRunner(f, unit=False), home=tmp_path)[0] == "absent"


# --- stdio MCP servers are denied to the team roles (R7-4) --------------------------------------------------------

DOC = {"mcp": {"servers": {"engram": {"command": "/b/engram"}, "supabase-admin": {"command": "npx"},
                           "clickup": {"url": "https://mcp.clickup.com/mcp"}}},
       "agents": {"entries": {"main": {}, "claude": {}, "kit-role-planner": {}, "kit-role-tester": {"tools": {"deny": ["write"]}},
                              "kit-lane-cli-opus55": {"tools": {"deny": ["bundle-mcp"]}}}}}


def test_only_process_spawning_servers_are_scoped():
    assert mcp.stdio_servers(DOC) == ["engram", "supabase-admin"]


def test_only_the_roles_lose_them_by_default_and_lanes_and_personas_are_left_alone():
    scope = mcp.role_scope(DOC, ["kit-role-*"])
    assert sorted(scope) == ["kit-role-planner", "kit-role-tester"]
    assert scope["kit-role-planner"] == ["engram__*", "supabase-admin__*"]


def test_the_patch_keeps_an_operator_deny_and_a_second_pass_is_a_no_op():
    scope = mcp.role_scope(DOC, ["kit-role-*"])
    patch = mcp.scope_patch(DOC, [], {}, scope)
    assert patch["agents"]["entries"]["kit-role-tester"]["tools"]["deny"] == ["write", "engram__*", "supabase-admin__*"]
    applied = json.loads(json.dumps(DOC))
    for aid, ent in patch["agents"]["entries"].items():
        applied["agents"]["entries"][aid]["tools"] = {"deny": ent["tools"]["deny"]}
    assert mcp.scope_patch(applied, [], {}, mcp.role_scope(applied, ["kit-role-*"])) == {}


def test_the_deny_globs_come_from_the_host_env():
    from ai_resources import openclaw_reconcile as rec
    assert rec.deny_agent_globs({}) == ["kit-role-*"]
    assert rec.deny_agent_globs({"OPENCLAW_MCP_DENY_AGENTS": "kit-role-*, snoutzone"}) == ["kit-role-*", "snoutzone"]
