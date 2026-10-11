"""The OpenClaw host section of `ai-resources setup`, driven through the cockpit's wizard entry points.

Every test calls what the wizard calls (`openclaw.prompt`, `openclaw.configure`, `openclaw.teardown`)
with a scripted `ui` and a simulated host: `openclaw` is a fake that applies `config patch` to a
temporary openclaw.json, systemctl is a recorder, and HOME, XDG_CONFIG_HOME, kit-host.env and
~/.claude/settings.json all point into tmp_path. Nothing here can reach the real ~/.claude,
~/.openclaw, systemd or a gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sys
from dataclasses import asdict

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state, ui, wizard  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_host as section, claude, openclaw  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "openclaw_host_config.json"
USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /me/kubectl-context-guard.py"}]}
LEGACY_HOOK = {"hooks": [{"type": "command", "command": "python3 $HOME/.claude/hooks/openclaw-team-progress.py"}]}

ANSWERS = {
    "Configure this machine as an OpenClaw host": True,
    "Narrate the Claude Code team's work": "milestones",
    "Install the gateway guard hook": True,
    "Install the openclaw-* systemd units (daily": True,
    "Apply the canonical OpenClaw config block (heartbeats": True,
    "Your Telegram user id": "123456789",
    "Where the backup tiers are written": "/srv/backups",
    "Public host name": "ai.example.org",
    "CIDR of the ingress": "10.9.0.0/24",
    "Let the health check restart the gateway gracefully": "notify",   # ADR-0004
    "Let setup fill the gateway's resource guards": "on",
    "Telegram group chat id for agent": "",          # empty: leave every binding alone (C2)
    "Enable the workboard plugin (a shared": True,
    "Write an AGENTS.md into every agent workspace": True,
    "Check this host against the documented setup": False,
    "When should `ai-resources doctor` report that": "linked",
}
GATES = {
    "Install the openclaw-* systemd units and enable": True,
    "Apply this patch to the running gateway's config": True,
    "Apply in a drained window": True,       # gateway.bind is restart-required (ADR-0003)
    "Enable the workboard plugin (`openclaw plugins enable workboard`)": True,
}


class ScriptedUi:
    """Answers ui.confirm/select/text by a substring of the message. An unanswered question fails."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.asked: list[str] = []
        self.validators: dict[str, object] = {}
        self.log: list[tuple[str, str]] = []
        self.non_interactive = False

    def _answer(self, message):
        self.asked.append(message)
        # Longest key first: "Enable the workboard plugin (`...`)" must not match the shorter prompt key.
        for key in sorted(self.answers, key=len, reverse=True):
            if key in message:
                return self.answers[key]
        raise AssertionError(f"unscripted question: {message!r}")

    def confirm(self, message, default=True):
        return self._answer(message)

    def select(self, message, choices, default=None, instruction=""):
        return self._answer(message)

    def text(self, message, default="", validate=None):
        self.validators[message] = validate
        return self._answer(message)

    def install(self, monkeypatch):
        monkeypatch.setattr(ui, "confirm", self.confirm)
        monkeypatch.setattr(ui, "select", self.select)
        monkeypatch.setattr(ui, "text", self.text)
        monkeypatch.setattr(ui, "is_non_interactive", lambda: self.non_interactive)
        for level in ("info", "ok", "warn", "error", "detail"):
            monkeypatch.setattr(ui, level, lambda msg, _l=level: self.log.append((_l, msg)))

    def messages(self, level):
        return [m for l, m in self.log if l == level]


def _merge(node, patch, path, replace):
    for key, value in patch.items():
        here = ".".join([*path, key])
        if value is None:
            node.pop(key, None)
        elif isinstance(value, dict) and here not in replace and isinstance(node.get(key), dict):
            _merge(node[key], value, [*path, key], replace)
        elif isinstance(value, dict) and here not in replace:
            node[key] = {}
            _merge(node[key], value, [*path, key], replace)
        else:
            node[key] = copy.deepcopy(value)


class FakeOpenclaw:
    """`openclaw` against a JSON file: `config patch` merges (--dry-run only validates)."""

    def __init__(self, cfg: pathlib.Path):
        self.cfg = cfg
        self.calls: list[tuple[list[str], dict | None]] = []
        self.dry_run_ok = True

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append((list(args), json.loads(stdin) if stdin else None))
        if args[:2] == ["config", "file"]:
            return 0, str(self.cfg)
        if args[:2] == ["config", "patch"]:
            if "--dry-run" in args:
                return (0, "valid") if self.dry_run_ok else (1, "invalid: schema")
            replace = {args[i + 1] for i, a in enumerate(args) if a == "--replace-path"}
            doc = json.loads(self.cfg.read_text(encoding="utf-8"))
            _merge(doc, json.loads(stdin), [], replace)
            self.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return 0, "ok"
        if args[:2] == ["plugins", "enable"] or args[:2] == ["plugins", "disable"]:
            doc = json.loads(self.cfg.read_text(encoding="utf-8"))
            doc.setdefault("plugins", {}).setdefault("entries", {}).setdefault(args[2], {})["enabled"] = args[1] == "enable"
            self.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return 0, "ok"
        return 0, "ok"

    def real_patches(self):
        return [(a, p) for a, p in self.calls if a[:2] == ["config", "patch"] and "--dry-run" not in a]

    def dry_patches(self):
        return [(a, p) for a, p in self.calls if a[:2] == ["config", "patch"] and "--dry-run" in a]


class FakeSystemd:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.enabled: set[str] = set()

    def __call__(self, argv, *, env=None, timeout=120, input=None):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return (0, "enabled") if argv[3] in self.enabled else (1, "disabled")
        if argv[:3] == ["systemctl", "--user", "enable"]:
            self.enabled.add(argv[4])
        if argv[:3] == ["systemctl", "--user", "disable"]:
            self.enabled.discard(argv[4])
        if argv[:2] == ["git", "-C"]:
            return 128, "not a git repository"
        if argv[:3] == ["systemctl", "--user", "show"] and "TasksCurrent" in argv:
            return 0, "[not set]"                  # the drained window sees an empty cgroup
        return 0, ""


class Host:
    """A simulated OpenClaw host under tmp_path, plus a snapshot of everything the kit may write."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.workspaces = {name: tmp / "ws" / name for name in ("default", "main", "app", "infra", "docs", "claude")}
        for ws in self.workspaces.values():
            ws.mkdir(parents=True)
        (self.workspaces["app"] / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        doc["agents"]["defaults"]["workspace"] = str(self.workspaces["default"])
        for aid, ws in self.workspaces.items():
            if aid in doc["agents"]["entries"]:
                doc["agents"]["entries"][aid]["workspace"] = str(ws)
        self.cfg = tmp / "openclaw.json"
        self.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        self.original_config = self.cfg.read_bytes()
        self.env_file = self.home / ".openclaw" / "kit-host.env"
        self.units = tmp / "xdg" / "systemd" / "user"
        self.settings = claude.SETTINGS_PATH
        self.oc = FakeOpenclaw(self.cfg)
        self.systemd = FakeSystemd()

    def write_settings(self, hooks: dict):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")

    def hooks(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8")).get("hooks", {})

    def files(self) -> dict[str, bytes]:
        """Every file the kit may have written, by path."""
        found: dict[str, bytes] = {}
        for root in (self.home, self.units, self.tmp / "ws", claude.SETTINGS_PATH.parent):
            for p in sorted(root.rglob("*")) if root.exists() else []:
                if p.is_file():
                    found[str(p)] = p.read_bytes()
        found[str(self.cfg)] = self.cfg.read_bytes()
        return found


@pytest.fixture
def sim(tmp_path, monkeypatch):
    h = Host(tmp_path)
    home = h.home
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # The fixture is a customised host. Setup is fill-only (ADR-0003), so this simulated operator opts
    # into the canonical profile with `force: ["*"]`; the fill-only tests below delete this file.
    h.overrides_file = tmp_path / "overrides" / "kit-host-overrides.json5"
    h.overrides_file.parent.mkdir()
    h.overrides_file.write_text('{"force": ["*"]}\n', encoding="utf-8")
    monkeypatch.setattr(host, "host_overrides_path", lambda: h.overrides_file)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(h.cfg))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host, "HOST_ENV_PATH", h.env_file)
    monkeypatch.setattr(host, "tailnet_available", lambda: True)   # the suite must not depend on this machine's Tailscale
    monkeypatch.setattr(openclaw, "CONFIG_ROOT", home / ".openclaw")
    monkeypatch.setattr(openclaw, "_openclaw", h.oc)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(section, "_runner", lambda: h.systemd)
    monkeypatch.setattr(section, "_window_kwargs", lambda: {"sleep": lambda _s: None})
    # The suite must never reach the operator's real files (conftest redirects claude.SETTINGS_PATH).
    assert str(claude.SETTINGS_PATH).startswith(str(tmp_path.parent)), claude.SETTINGS_PATH
    assert str(h.env_file).startswith(str(tmp_path))
    return h


@pytest.fixture
def script(monkeypatch):
    fake = ScriptedUi()
    fake.answers = {**ANSWERS, **GATES}
    fake.install(monkeypatch)
    monkeypatch.setattr(openclaw, "_prompt_engine", lambda s, **_k: None)
    monkeypatch.setattr(openclaw.voice, "prompt", lambda s: None)
    return fake


def _state() -> state.SetupState:
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    s.openclaw.engine = "keep"
    return s


def _run_wizard(s, *, dry_run=False):
    """What step 7 then step 9 do for the OpenClaw cockpit."""
    openclaw.prompt(s, dry_run=dry_run)
    return openclaw.configure({"state": s, "dry_run": dry_run})


def _configure(s, *, dry_run=False):
    return openclaw.configure({"state": s, "dry_run": dry_run})


def outside_block(text: str) -> str:
    """The file minus the kit's managed block: what the operator owns."""
    from ai_resources.setup.cockpits import _shared
    lines = text.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    if begin is None or end is None:
        return text
    return ("".join(lines[:begin] + lines[end + 1:])).lstrip("\r\n")


def without_blocks(files: dict[str, bytes]) -> dict[str, bytes]:
    """`files` with the kit block stripped from every AGENTS.md; a file that was only the block is gone."""
    out = {k: outside_block(v.decode("utf-8")).encode("utf-8") if k.endswith("AGENTS.md") else v
           for k, v in files.items()}
    return {k: v for k, v in out.items() if not (k.endswith("AGENTS.md") and not v)}


# --- the questions ------------------------------------------------------------------------------------

def test_a_first_run_defaults_every_answer_to_no_except_the_workboard(sim, script, monkeypatch):
    defaults: dict[str, object] = {}
    real_confirm, real_select, real_text = script.confirm, script.select, script.text

    def confirm(message, default=True):
        defaults[message[:45]] = default
        return real_confirm(message, default)

    def select(message, choices, default=None, instruction=""):
        defaults[message[:45]] = default
        return real_select(message, choices, default, instruction)

    def text(message, default="", validate=None):
        defaults[message[:45]] = default
        return real_text(message, default, validate)

    monkeypatch.setattr(ui, "confirm", confirm)
    monkeypatch.setattr(ui, "select", select)
    monkeypatch.setattr(ui, "text", text)
    s = _state()
    openclaw.prompt(s)

    def default_of(prefix: str):
        (value,) = [v for k, v in defaults.items() if k.startswith(prefix)]
        return value

    for prefix in ("Configure this machine as an OpenClaw host", "Install the gateway guard hook",
                   "Install the openclaw-* systemd units (daily", "Apply the canonical OpenClaw config block",
                   "Write an AGENTS.md into every agent works", "Check this host against the documented"):
        assert default_of(prefix) is False, prefix
    assert default_of("Enable the workboard plugin (a shared") is True
    assert default_of("Narrate the Claude Code team's work") == "off"
    assert default_of("Your Telegram user id, for failure not") == ""
    assert default_of("Public host name of the control UI, wit") == ""
    assert default_of("CIDR of the ingress that reaches the ga") == ""
    assert default_of("Where the backup tiers are written") == "/srv/openclaw-backups"
    # v1.9.8 adds one group-id question per routed agent (app, infra, docs here); with no binding yet
    # each defaults to empty, which leaves that agent unbound.
    group_questions = [k for k in defaults if k.startswith("Telegram group chat id for agent")]
    assert len(group_questions) == 3 and all(defaults[k] == "" for k in group_questions)
    # +1: the stale-plugin question; +2: the ADR-0004 resource-safety questions (graceful restart, guards)
    assert len(defaults) == 12 + 3 + 1 + 2, sorted(defaults)
    assert default_of("Let the health check restart the gateway") == "notify"
    assert default_of("Let setup fill the gateway's resource guard") == "on"


def test_declining_the_master_question_asks_nothing_else(sim, script):
    """Declining the host master question skips every HOST question: the invariant is unchanged.
    The one extra question is the stale-plugin check, which is the OpenClaw cockpit's own and not a
    host answer. It gates a report (`ai-resources doctor`) that applies to a host on engine `keep`
    that never opted into the host section, which is the case it exists for."""
    script.answers["Configure this machine as an OpenClaw host"] = False
    s = _state()
    openclaw.prompt(s)
    assert s.openclaw.host is False
    # the stale-plugin question is the cockpit's own, not the host section's: it is still asked
    assert len(script.asked) == 2 and "When should `ai-resources doctor` report that" in script.asked[1]


def test_prompt_records_every_answer(sim, script):
    s = _state()
    openclaw.prompt(s)
    o = s.openclaw
    assert (o.host, o.host_narration, o.host_guard, o.host_units, o.host_config) == (True, "milestones", True, True, True)
    assert (o.host_operator_id, o.host_backup_dir, o.host_domain, o.host_pod_cidr) == (
        "123456789", "/srv/backups", "ai.example.org", "10.9.0.0/24")
    assert (o.host_workboard, o.host_agents_md, o.host_check) == (True, True, False)
    assert not any("token" in q.lower() or "password" in q.lower() for q in script.asked), "secrets are never asked"


def test_narration_off_stores_an_empty_level(sim, script):
    script.answers["Narrate the Claude Code team's work"] = "off"
    s = _state()
    openclaw.prompt(s)
    assert s.openclaw.host_narration == ""


def test_the_host_values_are_validated_as_they_are_typed(sim, script):
    s = _state()
    openclaw.prompt(s)
    domain = script.validators["Public host name of the control UI, without scheme (empty: skip those keys):"]
    cidr = script.validators["CIDR of the ingress that reaches the gateway, e.g. 10.42.0.0/24 (empty: skip):"]
    operator = script.validators["Your Telegram user id, for failure notices (empty: no notices):"]
    assert domain("ai.example.org") is True and isinstance(domain("https://ai.example.org"), str)
    assert cidr("10.9.0.0/24") is True and isinstance(cidr("not-a-cidr"), str)
    assert operator("123456789") is True and isinstance(operator("me@example.org"), str)


def test_the_domain_and_cidr_are_only_asked_when_the_config_block_is_wanted(sim, script):
    script.answers["Apply the canonical OpenClaw config block (heartbeats"] = False
    s = _state()
    openclaw.prompt(s)
    assert not any("Public host name" in q or "CIDR of the ingress" in q for q in script.asked)
    assert s.openclaw.host_domain == "" and s.openclaw.host_pod_cidr == ""


# --- configure: every section -------------------------------------------------------------------------

def test_configure_applies_every_section(sim, script):
    sim.write_settings({"PreToolUse": [USER_HOOK], "UserPromptSubmit": [LEGACY_HOOK]})
    s = _state()
    changed_files = _run_wizard(s)
    o = s.openclaw

    # kit-host.env carries the answers the scripts and the narration hook read.
    env = host.read_host_env(sim.env_file)
    assert env == {"OPENCLAW_OWNER_TELEGRAM_ID": "123456789", "OPENCLAW_BACKUP_DIR": "/srv/backups",
                   "OPENCLAW_PUBLIC_DOMAIN": "ai.example.org", "OPENCLAW_POD_CIDR": "10.9.0.0/24",
                   "OPENCLAW_NARRATION": "milestones", "OPENCLAW_GUARD": "1",
                   "OPENCLAW_GRACEFUL_RESTART": "notify", "OPENCLAW_RESOURCE_GUARDS": "on"}

    # Hooks: the kit's are in, the user's own stays, the hand-installed legacy copy is replaced.
    hooks = sim.hooks()
    commands = [h["command"] for ev in hooks.values() for e in ev for h in e["hooks"]]
    assert sum("openclaw_team_progress.py" in c for c in commands) == 4
    assert sum("openclaw_gateway_guard.py" in c for c in commands) == 1
    assert any("kubectl-context-guard.py" in c for c in commands)
    assert not any("openclaw-team-progress.py" in c for c in commands)
    assert o.host_hooks_applied is True

    # Units: ten files in the user unit dir, every timer enabled, systemd reloaded.
    assert sorted(p.name for p in sim.units.iterdir()) == sorted(host.UNIT_NAMES)
    assert sim.systemd.enabled == set(host.TIMER_NAMES)
    assert ["systemctl", "--user", "daemon-reload"] in sim.systemd.calls
    assert o.host_units_applied is True

    # Config: validated with --dry-run first, then applied atomically; secrets are not part of it.
    # ADR-0003: the whole patch is validated first; the hot half is applied as before and the restart-required
    # half (gateway.bind) goes in a second write, inside a drained window.
    from ai_resources import openclaw_reload_rules as rr
    dry_whole, dry_restart = sim.oc.dry_patches()[0], sim.oc.dry_patches()[1]
    (args, patch), (restart_args, restart_patch) = sim.oc.real_patches()
    assert rr.deep_merge(patch, restart_patch) == dry_whole[1] and "--stdin" in args
    assert restart_patch == {"gateway": {"bind": "tailnet"}} and dry_restart[1] == restart_patch
    assert sim.oc.calls.index(dry_whole) < sim.oc.calls.index((args, patch)) < sim.oc.calls.index((restart_args, restart_patch))
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    assert doc["gateway"]["bind"] == "tailnet" and doc["gateway"]["publicOrigin"] == "https://ai.example.org"
    assert doc["channels"]["telegram"]["allowedUsers"] == [111111111]           # channels stay the user's
    assert doc["channels"]["telegram"]["groups"] == json.loads(sim.original_config)["channels"]["telegram"]["groups"]
    assert set(patch["channels"]) == {"telegram"} and set(patch["channels"]["telegram"]) == {"streaming"}
    assert o.host_config_changes and o.config_path == str(sim.cfg)
    assert sim.cfg in changed_files

    # Workboard is enabled through OpenClaw's own command.
    assert (["plugins", "enable", "workboard"], None) in sim.oc.calls
    assert o.host_workboard_applied == "enabled-by-kit"

    # AGENTS.md: a template where there was none; an existing file keeps every byte outside the kit
    # block (v1.9.7: the block is refreshed in it too); the engine-owned worker gets the block alone.
    for aid in ("main", "infra", "docs"):
        assert (sim.workspaces[aid] / "AGENTS.md").is_file()
    app = (sim.workspaces["app"] / "AGENTS.md").read_text(encoding="utf-8")
    assert outside_block(app) == "# mine\n" and "kit-orchestration" in app and "sessions_spawn" in app
    worker = (sim.workspaces["claude"] / "AGENTS.md").read_text(encoding="utf-8")
    assert "kit-orchestration" in worker and "sessions_spawn" not in worker
    assert len(o.host_agents_md_written) == 3

    # The unpinned MCP server is reported, never rewritten; the gateway is never restarted.
    assert any("supa" in m and "@latest" in m for m in script.messages("warn"))
    assert doc["mcp"]["servers"] == json.loads(sim.original_config)["mcp"]["servers"]
    assert section.RESTART_NOTE in script.messages("warn")
    for argv in [a for a, _ in sim.oc.calls] + sim.systemd.calls:
        assert "restart" not in argv and "doctor" not in argv
    # The only stop/start of the gateway is the confirmed drained window that carries gateway.bind
    # (ADR-0003): one stop, one start, the second after the write.
    gateway_calls = [c for c in sim.systemd.calls if "openclaw-gateway.service" in c and c[2] in ("stop", "start")]
    assert [c[2] for c in gateway_calls] == ["stop", "start"]


def test_the_status_line_reports_what_was_applied(sim, script):
    s = _state()
    _run_wizard(s)
    line = wizard._openclaw_status_line(s)
    assert line.startswith("OpenClaw host: ")
    for part in ("narration milestones, guard", "units", "config (", "workboard", "3 AGENTS.md"):
        assert part in line
    assert wizard._openclaw_status_line(_state()) == ""


def test_the_team_hook_alone_needs_no_guard(sim, script):
    script.answers["Install the gateway guard hook"] = False
    s = _state()
    _run_wizard(s)
    commands = [h["command"] for ev in sim.hooks().values() for e in ev for h in e["hooks"]]
    assert sum("openclaw_team_progress.py" in c for c in commands) == 4
    assert not any("openclaw_gateway_guard.py" in c for c in commands)


def test_hooks_are_not_registered_without_claude_code(sim, script):
    s = _state()
    s.cockpits["claude"] = state.CockpitState(installed=False)
    _run_wizard(s)
    assert not sim.settings.exists()
    assert s.openclaw.host_hooks_applied is False
    assert any("Claude Code is not installed" in m for m in script.messages("warn"))


def test_invalid_host_values_apply_nothing(sim, script):
    script.answers["Public host name"] = "https://ai.example.org/"
    s = _state()
    before = sim.files()
    _run_wizard(s)
    # The host section applied nothing; only the kit block (engine- and host-independent) may appear.
    assert without_blocks(sim.files()) == without_blocks(before)
    assert not sim.env_file.exists() and not sim.oc.real_patches() and not sim.systemd.calls
    assert any("nothing applied" in m for m in script.messages("error"))


# --- consent gates -------------------------------------------------------------------------------------

def test_a_declined_gate_leaves_the_running_gateway_alone(sim, script):
    for gate in GATES:
        script.answers[gate] = False
    s = _state()
    _run_wizard(s)
    assert not sim.units.exists() and not sim.oc.real_patches()
    assert not any(a[:2] == ["plugins", "enable"] for a, _ in sim.oc.calls)
    assert not [c for c in sim.systemd.calls if c[:3] in (["systemctl", "--user", "enable"], ["systemctl", "--user", "daemon-reload"])]
    o = s.openclaw
    assert (o.host_units_applied, o.host_config_changes, o.host_workboard_applied) == (False, [], "")
    # The local pieces do not depend on those gates.
    assert sim.env_file.is_file() and o.host_hooks_applied and o.host_agents_md_written


def test_an_unattended_run_never_touches_the_gateway_but_keeps_the_local_pieces(sim, script):
    s = _state()
    _run_wizard(s)                       # the interactive run that agreed to everything
    # Another host: same answers saved, but the units and config were never applied there.
    for p in sim.units.iterdir():
        p.unlink()
    sim.cfg.write_bytes(sim.original_config)
    sim.systemd.enabled.clear()
    sim.oc.calls.clear()
    script.non_interactive = True
    script.asked.clear()
    _configure(s)
    assert not sim.oc.real_patches()
    assert not [p for p in sim.units.iterdir()]
    assert not any(a[:2] == ["plugins", "enable"] for a, _ in sim.oc.calls)
    assert any("Skipped (needs an interactive confirm)" in m for m in script.messages("info"))


def test_a_rejected_dry_run_fails_closed_without_a_per_key_fallback(sim, script):
    sim.oc.dry_run_ok = False
    script.answers["Enable the workboard plugin (a shared"] = False   # its own section; not under test here
    s = _state()
    _run_wizard(s)
    assert sim.oc.dry_patches() and not sim.oc.real_patches()
    assert not any(a[:2] == ["config", "set"] for a, _ in sim.oc.calls)
    assert s.openclaw.host_config_changes == []
    assert sim.cfg.read_bytes() == sim.original_config
    assert any("rejected the config patch in a dry run" in m for m in script.messages("error"))


# --- second run ----------------------------------------------------------------------------------------

def test_a_second_run_is_a_byte_level_no_op(sim, script):
    sim.write_settings({"PreToolUse": [USER_HOOK]})
    s = _state()
    _run_wizard(s)
    files_before = sim.files()
    state_before = asdict(s)
    mutations = lambda: [a for a, _ in sim.oc.calls if a[:2] in (["config", "patch"], ["plugins", "enable"])] \
        + [c for c in sim.systemd.calls if len(c) > 2 and c[2] in ("enable", "daemon-reload", "disable")]
    calls_before = len(mutations())

    script.asked.clear()
    script.log.clear()
    changed = _run_wizard(s)

    assert sim.files() == files_before
    assert asdict(s) == state_before
    assert len(mutations()) == calls_before, "a second run must not mutate anything"
    assert sim.cfg not in changed
    # It re-asks the wizard questions (they carry the saved answers) but no consent gate.
    assert not any(any(g in q for g in GATES) for q in script.asked)


def test_a_third_run_after_a_fresh_prompt_keeps_the_saved_answers(sim, script):
    s = _state()
    _run_wizard(s)
    first = asdict(s)
    _run_wizard(s)
    _run_wizard(s)
    assert asdict(s) == first


def test_a_dry_run_writes_nothing_and_sends_no_real_patch(sim, script):
    s = _state()
    before = sim.files()
    _run_wizard(s, dry_run=True)
    assert sim.files() == before
    assert not sim.oc.real_patches()
    assert not [c for c in sim.systemd.calls if c[2] in ("enable", "daemon-reload")]
    assert not any(a[:2] == ["plugins", "enable"] for a, _ in sim.oc.calls)
    assert not s.openclaw.host_hooks_applied and not s.openclaw.host_config_changes
    details = " ".join(script.messages("detail"))
    for expected in ("Would update", "Would register the OpenClaw hooks", "Would install the openclaw-* units",
                     "Dry run accepted by OpenClaw", "Would run `openclaw plugins enable workboard`", "Would write"):
        assert expected in details


# --- teardown ------------------------------------------------------------------------------------------

def test_teardown_removes_exactly_what_configure_added(sim, script):
    sim.write_settings({"PreToolUse": [USER_HOOK]})
    s = _state()
    _run_wizard(s)
    hand_written = sim.workspaces["main"] / "AGENTS.md"
    assert hand_written.is_file()

    removed = openclaw.teardown(s)

    assert str(sim.cfg) in removed
    assert sim.cfg.read_bytes() == sim.original_config, "openclaw.json is back to what it was"
    assert not sim.env_file.exists()
    assert not sim.units.exists() or not list(sim.units.iterdir())
    assert sim.systemd.enabled == set()
    assert not hand_written.exists()
    assert (sim.workspaces["app"] / "AGENTS.md").read_text(encoding="utf-8") == "# mine\n"
    hooks = sim.hooks()
    assert hooks == {"PreToolUse": [USER_HOOK]}, "only the user's own hook is left"
    assert (["plugins", "disable", "workboard"], None) in sim.oc.calls
    assert not section.applied_any(s.openclaw) and s.openclaw == state.OpenClawState()
    for argv in [a for a, _ in sim.oc.calls] + sim.systemd.calls:
        assert "restart" not in argv and "doctor" not in argv


def test_teardown_restores_a_host_env_the_user_already_had(sim, script):
    sim.env_file.parent.mkdir(parents=True)
    original = "# mine\nOPENCLAW_BACKUP_DIR=/old\nMY_OWN=1\n"
    sim.env_file.write_text(original, encoding="utf-8")
    s = _state()
    _run_wizard(s)
    env = host.read_host_env(sim.env_file)
    assert env["OPENCLAW_BACKUP_DIR"] == "/srv/backups" and env["MY_OWN"] == "1"

    openclaw.teardown(s)

    assert sim.env_file.read_text(encoding="utf-8") == original


def test_teardown_keeps_an_agents_md_the_user_edited(sim, script):
    s = _state()
    _run_wizard(s)
    edited = sim.workspaces["infra"] / "AGENTS.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nMy own rule.\n", encoding="utf-8")
    kept = edited.read_bytes()

    openclaw.teardown(s)

    assert edited.read_bytes() == kept, "an edited file is the user's now"
    assert not (sim.workspaces["docs"] / "AGENTS.md").exists()


def test_teardown_does_not_disable_a_workboard_the_user_had_enabled(sim, script):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["plugins"]["entries"]["workboard"]["enabled"] = True
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    original = json.loads(sim.cfg.read_text(encoding="utf-8"))
    s = _state()
    _run_wizard(s)
    assert s.openclaw.host_workboard_applied == "already-enabled"
    assert not any(a[:2] == ["plugins", "enable"] for a, _ in sim.oc.calls)

    openclaw.teardown(s)

    assert not any(a[:2] == ["plugins", "disable"] for a, _ in sim.oc.calls)
    assert json.loads(sim.cfg.read_text(encoding="utf-8")) == original


def test_teardown_is_a_no_op_when_the_kit_never_configured_a_host(sim, script):
    s = _state()
    before = sim.files()
    assert openclaw.teardown(s) == []
    assert sim.files() == before and not sim.oc.calls


def test_a_failed_config_restore_is_reported_and_keeps_the_record(sim, script, monkeypatch):
    s = _state()
    _run_wizard(s)
    recorded = list(s.openclaw.host_config_changes)
    real = sim.oc.__call__

    def failing(args, stdin=None, timeout=120):
        if args[:2] == ["config", "patch"]:
            sim.oc.calls.append((list(args), json.loads(stdin) if stdin else None))
            return 1, "gateway busy"
        return real(args, stdin, timeout)

    monkeypatch.setattr(openclaw, "_openclaw", failing)
    assert openclaw.teardown(s) == []
    assert s.openclaw.host_config_changes == recorded, "the record stays so a later teardown can retry"
    assert any("host config teardown failed" in m for m in script.messages("error"))


def test_unticking_the_master_answer_offers_to_undo_the_earlier_run(sim, script):
    s = _state()
    _run_wizard(s)
    script.answers["Configure this machine as an OpenClaw host"] = False
    script.answers["The kit configured this OpenClaw host earlier"] = True

    _run_wizard(s)

    assert sim.cfg.read_bytes() == sim.original_config
    assert not sim.env_file.exists() and not section.applied_any(s.openclaw)


def test_unticking_the_master_answer_and_declining_the_undo_changes_nothing(sim, script):
    s = _state()
    _run_wizard(s)
    before = sim.files()
    script.answers["Configure this machine as an OpenClaw host"] = False
    script.answers["The kit configured this OpenClaw host earlier"] = False

    _run_wizard(s)

    assert sim.files() == before and section.applied_any(s.openclaw)


# --- architect conditions: teardown removes no more and no less than configure added -------------------------

def test_teardown_disables_exactly_the_timers_the_kit_enabled(sim, script):
    already = {host.TIMER_NAMES[0], host.TIMER_NAMES[3]}
    sim.systemd.enabled |= already
    s = _state()
    _run_wizard(s)
    assert sim.systemd.enabled == set(host.TIMER_NAMES)
    assert set(s.openclaw.host_timers_enabled) == set(host.TIMER_NAMES) - already

    openclaw.teardown(s)

    assert sim.systemd.enabled == already, "timers that were enabled before the kit came stay enabled"
    disabled = {argv[4] for argv in sim.systemd.calls if argv[:3] == ["systemctl", "--user", "disable"]}
    assert disabled == set(host.TIMER_NAMES) - already
    assert s.openclaw.host_timers_enabled == []


def test_a_second_run_does_not_claim_a_timer_the_first_run_left_to_the_user(sim, script):
    sim.systemd.enabled.add(host.TIMER_NAMES[1])
    s = _state()
    _run_wizard(s)
    first = list(s.openclaw.host_timers_enabled)
    _run_wizard(s)
    assert s.openclaw.host_timers_enabled == first and host.TIMER_NAMES[1] not in first


def test_teardown_puts_back_the_hand_installed_hook_the_kit_replaced(sim, script):
    original = {"PreToolUse": [USER_HOOK, {"matcher": "*", "hooks": [
        {"type": "command", "command": "python3 $HOME/.claude/hooks/openclaw-team-progress.py", "timeout": 30}]}],
        "Stop": [LEGACY_HOOK], "UserPromptSubmit": [LEGACY_HOOK]}
    sim.write_settings(original)
    s = _state()
    _run_wizard(s)
    assert not any("openclaw-team-progress.py" in h["command"] for ev in sim.hooks().values()
                   for e in ev for h in e["hooks"]), "no double narration while the kit hook is in"
    assert len(s.openclaw.host_legacy_hooks) == 3
    _run_wizard(s)
    assert len(s.openclaw.host_legacy_hooks) == 3, "a second run does not record the same entries again"

    openclaw.teardown(s)

    assert sim.hooks() == original
    assert s.openclaw.host_legacy_hooks == []


def test_teardown_restores_a_legacy_hook_that_shared_an_entry_with_the_users_own(sim, script):
    shared = {"matcher": "Bash", "hooks": [USER_HOOK["hooks"][0], LEGACY_HOOK["hooks"][0]]}
    sim.write_settings({"PreToolUse": [shared]})
    s = _state()
    _run_wizard(s)
    assert USER_HOOK["hooks"][0] in [h for e in sim.hooks()["PreToolUse"] for h in e["hooks"]]

    openclaw.teardown(s)

    commands = [h["command"] for e in sim.hooks()["PreToolUse"] for h in e["hooks"]]
    assert sorted(commands) == sorted(h["command"] for h in shared["hooks"])


def test_nothing_is_recorded_as_replaced_when_no_legacy_hook_existed(sim, script):
    sim.write_settings({"PreToolUse": [USER_HOOK]})
    s = _state()
    _run_wizard(s)
    assert s.openclaw.host_legacy_hooks == []


def test_a_literal_token_is_never_persisted_or_restored(sim, script, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "ghp_from_env")
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["gateway"]["controlUi"]["github"] = {"token": "ghp_LITERALSECRET"}
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    s = _state()
    _run_wizard(s)
    ref = {"source": "env", "provider": "default", "id": "GH_TOKEN"}
    assert json.loads(sim.cfg.read_text(encoding="utf-8"))["gateway"]["controlUi"]["github"]["token"] == ref
    [change] = [c for c in s.openclaw.host_config_changes if c["path"][-1] == "token"]
    assert change["had"] is True and change["previous"] is None and change["secret"] is True

    state.save(s)
    saved = state.state_path()
    assert "ghp_LITERALSECRET" not in saved.read_text(encoding="utf-8")
    assert oct(saved.stat().st_mode & 0o777) == "0o600"

    openclaw.teardown(s)

    for _args, patch in sim.oc.real_patches():
        assert "ghp_LITERALSECRET" not in json.dumps(patch)
    restore = sim.oc.real_patches()[-1][1]
    assert "github" not in restore.get("gateway", {}).get("controlUi", {}), "the credential leaf is left alone"
    assert any("openclaw configure" in m for m in script.messages("warn"))


def test_the_state_file_is_owner_only_and_an_older_wide_one_is_tightened(sim):
    s = _state()
    state.save(s)
    path = state.state_path()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.chmod(0o644)
    state.save(s)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_unticking_the_master_answer_does_not_report_the_config_as_changed(sim, script):
    s = _state()
    _run_wizard(s)
    script.answers["Configure this machine as an OpenClaw host"] = False
    script.answers["The kit configured this OpenClaw host earlier"] = True
    s.openclaw.host = False
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))

    changed = section.configure(s, doc, "/kit", [], dry_run=False, apply_patch=openclaw.apply_patch,
                                oc=sim.oc, config_path=sim.cfg)

    assert changed is False
    assert not section.applied_any(s.openclaw), "the teardown itself did run"


# --- the host check ------------------------------------------------------------------------------------

class _Result:
    def __init__(self, step, status):
        self.step, self.status = step, status


def _stub_bootstrap(monkeypatch, results):
    calls = []

    def fake(runner=None, **kw):
        calls.append(kw)
        return results, None

    monkeypatch.setattr(host, "bootstrap", fake)
    return calls


def test_the_check_reports_first_and_skips_the_units_it_already_installed(sim, script, monkeypatch):
    script.answers["Check this host against the documented setup"] = True
    script.answers["Apply the 1 change(s) the check found"] = False
    calls = _stub_bootstrap(monkeypatch, [_Result("node", "satisfied"), _Result("boot", "would-change")])
    _run_wizard(_state())
    assert len(calls) == 1 and calls[0]["dry_run"] is True and calls[0]["skip"] == ("units",)
    assert any("boot (would-change)" in m for m in script.messages("warn"))


def test_the_check_applies_a_fix_only_after_its_own_confirm(sim, script, monkeypatch):
    script.answers["Check this host against the documented setup"] = True
    script.answers["Apply the 1 change(s) the check found"] = True
    script.answers["Enable boot start?"] = True
    calls = _stub_bootstrap(monkeypatch, [_Result("boot", "would-change")])
    _run_wizard(_state())
    assert [c["dry_run"] for c in calls] == [True, False]
    assert calls[1]["confirm"]("boot", "Enable boot start") is True
    assert "Enable boot start?" in script.asked


def test_an_unattended_check_reports_but_never_fixes(sim, script, monkeypatch):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_check = True
    script.non_interactive = True
    calls = _stub_bootstrap(monkeypatch, [_Result("boot", "would-change")])
    _configure(s)
    assert [c["dry_run"] for c in calls] == [True]


def test_a_clean_host_check_says_so(sim, script, monkeypatch):
    script.answers["Check this host against the documented setup"] = True
    _stub_bootstrap(monkeypatch, [_Result("node", "satisfied")])
    _run_wizard(_state())
    assert any("everything matches" in m for m in script.messages("ok"))


# --- the real machine stays out of reach --------------------------------------------------------------

def test_the_fixture_points_every_write_target_into_tmp(sim, tmp_path):
    for target in (sim.env_file, sim.units, sim.settings, sim.cfg):
        assert str(target).startswith(str(tmp_path.parent)), target


# --- step 10: setup ends by verifying every selected tooling ------------------------------------------------------

class _Console:
    def print(self, *_a, **_k):
        pass


def _step10_env(monkeypatch, findings):
    from ai_resources import verify
    calls, sections = [], []
    monkeypatch.setattr(verify, "run_all", lambda s, ids=None, ctx=None: calls.append(list(ids)) or findings)
    monkeypatch.setattr(ui, "section", lambda n, total, title: sections.append((n, total, title)))
    monkeypatch.setattr(ui, "console", lambda: _Console())
    return calls, sections


def test_step10_prints_its_header_and_one_line_per_finding_and_returns_zero_on_all_clear(script, monkeypatch):
    from ai_resources.verify import Finding
    calls, sections = _step10_env(monkeypatch, [Finding("ok", "claude", "block present"),
                                                Finding("ok", "openclaw", "gateway up")])
    assert wizard._step10_verify(_state(), ["claude", "openclaw"]) == 0
    assert sections == [(10, 10, "Verification")] and calls == [["claude", "openclaw"]]
    assert script.messages("ok")[:2] == ["claude: block present", "openclaw: gateway up"]
    assert script.messages("ok")[-1].startswith("All 2 selected toolings reached the expected state")


def test_step10_returns_non_zero_on_an_error_and_zero_on_a_warn(script, monkeypatch):
    from ai_resources.verify import Finding
    _step10_env(monkeypatch, [Finding("ok", "claude", "fine"), Finding("error", "openclaw", "block gone", "re-run setup")])
    assert wizard._step10_verify(_state(), ["claude", "openclaw"]) == 1
    assert "1 of 2 selected toolings did not reach the expected state" in script.messages("error")[-1]
    assert "re-run setup" in " ".join(script.messages("detail"))
    _step10_env(monkeypatch, [Finding("warn", "openclaw", "off-box not configured", "add the env line")])
    assert wizard._step10_verify(_state(), ["claude", "openclaw"]) == 0


def test_step10_judges_only_the_selected_cockpits(script, monkeypatch):
    import types
    from ai_resources import verify
    from ai_resources.setup import cockpits
    from ai_resources.setup import state as st
    seen = []
    fake = {cid: types.SimpleNamespace(verify=lambda ctx, _c=cid: seen.append(_c) or [verify.Finding("ok", _c, "x")])
            for cid in ("claude", "cursor", "openclaw")}
    monkeypatch.setattr(cockpits, "ALL", fake)
    monkeypatch.setattr(ui, "section", lambda *a: None)
    monkeypatch.setattr(ui, "console", lambda: _Console())
    assert wizard._step10_verify(st.SetupState(), ["claude", "openclaw"]) == 0
    assert seen == ["claude", "openclaw"]


def test_step10_with_nothing_applied_says_so(script, monkeypatch):
    calls, _ = _step10_env(monkeypatch, [])
    assert wizard._step10_verify(_state(), []) == 0 and calls == []


def _drive_run(monkeypatch, tmp_path, *, apply_rc, targets, findings):
    """wizard.run with every step but 9 and 10 stubbed out."""
    import argparse
    from ai_resources.setup import state as st
    calls, sections = _step10_env(monkeypatch, findings)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(st, "load", lambda: st.SetupState())
    monkeypatch.setattr(st, "save", lambda s: None)
    monkeypatch.setattr(st, "is_first_run", lambda: True)
    # "every step but 9 and 10": the model-selection steps added by plan v2 are stubbed like the others.
    for step in ("_step1_mode", "_step2_cockpits", "_step6_single_model_profile", "_step7_cockpit_config",
                 "_step_models", "_step_summary"):
        monkeypatch.setattr(wizard, step, lambda *a, **k: 0)

    def apply(s, dry_run=False):
        wizard._APPLIED_TARGETS = list(targets)
        return apply_rc
    monkeypatch.setattr(wizard, "_step9_apply", apply)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    args = argparse.Namespace(dry_run=False, non_interactive=False, profile="")
    return wizard.run(args), calls, sections


def test_run_exits_non_zero_on_an_error_finding_and_zero_on_a_warn(script, monkeypatch, tmp_path):
    from ai_resources.verify import Finding
    rc, calls, _ = _drive_run(monkeypatch, tmp_path, apply_rc=0, targets=["claude", "openclaw"],
                              findings=[Finding("error", "openclaw", "block gone")])
    assert rc == 1 and calls == [["claude", "openclaw"]]
    rc, calls, _ = _drive_run(monkeypatch, tmp_path, apply_rc=0, targets=["claude"],
                              findings=[Finding("warn", "claude", "meh")])
    assert rc == 0 and calls == [["claude"]]


def test_run_skips_verification_when_apply_failed(script, monkeypatch, tmp_path):
    rc, calls, sections = _drive_run(monkeypatch, tmp_path, apply_rc=1, targets=["claude"], findings=[])
    assert rc == 1 and calls == [] and (10, 10, "Verification") not in sections


# --- fill-only summary and the overrides file (ADR-0003) -----------------------------------------------------

def test_without_overrides_the_wizard_keeps_operator_values_and_says_so(sim, script):
    sim.overrides_file.unlink()
    s = _state()
    _run_wizard(s)
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    original = json.loads(sim.original_config)
    assert doc["gateway"]["bind"] == original["gateway"]["bind"]            # lan, the operator's
    assert doc["tools"]["profile"] == original["tools"]["profile"]
    info = " | ".join(script.messages("info"))
    assert "kept your value:" in info and "gateway.bind" in info
    assert "filled:" in info and "gateway.publicOrigin" in info
    assert all(c["path"] != ["gateway", "bind"] for c in s.openclaw.host_config_changes)


def test_a_broken_overrides_file_skips_the_config_step_and_does_not_crash(sim, script):
    sim.overrides_file.write_text('{"force": ["bad path"]}', encoding="utf-8")
    s = _state()
    _run_wizard(s)
    assert sim.oc.real_patches() == [] or all(p.get("gateway") is None for _a, p in sim.oc.real_patches())
    assert any("kit-host-overrides.json5" in m for m in script.messages("error"))
    assert s.openclaw.host_config_changes == []


# --- adopting the hand-installed health-restart timer --------------------------------------------------------------

def _put_hand_copy(sim):
    script = sim.home / ".local" / "bin" / "openclaw-health-restart.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\nsystemctl --user restart x  # hand copy\n", encoding="utf-8")
    return script


def test_an_unattended_run_leaves_the_hand_copy_and_says_how_to_adopt_it(sim, script):
    hand = _put_hand_copy(sim)
    s = _state()
    openclaw.prompt(s)
    script.non_interactive = True
    _configure(s)
    assert hand.exists()
    assert not (sim.home / ".openclaw" / "backup" / "hand-units").exists()
    assert any("hand-installed openclaw-health-restart" in m and "interactively" in m for m in script.messages("info"))


def test_an_interactive_run_adopts_it_and_teardown_puts_the_script_back(sim, script):
    hand = _put_hand_copy(sim)
    original = hand.read_text(encoding="utf-8")
    s = _state()
    _run_wizard(s)
    assert not hand.exists()
    [backup] = list((sim.home / ".openclaw" / "backup" / "hand-units").iterdir())
    assert (backup / "openclaw-health-restart.sh").read_text(encoding="utf-8") == original
    assert s.openclaw.host_hand_moved == {str(hand): str(backup / "openclaw-health-restart.sh")}
    assert any(c[:4] == ["systemctl", "--user", "disable", "--now"] and c[4] == "openclaw-health-restart.timer"
               for c in sim.systemd.calls)
    openclaw.teardown(s)
    assert hand.read_text(encoding="utf-8") == original, "nothing was deleted; the hand copy is back"
    assert s.openclaw.host_hand_moved == {}


def _put_hand_units(sim):
    """A hand-installed health-restart pair in the user unit dir, next to the script under ~/.local/bin."""
    files = {}
    sim.units.mkdir(parents=True, exist_ok=True)
    for name in ("openclaw-health-restart.service", "openclaw-health-restart.timer"):
        p = sim.units / name
        p.write_bytes(f"# hand copy of {name}\n[Unit]\nDescription=mine\n".encode())
        files[p] = p.read_bytes()
    return files


def test_teardown_puts_the_hand_installed_units_back_over_the_kits_copy(sim, script):
    hand = _put_hand_copy(sim)
    hand.chmod(0o755)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    for p, original in units.items():
        assert p.read_bytes() != original, "setup installed the kit's unit over the adopted one"
    openclaw.teardown(s)
    for p, original in units.items():
        assert p.read_bytes() == original, f"{p.name}: the operator's original bytes are back"
    assert hand.stat().st_mode & 0o111, "the exec bit of the hand script survives the round trip"
    assert s.openclaw.host_hand_moved == {}


def test_teardown_warns_and_leaves_a_unit_the_operator_changed_after_adoption(sim, script):
    _put_hand_copy(sim)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    target = next(iter(units))
    target.write_text("# edited after adoption\n", encoding="utf-8")
    openclaw.teardown(s)
    assert target.read_text(encoding="utf-8") == "# edited after adoption\n"
    assert any(target.name in m and "left" in m for m in script.messages("warn"))


def test_teardown_restores_a_unit_adopted_before_the_kit_sha_was_recorded(sim, script):
    """State written under 2.1.0 has no host_hand_kit_sha: teardown falls back to hashing the rendered unit."""
    _put_hand_copy(sim)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    s.openclaw.host_hand_kit_sha = {}
    openclaw.teardown(s)
    for p, original in units.items():
        assert p.read_bytes() == original, f"{p.name}: restored through the fallback"


def test_the_moved_aside_original_survives_when_teardown_leaves_an_edited_unit(sim, script):
    _put_hand_copy(sim)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    target = next(iter(units))
    target.write_text("# edited after adoption\n", encoding="utf-8")
    openclaw.teardown(s)
    backups = [p for d in (sim.home / ".openclaw" / "backup" / "hand-units").iterdir() for p in d.iterdir()]
    saved = next(p for p in backups if p.name == target.name)
    assert saved.read_bytes() == units[target], "the operator's original is still in the moved-aside copy"


def test_verify_flags_a_hand_copy_that_is_still_present(sim, script):
    from ai_resources.setup.cockpits import _openclaw_host as sec
    assert sec._hand_copy_findings() == []
    _put_hand_copy(sim)
    [finding] = sec._hand_copy_findings()
    assert finding.level == "warn" and "openclaw-health-restart.sh" in finding.message


# --- ADR-0004: the resource-safety answers -------------------------------------------------------------------------

def test_the_graceful_restart_answer_is_written_and_a_rerun_is_idempotent(sim, script):
    script.answers["Let the health check restart the gateway gracefully"] = "on"
    script.answers["Let setup fill the gateway's resource guards"] = "off"
    s = _state()
    _run_wizard(s)
    env = host.read_host_env(sim.env_file)
    assert env["OPENCLAW_GRACEFUL_RESTART"] == "on" and env["OPENCLAW_RESOURCE_GUARDS"] == "off"
    assert s.openclaw.host_graceful_restart == "on" and s.openclaw.host_resource_guards == "off"
    before = sim.env_file.read_bytes()
    _run_wizard(s)
    assert sim.env_file.read_bytes() == before, "a second run with the same answers changes nothing"
    # `off` really keeps the guard keys out of the live config
    doc = json.loads(sim.cfg.read_text())
    assert "sessionIdleTtlMs" not in (doc.get("mcp") or {})
    assert "timeoutSeconds" not in doc["agents"]["defaults"]


def test_guards_on_fill_both_keys_in_the_live_config(sim, script):
    s = _state()
    _run_wizard(s)
    doc = json.loads(sim.cfg.read_text())
    assert doc["mcp"]["sessionIdleTtlMs"] == 1800000
    assert doc["agents"]["defaults"]["timeoutSeconds"] == 14400


def test_unattended_runs_keep_an_existing_answer_and_otherwise_take_the_safe_default(sim, script):
    sim.env_file.parent.mkdir(parents=True, exist_ok=True)
    sim.env_file.write_text("OPENCLAW_GRACEFUL_RESTART=on\n", encoding="utf-8")
    seen = {}

    def select(message, choices, default=None, instruction=""):
        seen[message[:40]] = default
        return default                     # what ui.select returns when non-interactive

    import ai_resources.setup.ui as ui_mod
    script.answers["Configure this machine as an OpenClaw host"] = True
    real = ui_mod.select
    ui_mod.select = lambda m, c, default=None, instruction="": select(m, c, default, instruction) \
        if m.startswith(("Let the health", "Let setup fill")) else real(m, c, default, instruction)
    try:
        s = _state()
        openclaw.prompt(s)
    finally:
        ui_mod.select = real
    assert s.openclaw.host_graceful_restart == "on", "an existing value is kept"
    assert s.openclaw.host_resource_guards == "on", "no value: the shipped default"


def test_setup_never_runs_gateway_restart_itself_and_a_second_run_is_a_no_op(sim, script):
    s = _state()
    _run_wizard(s)
    after_first = sim.cfg.read_bytes()
    env_first = sim.env_file.read_bytes()
    sim.oc.calls.clear()
    sim.systemd.calls.clear()
    _run_wizard(s)
    assert sim.cfg.read_bytes() == after_first and sim.env_file.read_bytes() == env_first
    assert sim.oc.real_patches() == [], "the second run writes nothing"
    every = [a for a, _p in sim.oc.calls]
    assert ["gateway", "restart"] not in every
    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in sim.systemd.calls)


# --- 2.1.1 D3: the uploader's named gcloud configuration (opt-in, never activated) -----------------------------------

class FakeGcloud:
    """A gcloud that holds configurations in memory and records every call."""

    def __init__(self, configs=None, account="me@example.org", active="claude-ro"):
        self.calls: list[list[str]] = []
        self.configs: dict[str, dict[str, str]] = configs if configs is not None else {
            "default": {}, "claude-ro": {"auth/credential_file_override": "/agent/cred.json"}}
        self.account = account
        self.active = active

    def __call__(self, argv, **_kw):
        a = list(argv)
        self.calls.append(a)
        assert a[0] == "gcloud", a
        if a[1:3] == ["auth", "list"]:
            return 0, self.account
        if a[1:4] == ["config", "configurations", "list"]:
            return 0, "\n".join(f"{n}\t{str(n == self.active)}" for n in self.configs)
        if a[1:4] == ["config", "configurations", "create"]:
            assert "--no-activate" in a
            self.configs[a[4]] = {}
            return 0, ""
        if a[1:4] == ["config", "configurations", "delete"]:
            self.configs.pop(a[4], None)
            return 0, ""
        if a[1:3] == ["config", "set"]:
            cfg = next(x.split("=", 1)[1] for x in a if x.startswith("--configuration="))
            self.configs[cfg][a[3]] = a[4]
            return 0, ""
        if a[1:3] == ["config", "get"]:
            cfg = next(x.split("=", 1)[1] for x in a if x.startswith("--configuration="))
            return 0, self.configs.get(cfg, {}).get(a[3], "(unset)")
        raise AssertionError(f"unexpected gcloud call {a}")

    def writes(self):
        return [c for c in self.calls if c[1:3] == ["config", "set"] or c[1:4] in (
            ["config", "configurations", "create"], ["config", "configurations", "delete"])]


def test_the_plan_creates_a_named_configuration_that_is_never_activated_and_has_no_override():
    g = FakeGcloud()
    plan = host.offbox_identity_plan(g)
    assert plan["problems"] == [] and plan["account"] == "me@example.org" and plan["project"] == "wildbit-iac"
    flat = [" ".join(s) for s in plan["steps"]]
    assert flat == [
        "gcloud config configurations create openclaw-backup --no-activate",
        "gcloud config set account me@example.org --configuration=openclaw-backup",
        f"gcloud config set auth/impersonate_service_account {host.OFFBOX_SA_DEFAULT} --configuration=openclaw-backup",
        "gcloud config set core/project wildbit-iac --configuration=openclaw-backup"]
    assert not any("credential_file_override" in s or "iam-policy" in s or "policy-binding" in s
                   or s.startswith("gcloud config configurations activate") for s in flat)
    assert g.writes() == [], "planning writes nothing"


def test_apply_is_idempotent_and_a_second_plan_has_no_steps():
    g = FakeGcloud()
    record = host.offbox_identity_apply(g, host.offbox_identity_plan(g))
    assert record == {"name": "openclaw-backup", "account": "me@example.org", "impersonate": host.OFFBOX_SA_DEFAULT,
                      "project": "wildbit-iac"}
    again = host.offbox_identity_plan(g, created=record)
    assert again["steps"] == [] and again["problems"] == []
    assert g.active == "claude-ro", "the active configuration was never changed"
    assert g.configs["claude-ro"] == {"auth/credential_file_override": "/agent/cred.json"}


def test_an_existing_configuration_the_kit_did_not_create_is_refused():
    g = FakeGcloud(configs={"default": {}, "openclaw-backup": {"account": "x"}})
    plan = host.offbox_identity_plan(g)
    assert plan["steps"] == [] and any("did not create it" in p for p in plan["problems"])
    assert host.offbox_identity_apply(g, plan) is None and g.writes() == []


def test_the_protected_configurations_are_never_a_target():
    plan = host.offbox_identity_plan(FakeGcloud(), name="claude-ro")
    assert plan["steps"] == [] and any("never touches" in p for p in plan["problems"])


def test_a_service_account_login_is_not_taken_for_the_operator():
    plan = host.offbox_identity_plan(FakeGcloud(account="ai-ops-ro@proj.iam.gserviceaccount.com"))
    assert plan["steps"] == [] and any("no operator login" in p for p in plan["problems"])


def test_remove_deletes_only_a_kit_created_configuration_that_still_matches():
    g = FakeGcloud()
    record = host.offbox_identity_apply(g, host.offbox_identity_plan(g))
    g.calls.clear()
    assert host.offbox_identity_remove(g, record) is True
    assert [c[1:5] for c in g.writes()] == [["config", "configurations", "delete", "openclaw-backup"]]
    assert "claude-ro" in g.configs and "default" in g.configs


def test_remove_leaves_a_configuration_somebody_changed_or_activated():
    g = FakeGcloud()
    record = host.offbox_identity_apply(g, host.offbox_identity_plan(g))
    g.configs["openclaw-backup"]["account"] = "someone-else@example.org"
    lines: list[str] = []
    assert host.offbox_identity_remove(g, record, out=lines.append) is False
    assert "openclaw-backup" in g.configs and any("changed since" in ln for ln in lines)
    g.configs["openclaw-backup"]["account"] = "me@example.org"
    g.active = "openclaw-backup"
    assert host.offbox_identity_remove(g, record, out=lines.append) is False
    assert "openclaw-backup" in g.configs


def test_the_verb_dry_run_runs_no_write_and_names_the_infra_step(monkeypatch, capsys):
    import argparse
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    args = argparse.Namespace(dry_run=True, remove=False, yes=False, name="openclaw-backup",
                              sa=host.OFFBOX_SA_DEFAULT, account="", project="")
    monkeypatch.setattr(state, "load", lambda: state.SetupState())
    assert host.cmd_offbox_identity(args) == 0
    out = capsys.readouterr().out
    assert "roles/iam.serviceAccountTokenCreator" in out and "--no-activate" in out
    assert g.writes() == []


_HOSTILE_OFFBOX_VALUES = [
    ("sa", "x@p.iam.gserviceaccount.com /usr/bin/id"),
    ("sa", "--configuration=default"),
    ("sa", "not-an-email"),
    ("name", "--activate"),
    ("name", "a b"),
    ("name", "Upper"),
    ("account", "--help"),
    ("account", "me@example.org /usr/bin/id"),
    ("project", "-p"),
    ("project", "p q"),
]


@pytest.mark.parametrize("field,value", _HOSTILE_OFFBOX_VALUES)
def test_the_plan_refuses_a_hostile_value_and_runs_no_gcloud(field, value):
    g = FakeGcloud()
    kwargs = {"account": "me@example.org", field: value}
    plan = host.offbox_identity_plan(g, **kwargs)
    assert plan["steps"] == [] and plan["problems"], plan
    assert host.offbox_identity_apply(g, plan) is None
    assert g.writes() == []


@pytest.mark.parametrize("field,value", _HOSTILE_OFFBOX_VALUES)
def test_the_verb_refuses_a_hostile_flag_and_writes_nothing(field, value, monkeypatch, tmp_path, capsys):
    import argparse
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    env = tmp_path / "kit-host.env"
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    saved: list[object] = []
    monkeypatch.setattr(state, "load", lambda: state.SetupState())
    monkeypatch.setattr(state, "save", lambda s: saved.append(s))
    kw = dict(dry_run=False, remove=False, yes=True, name="openclaw-backup", sa=host.OFFBOX_SA_DEFAULT,
              account="me@example.org", project="")
    kw[field] = value
    assert host.cmd_offbox_identity(argparse.Namespace(**kw)) != 0
    assert "refusing" in capsys.readouterr().out
    assert g.writes() == [] and not env.exists() and saved == []


def test_a_valid_set_of_values_still_passes_the_check():
    g = FakeGcloud()
    plan = host.offbox_identity_plan(g, name="up-1", sa="uploader-sa@my-proj-1.iam.gserviceaccount.com",
                                     account="op.erator+x@example.org", project="my-proj-1")
    assert plan["problems"] == [] and plan["steps"]


def test_the_host_env_refuses_a_value_with_a_space(tmp_path):
    with pytest.raises(ValueError):
        host.write_host_env({"OPENCLAW_OFFBOX_IMPERSONATE_SA": "a@p.iam.gserviceaccount.com /usr/bin/id"},
                            tmp_path / "e")
    assert not (tmp_path / "e").exists()


def test_teardown_keeps_the_record_when_the_configuration_was_left_alone(monkeypatch, tmp_path):
    g = FakeGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    g.configs["openclaw-backup"]["account"] = "someone-else@example.org"
    assert section.teardown(s, apply_patch=lambda *a, **k: (True, ""), oc=lambda *a, **k: (0, "")) is False
    assert "openclaw-backup" in g.configs
    assert s.openclaw.host_offbox_config_created.get("name") == "openclaw-backup", \
        "a failed removal must keep the record so a later teardown can retry"


def test_cockpit_teardown_keeps_the_record_when_the_configuration_was_left_alone(monkeypatch, tmp_path):
    g = FakeGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "openclaw.json"))
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    g.configs["openclaw-backup"]["account"] = "someone-else@example.org"
    openclaw.teardown(s)
    assert "openclaw-backup" in g.configs
    assert s.openclaw.host_offbox_config_created.get("name") == "openclaw-backup", \
        "the cockpit teardown must not reset the state while the configuration is still owed"


def test_setup_offers_the_step_only_with_a_bucket_and_defaults_to_no(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=False, **_k: asked.append(msg) or default)
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **_k: default)
    monkeypatch.setattr(ui, "ask", lambda *a, **k: k.get("default", ""), raising=False)
    monkeypatch.setattr(host, "read_host_env", lambda *a, **k: {})
    o = state.SetupState()
    section.prompt(o, {})
    assert not any("off-box uploader" in m for m in asked)


def test_configure_dry_run_prints_the_commands_and_runs_nothing(monkeypatch):
    g = FakeGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    infos: list[str] = []
    monkeypatch.setattr(ui, "info", infos.append)
    monkeypatch.setattr(ui, "detail", infos.append)
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=True)
    assert g.writes() == [] and any("--no-activate" in m for m in infos)
    assert s.openclaw.host_offbox_config_created == {}


def test_configure_creates_records_and_teardown_removes_only_what_it_made(monkeypatch, tmp_path):
    g = FakeGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    env = tmp_path / "kit-host.env"
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    assert s.openclaw.host_offbox_config_created["name"] == "openclaw-backup"
    text = env.read_text()
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG=openclaw-backup" in text
    assert f"OPENCLAW_OFFBOX_IMPERSONATE_SA={host.OFFBOX_SA_DEFAULT}" in text
    g.calls.clear()
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    assert g.writes() == [], "a second run is a no-op"
    assert g.active == "claude-ro"
    section.teardown(s, apply_patch=lambda *a, **k: (True, ""), oc=lambda *a, **k: (0, ""))
    assert "openclaw-backup" not in g.configs and {"default", "claude-ro"} <= set(g.configs)
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG" not in env.read_text()


def test_the_verb_with_yes_says_it_is_running_not_that_it_would(monkeypatch, capsys):
    import argparse
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    args = argparse.Namespace(dry_run=False, remove=False, yes=True, name="openclaw-backup",
                              sa=host.OFFBOX_SA_DEFAULT, account="", project="")
    monkeypatch.setattr(state, "load", lambda: state.SetupState())
    monkeypatch.setattr(state, "save", lambda s: None)
    monkeypatch.setattr(host, "_write_offbox_knobs", lambda *a, **k: [])
    assert host.cmd_offbox_identity(args) == 0
    out = capsys.readouterr().out
    assert g.writes(), "the commands really ran"
    assert "would run" not in out and "running, in order" in out


# --- 2.1.1 pass 3: M1 derived project, L1 trailing newline, L5 orphaned configuration --------------------------------

@pytest.mark.parametrize("sa", [
    "kit-uploader@--.iam.gserviceaccount.com",
    "kit-uploader@--log-http.iam.gserviceaccount.com",
    "kit-uploader@-abcdef.iam.gserviceaccount.com",
    "kit-uploader@abc.iam.gserviceaccount.com",
])
def test_a_service_account_whose_project_part_is_not_a_project_id_is_refused(sa):
    g = FakeGcloud()
    plan = host.offbox_identity_plan(g, sa=sa, account="me@example.org")
    assert plan["steps"] == [] and plan["problems"], plan
    assert host.offbox_identity_apply(g, plan) is None
    assert g.writes() == [] and not any("--" in " ".join(c) and "core/project" in c for c in g.calls)


@pytest.mark.parametrize("field", ["name", "sa", "account", "project"])
def test_a_value_with_a_trailing_newline_is_refused(field):
    good = {"name": "openclaw-backup", "sa": host.OFFBOX_SA_DEFAULT, "account": "me@example.org",
            "project": "wildbit-iac"}
    assert host.offbox_identity_value_problems(**good) == []
    bad = dict(good, **{field: good[field] + "\n"})
    assert host.offbox_identity_value_problems(**bad), field


def test_the_host_env_refuses_a_trailing_newline_and_a_repeat_write_is_a_no_op(tmp_path):
    env = tmp_path / "kit-host.env"
    with pytest.raises(ValueError):
        host.write_host_env({"A": "x\n"}, env)
    with pytest.raises(ValueError):
        host.write_host_env({"A\n": "x"}, env)
    assert not env.exists()
    assert host.write_host_env({"A": "x"}, env) is True
    assert host.write_host_env({"A": "x"}, env) is False


class _FailingSetGcloud(FakeGcloud):
    def __init__(self, fail_on="core/project", **kw):
        super().__init__(**kw)
        self.fail_on = fail_on

    def __call__(self, argv, **kw):
        a = list(argv)
        if a[1:3] == ["config", "set"] and a[3] == self.fail_on:
            self.calls.append(a)
            return 1, "ERROR: boom"
        return super().__call__(argv, **kw)


def test_a_failed_apply_after_create_leaves_a_record_the_verb_can_remove(monkeypatch, tmp_path, capsys):
    import argparse
    g = _FailingSetGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    monkeypatch.setattr(state, "load", lambda: s)
    monkeypatch.setattr(state, "save", lambda _s: None)
    kw = dict(dry_run=False, remove=False, yes=True, name="openclaw-backup", sa=host.OFFBOX_SA_DEFAULT,
              account="me@example.org", project="")
    assert host.cmd_offbox_identity(argparse.Namespace(**kw)) == 1
    assert "openclaw-backup" in g.configs, "the create succeeded"
    assert s.openclaw.host_offbox_config_created.get("name") == "openclaw-backup"
    kw.update(remove=True)
    assert host.cmd_offbox_identity(argparse.Namespace(**kw)) == 0
    assert "openclaw-backup" not in g.configs
    assert s.openclaw.host_offbox_config_created == {}


def test_a_failed_cockpit_apply_after_create_is_recorded_and_teardown_removes_it(monkeypatch, tmp_path):
    g = _FailingSetGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    assert "openclaw-backup" in g.configs
    assert s.openclaw.host_offbox_config_created.get("name") == "openclaw-backup"
    section.teardown(s, apply_patch=lambda *a, **k: (True, ""), oc=lambda *a, **k: (0, ""))
    assert "openclaw-backup" not in g.configs and {"default", "claude-ro"} <= set(g.configs)


def test_a_rerun_after_a_failed_apply_adopts_the_configuration_and_finishes(monkeypatch, tmp_path):
    g = _FailingSetGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    s.openclaw.host_offbox_identity = True
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    g.fail_on = ""
    section._configure_offbox_identity(s.openclaw, dry_run=False)
    assert g.configs["openclaw-backup"]["core/project"] == "wildbit-iac"
    assert s.openclaw.host_offbox_config_created["project"] == "wildbit-iac"
    section.teardown(s, apply_patch=lambda *a, **k: (True, ""), oc=lambda *a, **k: (0, ""))
    assert "openclaw-backup" not in g.configs


# --- 2.1.1 pass 3: M3 env restore per key, L2 teardown path confinement, T1 hand .service only -----------------------

def test_teardown_survives_a_previous_env_value_it_cannot_write_back(sim, script):
    s = _state()
    _run_wizard(s)
    text = sim.env_file.read_text(encoding="utf-8")
    sim.env_file.write_text(text + "OPENCLAW_PASS3_A=kit\nOPENCLAW_PASS3_B=kit\nOPENCLAW_PASS3_C=kit\n"
                            "OPENCLAW_PASS3_D=kit\n", encoding="utf-8")
    s.openclaw.host_env_previous.update({"OPENCLAW_PASS3_A": "x y", "OPENCLAW_PASS3_B": "$HOME",
                                         "OPENCLAW_PASS3_C": "fine", "OPENCLAW_PASS3_D": None})
    removed = openclaw.teardown(s)
    assert removed == [], "an unfinished teardown reports nothing removed"
    assert not [p for p in sim.units.glob("openclaw-*")] if sim.units.exists() else True, \
        "the units were still undone"
    warned = " ".join(script.messages("warn"))
    assert "OPENCLAW_PASS3_A" in warned and "OPENCLAW_PASS3_B" in warned and "$HOME" not in warned
    env = sim.env_file.read_text(encoding="utf-8")
    assert "OPENCLAW_PASS3_A=kit" in env and "OPENCLAW_PASS3_B=kit" in env, "left as they are"
    assert "OPENCLAW_PASS3_C=fine" in env and "OPENCLAW_PASS3_D" not in env, "the other keys were restored"
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_PASS3_A", "OPENCLAW_PASS3_B"}, "kept for a retry"
    assert s.openclaw.host_env_created in (True, False)
    assert openclaw.teardown(s) == [], "a rerun does not crash and stays unfinished"
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_PASS3_A", "OPENCLAW_PASS3_B"}


def _victim(tmp_path, name="victim.txt", text="the operator's file\n"):
    v = tmp_path / "elsewhere" / name
    v.parent.mkdir(exist_ok=True)
    v.write_text(text, encoding="utf-8")
    return v


def test_teardown_ignores_unit_records_that_point_outside_the_unit_dir(sim, script, tmp_path):
    victim = _victim(tmp_path)
    s = _state()
    _run_wizard(s)
    rel = "../" * 8 + str(victim).lstrip("/")
    s.openclaw.host_units_previous.update({rel: "pwned", str(victim): "pwned", "not-a-kit-unit.service": "x"})
    openclaw.teardown(s)
    assert victim.read_text(encoding="utf-8") == "the operator's file\n"
    assert not (sim.units / "not-a-kit-unit.service").exists()
    assert any("not a unit the kit manages" in m or "ignored" in m for m in script.messages("warn"))


def test_teardown_does_not_write_through_a_symlinked_unit(sim, script, tmp_path):
    victim = _victim(tmp_path)
    s = _state()
    _run_wizard(s)
    link = sim.units / "openclaw-watchdog.timer"
    link.unlink()
    link.symlink_to(victim)
    s.openclaw.host_units_previous["openclaw-watchdog.timer"] = "pwned"
    openclaw.teardown(s)
    assert victim.read_text(encoding="utf-8") == "the operator's file\n"


def test_teardown_does_not_move_a_file_over_a_path_outside_the_unit_dir(sim, script, tmp_path):
    victim = _victim(tmp_path)
    moved = _victim(tmp_path, "moved.txt", "payload\n")
    s = _state()
    _run_wizard(s)
    s.openclaw.host_hand_moved[str(victim)] = str(moved)
    s.openclaw.host_hand_kit_sha[str(victim)] = hashlib.sha256(victim.read_bytes()).hexdigest()
    openclaw.teardown(s)
    assert victim.read_text(encoding="utf-8") == "the operator's file\n"
    assert moved.read_text(encoding="utf-8") == "payload\n"
    assert any(str(victim) in m for m in script.messages("warn"))


def test_teardown_does_not_disable_a_timer_the_kit_never_manages(sim, script):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_timers_enabled.extend(["--now", "ssh.timer"])
    sim.systemd.calls.clear()
    openclaw.teardown(s)
    disables = [c for c in sim.systemd.calls if c[:3] == ["systemctl", "--user", "disable"]]
    assert not any(c[-1] in ("--now", "ssh.timer") for c in disables)


def test_teardown_removes_the_kit_timer_written_next_to_a_hand_service_only(sim, script):
    """T1: only the hand .service exists; the kit writes its own timer, so teardown must remove it again."""
    sim.units.mkdir(parents=True, exist_ok=True)
    service = sim.units / "openclaw-health-restart.service"
    service.write_bytes(b"# hand copy of the service\n[Unit]\nDescription=mine\n")
    before = {p.name: p.read_bytes() for p in sim.units.iterdir()}
    s = _state()
    _run_wizard(s)
    assert (sim.units / "openclaw-health-restart.timer").exists(), "setup wrote the kit's timer"
    openclaw.teardown(s)
    after = {p.name: p.read_bytes() for p in sim.units.iterdir()}
    assert after == before, "the unit directory is what it was before setup"

