"""`ai-resources openclaw bootstrap`: eight idempotent steps against a simulated host.

`HostSim` is a fake runner that answers the probes a real host would and applies the mutations
it is asked for, recording every command. Nothing here can reach npm, brew, sudo or systemd.
Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

GOOD_UNIT = """[Service]
ExecStart={node} --max-old-space-size=8192 {root}/openclaw/dist/index.js gateway --port 18789
Environment=PATH=/usr/bin:/bin
"""
MEMORY_12G = str(12 * 1024 ** 3)


class HostSim:
    """A host that is fully correct until a test breaks one thing."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.brew_node = tmp / "brew" / "bin" / "node"
        self.brew_node.parent.mkdir(parents=True)
        self.brew_node.write_text("", encoding="utf-8")
        (self.brew_node.parent / "npm").write_text("", encoding="utf-8")
        self.node_version = "v24.16.1"
        self.root = tmp / "brew" / "lib" / "node_modules"          # `npm root -g`
        self.openclaw_version: str | None = "2026.9.4"
        self.native = True
        self.install_creates_natives = True
        self._make_package(self.root)
        self.unit: str | None = GOOD_UNIT.format(node=self.brew_node, root=self.root)
        self.exec_start_argv = f"{self.brew_node} --max-old-space-size=8192 {self.root}/openclaw/dist/index.js gateway"
        self.linger, self.enabled = "yes", "enabled"
        self.memory_high = MEMORY_12G
        self.gateway_active = "active"
        self.health_rc = 0
        self.timers_enabled = True
        self.backup = tmp / "backups"
        for t in ("daily", "weekly", "monthly"):
            (self.backup / t).mkdir(parents=True)
        self.units = tmp / "units"
        self.units.mkdir()
        host.install_units(self.units, markers=host.unit_markers(), runner=lambda *a, **k: (0, ""))
        self.commands: list[list[str]] = []
        self.probes: list[list[str]] = []
        self.host_env = tmp / "kit-host.env"

    def _make_package(self, root: pathlib.Path):
        pkg = root / "openclaw"
        (pkg / "dist").mkdir(parents=True, exist_ok=True)
        (pkg / "dist" / "index.js").write_text("", encoding="utf-8")
        if self.native:
            (pkg / "node_modules" / "tree-sitter-bash" / "prebuilds" / "linux-x64").mkdir(parents=True, exist_ok=True)
            (pkg / "node_modules" / "tree-sitter-bash" / "prebuilds" / "linux-x64" / "t.node").write_text("")
            (pkg / "node_modules" / "@koromix" / "koffi-linux-x64").mkdir(parents=True, exist_ok=True)
            (pkg / "node_modules" / "@koromix" / "koffi-linux-x64" / "koffi.node").write_text("")

    def __call__(self, argv, **_kw):
        a = list(argv)
        mutating = self._is_mutation(a)
        (self.commands if mutating else self.probes).append(a)
        if a[0] == "brew":
            if a[1:3] == ["--prefix", "node"]:
                return 0, str(self.brew_node.parent.parent)
            if a[1] == "--version":
                return 0, "Homebrew 5"
            if a[1] == "install":
                self.node_version = "v24.16.1"
                return 0, ""
        if a[1:] == ["--version"] and a[0].endswith("node"):
            return 0, self.node_version
        if a[0] == "openclaw" and a[1:] == ["--version"]:
            return (0, f"OpenClaw {self.openclaw_version} (abc)") if self.openclaw_version else (127, "not found")
        if a[0] == "openclaw" and a[1:] == ["health"]:
            return self.health_rc, ""
        if a[0] == "npm" and a[1:3] == ["root", "-g"]:
            return 0, str(self.root)
        if a[0].endswith("npm") and a[1] == "install":
            self.openclaw_version = "2026.9.9"
            if self.install_creates_natives:
                self.native = True
                self._make_package(self.root)
            return 0, ""
        if a[:3] == ["systemctl", "--user", "cat"]:
            return (0, self.unit) if self.unit else (1, "No files found")
        if a[:3] == ["systemctl", "--user", "show"]:
            if "ExecStart" in a:
                return (0, f"{{ path=x ; argv[]={self.exec_start_argv} ; ignore_errors=no }}") if self.unit else (0, "")
            if "MemoryHigh" in a:
                return (0, self.memory_high) if self.unit else (1, "")
        if a[:3] == ["systemctl", "--user", "is-active"]:
            return 0, self.gateway_active
        if a[:3] == ["systemctl", "--user", "is-enabled"]:
            if a[3].endswith(".timer"):
                return (0, "enabled") if self.timers_enabled else (1, "disabled")
            return 0, self.enabled
        if a[0] == "loginctl" and a[1] == "show-user":
            return 0, self.linger
        if a[0] == "loginctl" and a[1] == "enable-linger":
            self.linger = "yes"
        if a[:3] == ["systemctl", "--user", "enable"]:
            if a[-1].endswith(".timer"):
                self.timers_enabled = True
            else:
                self.enabled = "enabled"
        if a[:3] == ["systemctl", "--user", "set-property"]:
            self.memory_high = MEMORY_12G
        if a[0] == "sudo":
            for t in a[a.index("-m") + 2:]:
                pathlib.Path(t).mkdir(parents=True, exist_ok=True)
        if "gateway" in a and "install" in a:
            self.unit = GOOD_UNIT.format(node=self.brew_node, root=self.root)
        if a[:3] == ["systemctl", "--user", "daemon-reload"]:
            return 0, ""
        return 0, ""

    @staticmethod
    def _is_mutation(a: list[str]) -> bool:
        head = a[0]
        if head in ("brew",):
            return a[1] == "install"
        if head.endswith("npm"):
            return a[1] in ("install", "uninstall")
        if head == "sudo" or (head == "loginctl" and a[1] == "enable-linger"):
            return True
        if head == "systemctl":
            return a[2] in ("enable", "set-property", "daemon-reload")
        return "gateway" in a and "install" in a


@pytest.fixture
def sim(tmp_path):
    return HostSim(tmp_path)


def run(sim, **kw):
    lines: list[str] = []
    kw.setdefault("backup_dir", str(sim.backup))
    kw.setdefault("home", sim.home)
    kw.setdefault("host_env_path", sim.host_env)
    kw.setdefault("unit_dir", sim.units)
    kw.setdefault("sleep", lambda _s: None)
    results, ctx = host.bootstrap(sim, out=lines.append, **kw)
    return {r.step: r for r in results}, ctx, lines


@pytest.fixture(autouse=True)
def stable_env(monkeypatch):
    monkeypatch.setenv("USER", "tester")


# --- AC-10.1: the node gate ----------------------------------------------------------------------------

@pytest.mark.parametrize("version,ok", [("v24.15.0", False), ("v25.0.0", False), ("v26.0.0", False),
                                        ("v24.16.0", True), ("v24.99.1", True), ("v26.1.0", True),
                                        ("v27.0.0", True), ("v22.11.0", False), ("garbage", False)])
def test_the_node_range_is_non_contiguous(version, ok):
    assert host.node_version_ok(version) is ok


def test_a_node_outside_the_range_is_replaced_through_brew(sim):
    sim.node_version = "v25.2.0"
    results, ctx, _ = run(sim, only="node")
    assert results["node"].status == "changed"
    assert ["brew", "install", "node"] in ctx.commands


# --- AC-10.3: everything correct means zero mutations ------------------------------------------------------------

def test_a_correct_host_is_all_satisfied_in_the_documented_order_with_zero_mutations(sim):
    results, ctx, lines = run(sim)
    assert list(results) == list(host.BOOTSTRAP_STEPS)
    assert all(r.status == "satisfied" for r in results.values()), {k: (v.status, v.detail) for k, v in results.items()}
    assert sim.commands == [] and ctx.commands == []
    assert [ln.split("]")[1].split(":")[0].strip() for ln in lines] == list(host.BOOTSTRAP_STEPS)


def test_a_second_bootstrap_after_repairing_a_broken_host_changes_nothing(sim):
    sim.node_version, sim.linger, sim.enabled = "v25.0.0", "no", "disabled"
    sim.memory_high = "infinity"
    sim.timers_enabled = False
    first, _, _ = run(sim)
    assert any(r.status == "changed" for r in first.values())
    sim.commands.clear()
    second, _, _ = run(sim)
    assert all(r.status == "satisfied" for r in second.values())
    assert sim.commands == []


def test_dry_run_touches_nothing_and_reports_what_it_would_do(sim):
    sim.linger = "no"
    sim.memory_high = "infinity"
    results, ctx, lines = run(sim, dry_run=True)
    assert sim.commands == [] and ctx.commands == []
    assert results["boot"].status == "would-change" and results["memory-high"].status == "would-change"
    text = "\n".join(lines)
    assert "loginctl enable-linger tester" in text and "MemoryHigh=12G" in text
    assert "dry run: 2 step(s) would change something" in text


def test_dry_run_on_a_correct_host_reports_zero_changes(sim):
    _, _, lines = run(sim, dry_run=True)
    assert lines[-1] == "dry run: nothing to change"


def test_only_runs_a_single_step(sim):
    results, _, _ = run(sim, only="boot")
    assert list(results) == ["boot"]


def test_an_unknown_step_is_rejected(sim):
    with pytest.raises(ValueError):
        run(sim, only="nope")


# --- AC-10.2: the openclaw install ---------------------------------------------------------------------------------------

def test_the_install_carries_all_five_allowed_scripts_in_the_documented_string(sim):
    sim.openclaw_version = None
    results, _, _ = run(sim, only="openclaw")
    (install,) = [c for c in sim.commands if c[0].endswith("npm")]
    assert install[1:] == ["install", "-g",
                           "--allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs",
                           "openclaw@latest"]
    assert results["openclaw"].status == "changed"


def _drop_natives(sim):
    for f in sim.root.glob("openclaw/node_modules/**/*.node"):
        f.unlink()


def test_missing_native_modules_trigger_a_reinstall_that_repairs_them(sim):
    _drop_natives(sim)
    res, _, _ = run(sim, only="openclaw")
    assert res["openclaw"].status == "changed"
    assert list(sim.root.glob("openclaw/node_modules/tree-sitter-bash/**/*.node"))


def test_an_install_that_still_lacks_the_native_modules_fails_the_step(sim):
    _drop_natives(sim)
    sim.install_creates_natives = False
    res, _, _ = run(sim, only="openclaw")
    assert res["openclaw"].status == "failed" and "native modules missing" in res["openclaw"].detail


def test_an_install_records_the_previous_version_for_a_one_command_rollback(sim):
    results, _, _ = run(sim, only="openclaw", upgrade=True)
    env = host.read_host_env(sim.host_env)
    assert env["OPENCLAW_PREVIOUS_VERSION"] == "2026.9.4" and env["OPENCLAW_INSTALLED_VERSION"] == "2026.9.9"
    assert "openclaw@2026.9.4" in results["openclaw"].detail


def test_an_unhealthy_gateway_after_the_install_fails_with_the_rollback_and_repairs_nothing(sim):
    sim.health_rc = 1
    results, _, _ = run(sim, only="openclaw", upgrade=True, health_wait=10)
    r = results["openclaw"]
    assert r.status == "failed" and "openclaw@2026.9.4" in r.detail
    assert not any("gateway" in c and "install" in c for c in sim.commands)


def test_without_upgrade_a_present_openclaw_is_left_alone(sim):
    results, _, _ = run(sim, only="openclaw")
    assert results["openclaw"].status == "satisfied" and sim.commands == []


# --- AC-10.4: the shadowed copy -------------------------------------------------------------------------------------------------

def _shadow(sim, *, with_npm=True):
    bindir = sim.home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin"
    pkg = bindir.parent / "lib" / "node_modules" / "openclaw"
    pkg.mkdir(parents=True)
    bindir.mkdir(parents=True, exist_ok=True)
    (pkg / "openclaw.mjs").write_text("", encoding="utf-8")
    (bindir / "openclaw").symlink_to(pkg / "openclaw.mjs")
    if with_npm:
        (bindir / "npm").write_text("", encoding="utf-8")
    real = sim.root / "openclaw" / "openclaw.mjs"
    real.write_text("", encoding="utf-8")
    (sim.brew_node.parent / "openclaw").symlink_to(real)
    return bindir


@pytest.fixture
def path_env(sim, monkeypatch):
    monkeypatch.setenv("PATH", str(sim.brew_node.parent))


def test_a_shadowed_copy_is_removed_when_the_unit_proves_the_other_one_runs(sim, path_env):
    bindir = _shadow(sim)
    results, ctx, _ = run(sim, only="single-copy")
    assert results["single-copy"].status == "changed"
    assert [str(bindir / "npm"), "uninstall", "-g", "openclaw"] in sim.commands


def test_it_refuses_when_the_unit_does_not_prove_which_copy_runs(sim, path_env):
    _shadow(sim)
    sim.exec_start_argv = "/usr/bin/other something.js"
    results, _, _ = run(sim, only="single-copy")
    assert results["single-copy"].status == "refused" and sim.commands == []


def test_it_refuses_when_there_is_no_gateway_unit_to_prove_anything(sim, path_env):
    _shadow(sim)
    sim.unit = None
    sim.exec_start_argv = ""
    results, _, _ = run(sim, only="single-copy")
    assert results["single-copy"].status == "refused" and sim.commands == []


def test_it_never_removes_the_copy_the_unit_actually_runs(sim, path_env):
    """The version-managed copy IS the running one: removing the other would be the safe move,
    and removing this one would break the gateway."""
    bindir = _shadow(sim)
    sim.exec_start_argv = f"/n --x {bindir.parent}/lib/node_modules/openclaw/openclaw.mjs gateway"
    results, _, _ = run(sim, only="single-copy")
    assert not any(str(bindir / "npm") in c for c in sim.commands)
    assert results["single-copy"].status in ("refused", "satisfied")


def test_dry_run_prints_exactly_what_would_be_removed(sim, path_env):
    bindir = _shadow(sim)
    results, _, lines = run(sim, only="single-copy", dry_run=True)
    assert sim.commands == [] and results["single-copy"].status == "would-change"
    assert f"{bindir}/npm uninstall -g openclaw" in "\n".join(lines)
    assert str(bindir / "openclaw") in results["single-copy"].detail


def test_a_declined_removal_removes_nothing(sim, path_env):
    _shadow(sim)
    results, _, _ = run(sim, only="single-copy", confirm=lambda step, what: False)
    assert results["single-copy"].status == "declined" and sim.commands == []


# --- AC-10.5: T07 -------------------------------------------------------------------------------------------------------------------------

def test_a_unit_that_does_not_run_brews_node_is_regenerated_through_brews_node(sim):
    sim.unit = GOOD_UNIT.format(node="/tmp/fnm_multishells/1/bin/node", root=sim.root)
    results, _, _ = run(sim, only="gateway-unit")
    (cmd,) = [c for c in sim.commands if "gateway" in c]
    assert cmd[0] == str(sim.brew_node) and cmd[-3:] == ["gateway", "install", "--force"]
    assert results["gateway-unit"].status == "changed"
    assert "restart" in results["gateway-unit"].detail
    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in sim.commands)


def test_if_the_regenerated_unit_still_is_not_brews_node_it_fails_naming_t07(sim, monkeypatch):
    sim.unit = GOOD_UNIT.format(node="/tmp/fnm_multishells/1/bin/node", root=sim.root)
    orig = sim.__call__

    def stubborn(argv, **kw):
        if "gateway" in argv and "install" in argv:
            sim.commands.append(list(argv))
            return 0, ""  # the unit is NOT fixed
        return orig(argv, **kw)
    lines = []
    results, _ = host.bootstrap(stubborn, only="gateway-unit", home=sim.home, backup_dir=str(sim.backup),
                                host_env_path=sim.host_env, unit_dir=sim.units, out=lines.append)
    assert results[0].status == "failed" and "T07" in results[0].detail


def test_an_ephemeral_path_in_the_unit_environment_is_a_problem(sim):
    sim.unit = GOOD_UNIT.format(node=sim.brew_node, root=sim.root).replace(
        "Environment=PATH=/usr/bin:/bin", "Environment=PATH=/run/user/1000/fnm_multishells/9/bin:/usr/bin")
    results, _, _ = run(sim, only="gateway-unit", dry_run=True)
    assert results["gateway-unit"].status == "would-change" and "T07" in results["gateway-unit"].detail


def test_a_missing_gateway_unit_is_installed(sim):
    sim.unit = None
    results, _, _ = run(sim, only="gateway-unit")
    assert results["gateway-unit"].status == "changed"


# --- the remaining steps ------------------------------------------------------------------------------------------------------------------------

def test_boot_enables_linger_and_the_unit_only_when_needed(sim):
    sim.linger, sim.enabled = "no", "enabled"
    run(sim, only="boot")
    assert sim.commands == [["loginctl", "enable-linger", "tester"]]


def test_backup_dirs_are_created_with_install_d_owned_by_the_user(sim):
    for t in ("daily", "weekly", "monthly"):
        (sim.backup / t).rmdir()
    results, _, _ = run(sim, only="backup-dirs")
    (cmd,) = sim.commands
    assert cmd[:6] == ["sudo", "install", "-d", "-o", "tester", "-m"] and results["backup-dirs"].status == "changed"
    assert all((sim.backup / t).is_dir() for t in ("daily", "weekly", "monthly"))


def test_memory_high_uses_set_property_and_never_a_drop_in(sim):
    sim.memory_high = "infinity"
    run(sim, only="memory-high")
    assert sim.commands == [["systemctl", "--user", "set-property", host.GATEWAY_UNIT, "MemoryHigh=12G"]]
    assert not any(".d" in " ".join(c) or "drop" in " ".join(c) for c in sim.commands)


def test_memory_high_needs_an_installed_unit(sim):
    sim.unit = None
    results, _, _ = run(sim, only="memory-high")
    assert results["memory-high"].status == "refused"


def test_units_are_installed_and_their_timers_enabled(sim):
    for f in sim.units.iterdir():
        f.unlink()
    sim.timers_enabled = False
    results, _, _ = run(sim, only="units")
    assert results["units"].status in ("changed", "failed")
    assert len(list(sim.units.iterdir())) == 10


# --- AC-10.6 and consent -------------------------------------------------------------------------------------------------------------------------

def test_bootstrap_never_writes_openclaw_json_or_restarts_the_gateway(sim):
    sim.node_version, sim.linger, sim.memory_high, sim.openclaw_version = "v25.0.0", "no", "infinity", None
    sim.unit = GOOD_UNIT.format(node="/tmp/x/bin/node", root=sim.root)
    run(sim, upgrade=True)
    flat = [" ".join(c) for c in sim.commands]
    assert not any("openclaw.json" in c for c in flat)
    assert not any(c.startswith("openclaw config") for c in flat)
    assert not any(" restart" in c or " stop" in c for c in flat)


def test_every_change_goes_through_the_confirm_callback(sim):
    sim.linger, sim.memory_high = "no", "infinity"
    asked: list[str] = []
    results, _, _ = run(sim, confirm=lambda step, what: asked.append(step) or False)
    assert sim.commands == []
    assert results["boot"].status == "declined" and results["memory-high"].status == "declined"
    assert set(asked) >= {"boot", "memory-high"}


def test_a_step_that_raises_is_reported_and_does_not_take_the_run_down(sim, monkeypatch):
    monkeypatch.setitem(host._STEP_FUNCS, "boot", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    results, _, _ = run(sim)
    assert results["boot"].status == "failed" and "boom" in results["boot"].detail
    assert results["units"].status == "satisfied"


def test_the_cli_registers_bootstrap_with_its_flags():
    from ai_resources import cli
    args = cli.build_parser().parse_args(["openclaw", "bootstrap", "--dry-run", "--only", "boot", "--upgrade"])
    assert args.func is host.cmd_bootstrap and args.dry_run and args.only == "boot" and args.upgrade
