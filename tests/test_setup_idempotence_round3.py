"""ADR-0015 S16 / acceptance 18: every cockpit setup configures, run twice in a scratch HOME, leaves the second run with
zero diff, no secret in the kit state and no gateway restart. Nothing here reaches the operator's real ~/.claude,
~/.openclaw, ~/.gemini, ~/.config/ai-resources or the installed engram plugin: scratch HOME, stubs and dry runs only."""
from __future__ import annotations

import copy
import json
import pathlib
import re
import subprocess

import pytest
import yaml

from ai_resources import openclaw_host as host
from ai_resources import repo_root
from ai_resources.setup import cockpit_auth, effort, profiles, state
from ai_resources.setup.cockpits import _shared, claude, codex, cursor, gemini, openclaw as oc
from ai_resources.setup.cockpits import _openclaw_host as section
from test_openclaw_host_wizard import sim  # noqa: F401  (fixture)

TOKEN_LIKE = re.compile(r"sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]+|ghp_[A-Za-z0-9]{20,}|[A-Za-z0-9_-]{40,}")
SENTINEL = "sk-ant-api03-SCRATCH-SENTINEL-KEY-0123456789"
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT[0-9:.+]+")      # last_configured_at and friends: stamped on every run, kept in memory


def tree(root: pathlib.Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            out[str(p.relative_to(root))] = str(p.readlink()).encode()
        elif p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class FakeOpenClaw:
    """A stateful stand-in for the OpenClaw CLI and its config file: `agents add`, `config patch`, plugin install."""

    def __init__(self, home: pathlib.Path):
        self.home = home
        self.doc: dict = {"agents": {"entries": {"main": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}}}}
        self.calls: list[list[str]] = []
        self.patches: list[dict] = []

    def cli(self, args, stdin=None, timeout=120):
        self.calls.append(list(args))
        if args[:2] == ["agents", "list"]:
            return 0, json.dumps([{"id": i} for i in (self.doc.get("agents", {}).get("entries") or {})])
        if args[:2] == ["agents", "add"]:
            self.doc.setdefault("agents", {}).setdefault("entries", {}).setdefault(args[2], {"name": args[2]})
            return 0, "added"
        if args[:2] == ["plugins", "install"]:
            return 0, "linked"
        if args[:2] == ["models", "list"]:
            return 0, (pathlib.Path(__file__).parent / "fixtures" / "openclaw" / "providers_models_list.json").read_text()
        if args[:3] == ["models", "auth", "list"]:
            return 0, json.dumps({"profiles": [{"id": "anthropic:default", "provider": "anthropic"}]})
        return 1, "unexpected"

    def apply(self, patch, dry_run=False, replace_paths=None, allow_restart=False):
        if not dry_run:
            self.patches.append(copy.deepcopy(patch))
            _merge(self.doc, patch)
        return True, "ok"


def _merge(dst, src):
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            dst[k] = list(dict.fromkeys(dst[k] + v)) if k == "extraDirs" else copy.deepcopy(v)
        else:
            dst[k] = copy.deepcopy(v)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)                 # present in the environment, never read by the kit
    monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    # Claude Code
    cl = home / ".claude"
    for attr, rel in (("CONFIG_ROOT", ""), ("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"),
                      ("AGENTS_DIR", "agents"), ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, cl / rel if rel else cl)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_trim_engram_hooks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    # agy / Gemini files and Cursor
    g = home / ".gemini"
    monkeypatch.setattr(gemini, "CONFIG_ROOT", g)
    monkeypatch.setattr(gemini, "SETTINGS_PATH", g / "settings.json")
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", g / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", g / "GEMINI.md")
    cu = home / ".cursor"
    monkeypatch.setattr(cursor, "INSTRUCTIONS_PATH", cu / "AGENT_KIT.md")
    monkeypatch.setattr(cursor, "MCP_PATH", cu / "mcp.json")
    # OpenClaw
    fake = FakeOpenClaw(home)
    oc_root = home / ".openclaw"
    monkeypatch.setattr(oc, "CONFIG_ROOT", oc_root)
    monkeypatch.setattr(oc, "_openclaw", fake.cli)
    monkeypatch.setattr(oc, "_apply_hot", lambda patch, dry_run=False, replace_paths=None: fake.apply(patch, dry_run, replace_paths))
    monkeypatch.setattr(oc, "_lanes_apply", lambda patch, dry_run=False, replace_paths=None: fake.apply(patch, dry_run, replace_paths))
    monkeypatch.setattr(oc, "_lanes_classify", lambda patch: [])
    monkeypatch.setattr(oc, "read_config", lambda p: fake.doc)
    monkeypatch.setattr(oc.openclaw_host, "change_records", lambda *a, **k: [])
    monkeypatch.setattr(oc, "restart_gateway", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gateway restart")))
    monkeypatch.setattr(host, "restart_gateway", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gateway restart")),
                        raising=False)
    monkeypatch.setattr(oc.shutil, "which", lambda name: f"/fake/{name}")
    return type("S", (), {"home": home, "fake": fake, "oc_root": oc_root})


def new_state():
    s = state.SetupState()
    s.profile.name = "balanced"
    s.cockpits = {c: state.CockpitState(installed=True) for c in ("claude", "openclaw", "agy", "cursor", "codex")}
    cockpit_auth.set_method(s, "claude", "subscription")
    cockpit_auth.set_method(s, "agy", "google_account")
    cockpit_auth.set_method(s, "cursor", "managed")
    cockpit_auth.set_providers(s, {"anthropic": "cli", "openai": "api_key"})
    cockpit_auth.apply_legacy_flags(s)
    return s


def configure_all(s, scratch):
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    ctx = {"state": s, "executors": ex, "master_key": "", "gateway_url": ""}
    claude.configure(ctx)
    gemini.configure({"state": s})
    cursor.configure({"state": s})
    codex.configure({"state": s})
    ak = str(repo_root())
    runs: list = []
    doc = scratch.fake.doc
    oc._configure_skills(s, doc, scratch.oc_root / "openclaw.json", [], ak, dry_run=False, run_changes=runs)
    oc._configure_roles(ctx, s, scratch.fake.doc, ak, dry_run=False, run_changes=runs)
    oc._configure_orchestrator_plugin(ctx, s, scratch.fake.doc, scratch.oc_root / "openclaw.json", ak, dry_run=False,
                                      run_changes=runs)


def snapshot(scratch, s):
    return {"home": tree(scratch.home), "doc": json.dumps(scratch.fake.doc, sort_keys=True),
            "state": TIMESTAMP.sub("<ts>", yaml.safe_dump(state._to_dict(s), sort_keys=True)), "patches": len(scratch.fake.patches),
            "calls": len(scratch.fake.calls)}


def test_every_cockpit_configured_twice_leaves_the_second_run_with_zero_diff(scratch, monkeypatch):
    # the kit skills the installed copy would offer: the real repo's, linked through scratch paths only
    s = new_state()
    configure_all(s, scratch)
    first = snapshot(scratch, s)
    configure_all(s, scratch)
    second = snapshot(scratch, s)
    assert second["home"] == first["home"], sorted(set(first["home"]) ^ set(second["home"]))[:5]
    assert second["doc"] == first["doc"] and second["patches"] == first["patches"]
    assert second["state"] == first["state"]
    # a rerun may read (list agents) but never writes through the CLI again
    writes = [c for c in scratch.fake.calls[first["calls"]:] if c[:2] in (["agents", "add"], ["plugins", "install"])]
    assert writes == []


def test_the_first_run_reaches_all_cockpits_with_their_own_files(scratch):
    s = new_state()
    configure_all(s, scratch)
    h = scratch.home
    assert (h / ".claude" / "settings.json").is_file() and list((h / ".claude" / "agents").glob("*.md"))
    assert "effort: high" in (h / ".claude" / "agents" / "planner.md").read_text()
    assert "## Kit roles" in (h / ".gemini" / "GEMINI.md").read_text()
    assert (h / ".cursor" / "AGENT_KIT.md").is_file() and (h / ".agents" / "skills").is_dir()
    entries = scratch.fake.doc["agents"]["entries"]
    assert len([i for i in entries if i.startswith("kit-role-")]) == 15
    assert entries["kit-role-planner"]["thinkingDefault"] == "high"
    cfg = scratch.fake.doc["plugins"]["entries"]["ai-resources"]["config"]
    assert cfg["enableOrchestrator"] is True
    assert [e for e in s.openclaw.host_restart_pending if e["op"] == "plugin"]
    assert scratch.fake.doc["skills"]["load"]["extraDirs"]


def test_no_secret_reaches_the_kit_state_or_any_scratch_file(scratch):
    s = new_state()
    configure_all(s, scratch)
    configure_all(s, scratch)
    doc = state._to_dict(s)
    assert cockpit_auth.secret_findings(doc["cockpit_auth"]) == []
    state.save(s)
    text = state.state_path().read_text()
    assert SENTINEL not in text
    for rel, data in tree(scratch.home).items():
        assert SENTINEL.encode() not in data, rel
    # the serialized kit state holds no token-shaped string at all
    flat = yaml.safe_dump(doc)
    assert not [m for m in TOKEN_LIKE.findall(flat) if not m.startswith(("/", "kit-role-")) and "/" not in m
                and not re.fullmatch(r"[a-z0-9-]+", m)], TOKEN_LIKE.findall(flat)


def test_no_gateway_restart_and_only_read_or_link_commands_are_issued(scratch):
    s = new_state()
    configure_all(s, scratch)
    configure_all(s, scratch)
    for call in scratch.fake.calls:
        assert call[0] != "gateway" and call[:2] != ["gateway", "restart"], call
        assert call[:2] in (["agents", "list"], ["agents", "add"], ["plugins", "install"], ["models", "list"],
                            ["models", "auth"]), call


def test_a_dry_run_of_every_openclaw_writer_writes_nothing(scratch):
    s = new_state()
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    ak, runs = str(repo_root()), []
    before = snapshot(scratch, s)
    oc._configure_skills(s, scratch.fake.doc, scratch.oc_root / "o.json", [], ak, dry_run=True, run_changes=runs)
    oc._configure_roles({"executors": ex}, s, scratch.fake.doc, ak, dry_run=True, run_changes=runs)
    oc._configure_orchestrator_plugin({}, s, scratch.fake.doc, scratch.oc_root / "o.json", ak, dry_run=True, run_changes=runs)
    assert snapshot(scratch, s)["doc"] == before["doc"] and scratch.fake.patches == []
    assert [c for c in scratch.fake.calls if c[:2] in (["agents", "add"], ["plugins", "install"])] == []
    assert s.openclaw.host_restart_pending == []


def test_a_rerun_after_an_operator_edit_keeps_it(scratch):
    s = new_state()
    configure_all(s, scratch)
    scratch.fake.doc["agents"]["entries"]["kit-role-verifier"]["model"]["primary"] = "anthropic/claude-sonnet-5-5"
    planner = scratch.home / ".claude" / "agents" / "planner.md"
    planner.write_text(planner.read_text().replace("effort: high", "effort: max"))
    configure_all(s, scratch)
    assert scratch.fake.doc["agents"]["entries"]["kit-role-verifier"]["model"]["primary"] == "anthropic/claude-sonnet-5-5"
    assert "effort: max" in planner.read_text()


# --- Acceptance 18 / S16: the real `ai-resources setup` entry point, twice, in a scratch HOME ---------------------------
# cli.cmd_setup -> wizard.run -> _step9_apply -> every cockpit's configure(), with only the openclaw binary, the gateway,
# the systemd runner and host detection stubbed. Everything the kit writes is hashed under one scratch root.

class _StubbedOpenClawCli:
    """The scratch host's `openclaw`: the simulated config/patch CLI of test_openclaw_host_wizard plus the agent, plugin and
    model commands the round-3 writers use. It writes only into the scratch openclaw.json; nothing reaches a binary."""

    def __init__(self, base):
        self.base = base
        self.cfg = base.cfg
        self.calls: list[list[str]] = []

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append(list(args))
        doc = json.loads(self.cfg.read_text(encoding="utf-8"))
        if args[:2] == ["agents", "list"]:
            return 0, json.dumps([{"id": i} for i in (doc.get("agents", {}).get("entries") or {})])
        if args[:2] == ["agents", "add"]:
            doc.setdefault("agents", {}).setdefault("entries", {}).setdefault(args[2], {"name": args[2]})
            self.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return 0, "added"
        if args[:2] == ["plugins", "install"]:
            return 0, "linked"
        if args[:2] == ["models", "list"]:
            return 0, (pathlib.Path(__file__).parent / "fixtures" / "openclaw" / "providers_models_list.json").read_text()
        if args[:3] == ["models", "auth", "list"]:
            return 0, json.dumps({"profiles": [{"id": "anthropic:default", "provider": "anthropic"}]})
        return self.base(args, stdin, timeout)

    def __getattr__(self, name):
        return getattr(self.base, name)


@pytest.fixture
def real_setup(sim, monkeypatch):
    """`sim` (test_openclaw_host_wizard) is the scratch OpenClaw host under tmp_path; this adds the Claude Code, agy and
    state paths inside the same scratch tree, the host detection and a saved state
    (the run asks for `--cockpits claude,openclaw,gemini`: `gemini` is the registry id of the module that writes the agy
    files, and `--cockpits agy` is refused as an unknown cockpit, see the handoff)."""
    import types

    from ai_resources.setup import detection, tools, ui, wizard

    root = sim.tmp
    home = sim.home
    sim.overrides_file.unlink()                                  # fill-only, as on a real host
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))
    monkeypatch.setenv("AI_RESOURCES_OFFLINE", "1")
    monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: repo_root())
    cl = home / ".claude"
    for attr, rel in (("CONFIG_ROOT", ""), ("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"),
                      ("AGENTS_DIR", "agents"), ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, cl / rel if rel else cl)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    g = home / ".gemini"
    monkeypatch.setattr(gemini, "CONFIG_ROOT", g)
    monkeypatch.setattr(gemini, "SETTINGS_PATH", g / "settings.json")
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", g / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", g / "GEMINI.md")
    # the OpenClaw binary and the gateway: stubbed (ADR-0003)
    cli = _StubbedOpenClawCli(sim.oc)
    monkeypatch.setattr(oc, "_openclaw", cli)
    monkeypatch.setattr(oc.shutil, "which", lambda name: f"/fake/{name}")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gateway restart"))  # noqa: E731
    monkeypatch.setattr(oc, "restart_gateway", boom)
    monkeypatch.setattr(host, "restart_gateway", boom, raising=False)
    # host detection, the install offer and the terminal
    det = {c: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/fake/{c}", name=c)
           for c in ("claude", "openclaw", "gemini")}
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: det)
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_step10_verify", lambda s, t: 0)    # read-only doctor: it would probe the real systemd
    # The host section's consent gates (config patch, systemd units, drained window) refuse in a non-interactive run
    # by design; the operator's "yes" is stubbed so the whole section is exercised (nothing else is made interactive).
    monkeypatch.setattr(section, "_gate", lambda message, detail="": True)
    spawned: list = []

    def refuse(argv, *a, **k):                    # a read-only probe of the fake binary path: recorded, never executed
        spawned.append(list(argv))
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    # a saved state
    s = state.SetupState()
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.profile.name = "balanced"
    s.openclaw.engine = "keep"
    # the OpenClaw host section as an operator saved it: canonical block (subagent limits, resource guards, heartbeats),
    # env file, guard hook, units and the AGENTS.md templates
    o = s.openclaw
    o.host, o.host_config, o.host_guard, o.host_units, o.host_agents_md = True, True, True, True, True
    o.host_narration, o.host_graceful_restart, o.host_resource_guards = "milestones", "notify", "on"
    o.host_domain, o.host_pod_cidr, o.host_operator_id, o.host_backup_dir = "ai.example.org", "10.9.0.0/24", "123456789", "/srv/backups"
    cockpit_auth.set_method(s, "claude", "subscription")
    cockpit_auth.set_method(s, "agy", "google_account")
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    cockpit_auth.apply_legacy_flags(s)
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    sim.cli = cli
    sim.spawned = spawned
    sim.root = root
    return sim


def _setup_args():
    import argparse
    return argparse.Namespace(dry_run=False, yes=False, non_interactive=True, profile="", lanes=False, no_lanes=False,
                              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
                              shape="", providers="", smoke_path="", cockpits="claude,openclaw,gemini", compose="", restore_hygiene=False,
                              restore_agy_mcp=False, restore_claude_md=False)


def _hash_tree(root: pathlib.Path) -> dict[str, str]:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            out[str(p.relative_to(root))] = "link:" + str(p.readlink())
        elif p.is_file():
            data = p.read_bytes()
            if p.name == "setup-state.yaml":                  # stamped on every run, like last_configured_at
                data = TIMESTAMP.sub("<ts>", data.decode()).encode()
            out[str(p.relative_to(root))] = hashlib.sha256(data).hexdigest()
    return out


def test_ai_resources_setup_run_twice_in_a_scratch_home_has_zero_diff_and_no_new_pending(real_setup, capsys):
    from ai_resources import cli
    sim = real_setup
    assert cli.cmd_setup(_setup_args()) == 0                       # the entry point `ai-resources setup` calls
    s1 = state.load()
    # the first run reached every section: Claude Code, agy, the OpenClaw host block, roles and the plugin
    h = sim.home
    assert (h / ".claude" / "settings.json").is_file() and list((h / ".claude" / "agents").glob("*.md"))
    assert "## Kit roles" in (h / ".gemini" / "GEMINI.md").read_text()
    live = json.loads(sim.cfg.read_text(encoding="utf-8"))
    sub = live["agents"]["defaults"]["subagents"]                  # host block: subagent limits
    assert (sub["maxConcurrent"], sub["maxChildrenPerAgent"], sub["maxSpawnDepth"], sub["runTimeoutSeconds"]) == (32, 16, 4, 7200)
    roles = {i: e for i, e in live["agents"]["entries"].items() if i.startswith("kit-role-")}
    assert len(roles) == 15 and roles["kit-role-planner"]["thinkingDefault"] == "high"      # kit-role agents
    assert roles["kit-role-tester"]["model"]["primary"] == "anthropic/claude-sonnet-5-5"     # agy not admitted: matrix fallback
    assert live["plugins"]["entries"]["ai-resources"]["config"]["enableOrchestrator"] is True
    assert [e for e in s1.openclaw.host_restart_pending if e["op"] == "plugin"]              # plugin pending
    assert (h / ".openclaw" / "kit-host.env").is_file()
    first = _hash_tree(sim.root)
    pending_first = copy.deepcopy(s1.openclaw.host_restart_pending)
    patches_first, calls_first = len(sim.oc.real_patches()), len(sim.cli.calls)

    capsys.readouterr()
    assert cli.cmd_setup(_setup_args()) == 0
    rerun_output = capsys.readouterr().out
    assert "your edited AGENTS.md is kept" not in rerun_output       # the kit's own block is not an operator edit
    s2 = state.load()
    second = _hash_tree(sim.root)
    changed = sorted(k for k in set(first) | set(second) if first.get(k) != second.get(k))
    assert changed == [], changed                                  # zero file diff under the scratch root
    assert s2.openclaw.host_restart_pending == pending_first       # zero new pending entries
    assert len(sim.oc.real_patches()) == patches_first, sim.oc.real_patches()[patches_first:]   # no config patch sent
    assert not [c for c in sim.cli.calls[calls_first:] if c[:2] in (["agents", "add"], ["plugins", "install"])]
    # nothing real was spawned: `subprocess.run` is refused above, so a recorded call is a read-only probe that never ran
    # (the live checks ask `openclaw models status --json` and `systemctl --user show <unit>`)
    read_only = lambda argv: (  # noqa: E731
        str(argv[0]).startswith("/fake/")
        or (pathlib.Path(argv[0]).name == "openclaw" and argv[1:4] == ["models", "status", "--json"])
        or argv[:3] == ["systemctl", "--user", "show"])
    assert all(read_only(argv) for argv in sim.spawned), sim.spawned
    # no secret in the kit state or in any scratch file
    assert cockpit_auth.secret_findings(state._to_dict(s2)["cockpit_auth"]) == []
    for rel, digest in second.items():
        assert not rel.endswith(".env") or SENTINEL not in (sim.root / rel).read_text(), rel
    assert SENTINEL not in state.state_path().read_text()


def test_the_host_wildcard_rewrite_leaves_kit_role_agents_on_their_team_model():
    """Regression found by the test above: the host profile's `*` entry (haiku orchestrator -> sonnet) also rewrote the
    kit-role agents the balanced team puts on haiku, so a second setup run patched them again."""
    doc = {"agents": {"entries": {"kit-role-tester": {"model": {"primary": "anthropic/claude-haiku-5-5"}},
                                  "app": {"model": {"primary": "anthropic/claude-haiku-4-5"}}}}}
    tree = host.expand_wildcards({"agents": {"entries": {"*": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}}}}, doc)
    assert set(tree["agents"]["entries"]) == {"app"}


def test_the_kit_block_in_a_role_workspace_is_not_an_operator_edit(tmp_path):
    from ai_resources.setup.cockpits import openclaw_roles as roles
    agent = {"prompt": "# Planner\n\nplan things\n"}
    ws = tmp_path / "kit-role-planner"
    assert roles._write_prompt(ws, agent, None) == "written"
    sha = __import__("hashlib").sha256(agent["prompt"].encode()).hexdigest()
    block = "<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->\nkit\n<!-- END ai-resources -->\n\n"
    (ws / "AGENTS.md").write_text(block + agent["prompt"])
    assert roles._write_prompt(ws, agent, sha) == "same"                       # block + the kit's text: nothing to do
    (ws / "AGENTS.md").write_text(block + agent["prompt"] + "my own rule\n")
    assert roles._write_prompt(ws, agent, sha) == "kept"                       # a real edit is still kept
