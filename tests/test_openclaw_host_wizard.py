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
    "Replace plugins.entries.workboard.enabled": False,   # ADR-0014 D5: disabling the workboard is its own confirm
    "Check that the utility model": False,          # ADR-0014 D5: the probe is opt-in and bills a tiny call
    "Turn the main agent's heartbeat off": False,   # ADR-0014 D5: the default keeps the 2h isolated light heartbeat
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
        self.terminal = True               # whether stdin is a real terminal (a piped `y` is not a confirmation)

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
        monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: self.terminal)
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
    # +1: ADR-0014 D5, the heartbeat question, which defaults to No (the 2h isolated light heartbeat stays).
    assert default_of("Turn the main agent's heartbeat off") is False
    assert len(defaults) == 12 + 3 + 1 + 2 + 1, sorted(defaults)
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


def test_telegram_disabled_skips_narration_group_ids_and_operator_id(sim, script):
    s = _state()
    s.openclaw.channels_seen = ["telegram:default", "slack:default"]
    s.openclaw.channels_wanted = ["slack:default"]
    openclaw.prompt(s)
    o = s.openclaw
    assert not any("Telegram topic" in q or "group chat id" in q or "Your Telegram user id" in q for q in script.asked)
    assert o.host_narration == ""
    assert o.host_operator_id == ""
    assert o.host_group_ids == {}


def test_routed_agents_excludes_kit_lane_internal_agents():
    doc = {"agents": {"entries": {
        "main": {}, "claude": {}, "app": {}, "devops": {},
        "kit-lane-cli-sonnet55": {}, "kit-lane-agy-gemini38flashhigh": {}
    }}}
    routed = section._routed_agents(doc)
    assert "kit-lane-cli-sonnet55" not in routed
    assert "kit-lane-agy-gemini38flashhigh" not in routed
    assert set(routed) == {"app", "devops"}



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
    assert sections == [(wizard._screen_of(10), wizard.SCREENS, "Verification")] and calls == [["claude", "openclaw"]]
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
                 "_step_models", "_step_preset", "_step_summary"):
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


def test_teardown_puts_back_a_hand_unit_that_was_a_symlink(sim, script, tmp_path):
    target = tmp_path / "dotfiles-unit.service"
    target.write_text("# stowed hand unit\n[Unit]\nDescription=mine\n", encoding="utf-8")
    _put_hand_copy(sim)
    sim.units.mkdir(parents=True, exist_ok=True)
    link = sim.units / "openclaw-health-restart.service"
    link.symlink_to(target)
    s = _state()
    _run_wizard(s)
    assert str(link) in s.openclaw.host_hand_moved
    openclaw.teardown(s)
    assert link.is_symlink() and link.resolve() == target.resolve(), "the operator's link is back"
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
    sim.env_file.write_text("OPENCLAW_OFFBOX_GCLOUD_CONFIG=kit\nOPENCLAW_OFFBOX_IMPERSONATE_SA=kit\n"
                            "OPENCLAW_POD_CIDR=kit\nOPENCLAW_NARRATION=kit\n", encoding="utf-8")
    s.openclaw.host_env_previous.update({"OPENCLAW_OFFBOX_GCLOUD_CONFIG": "x y", "OPENCLAW_OFFBOX_IMPERSONATE_SA": "$HOME",
                                         "OPENCLAW_POD_CIDR": "fine", "OPENCLAW_NARRATION": None})
    removed = openclaw.teardown(s)
    assert removed == [], "an unfinished teardown reports nothing removed"
    assert not [p for p in sim.units.glob("openclaw-*")] if sim.units.exists() else True, \
        "the units were still undone"
    warned = " ".join(script.messages("warn"))
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in warned and "OPENCLAW_OFFBOX_IMPERSONATE_SA" in warned and "$HOME" not in warned
    env = sim.env_file.read_text(encoding="utf-8")
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG=kit" in env and "OPENCLAW_OFFBOX_IMPERSONATE_SA=kit" in env, "left as they are"
    assert "OPENCLAW_POD_CIDR=fine" in env and "OPENCLAW_NARRATION" not in env, "the other keys were restored"
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_OFFBOX_GCLOUD_CONFIG", "OPENCLAW_OFFBOX_IMPERSONATE_SA"}, "kept for a retry"
    assert s.openclaw.host_env_created in (True, False)
    assert openclaw.teardown(s) == [], "a rerun does not crash and stays unfinished"
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_OFFBOX_GCLOUD_CONFIG", "OPENCLAW_OFFBOX_IMPERSONATE_SA"}


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



# --- kit 2.1.1 pass 4, S5/S6: one off-box configuration at a time, robust teardown, single-violation hand moves ----------

def test_the_plan_refuses_a_second_name_while_the_first_is_recorded_and_says_how_to_remove_it():
    g = FakeGcloud()
    first = host.offbox_identity_apply(g, host.offbox_identity_plan(g, name="first-cfg"))
    plan = host.offbox_identity_plan(g, name="second-cfg", created=first)
    assert plan["steps"] == [] and len(plan["problems"]) == 1
    assert "first-cfg" in plan["problems"][0] and "--remove" in plan["problems"][0]
    assert host.offbox_identity_apply(g, plan) is None
    assert "second-cfg" not in g.configs, "nothing was created"
    again = host.offbox_identity_plan(g, name="first-cfg", created=first)
    assert again["problems"] == [] and again["steps"] == [], "the recorded name itself is still idempotent"


def test_the_verb_run_with_another_name_is_refused_and_teardown_still_removes_the_first(monkeypatch, tmp_path, capsys):
    import argparse
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    monkeypatch.setattr(state, "load", lambda: s)
    monkeypatch.setattr(state, "save", lambda _s: None)

    def run(**kw):
        base = dict(dry_run=False, remove=False, yes=True, name="openclaw-backup", sa=host.OFFBOX_SA_DEFAULT,
                    account="me@example.org", project="")
        base.update(kw)
        return host.cmd_offbox_identity(argparse.Namespace(**base))

    assert run(name="first-cfg") == 0
    assert s.openclaw.host_offbox_config_created["name"] == "first-cfg"
    capsys.readouterr()
    assert run(name="second-cfg") == 1
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if ln.startswith("refusing:")] and "--remove" in out
    assert "second-cfg" not in g.configs
    assert s.openclaw.host_offbox_config_created["name"] == "first-cfg", "the first record is not replaced"
    assert run(remove=True, name=None) == 0           # `--remove` without `--name` (P11)
    assert "first-cfg" not in g.configs and s.openclaw.host_offbox_config_created == {}


def test_remove_refuses_a_recorded_name_the_kit_would_not_create(monkeypatch):
    for bad in ("Bad_Name", "-flag", "has space", "UPPER", "1abc", ""):
        g = FakeGcloud(configs={"default": {}, "claude-ro": {}, bad: {}} if bad else None)
        lines: list[str] = []
        assert host.offbox_identity_remove(g, {"name": bad}, out=lines.append) is False, bad
        assert not [c for c in g.calls if c[1:4] == ["config", "configurations", "delete"]], bad


def test_remove_still_deletes_a_well_formed_recorded_name():
    g = FakeGcloud(configs={"default": {}, "up-1": {}})
    assert host.offbox_identity_remove(g, {"name": "up-1"}) is True
    assert "up-1" not in g.configs


def test_teardown_survives_a_non_string_previous_env_value(sim, script):
    s = _state()
    _run_wizard(s)
    sim.env_file.write_text(sim.env_file.read_text(encoding="utf-8") + "OPENCLAW_OFFBOX_GCLOUD_CONFIG=kit\nOPENCLAW_POD_CIDR=kit\n",
                            encoding="utf-8")
    s.openclaw.host_env_previous.update({"OPENCLAW_OFFBOX_GCLOUD_CONFIG": 5, "OPENCLAW_POD_CIDR": "fine"})
    assert openclaw.teardown(s) == [], "unfinished, not a crash"
    warned = " ".join(script.messages("warn"))
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in warned
    env = sim.env_file.read_text(encoding="utf-8")
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG=kit" in env and "OPENCLAW_POD_CIDR=fine" in env, "the other key was restored"
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_OFFBOX_GCLOUD_CONFIG"}


@pytest.mark.parametrize("key", ["LD_PRELOAD", "PATH", "BASH_ENV", "HOME", "OPENCLAW-X", "openclaw_lower"])
def test_teardown_never_writes_back_a_key_outside_the_kits_own_namespace(sim, script, key):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous[key] = "/home/u/evil.so"
    openclaw.teardown(s)
    after = sim.env_file.read_text(encoding="utf-8") if sim.env_file.exists() else ""
    assert key not in after and f"{key}=" not in after
    assert key in " ".join(script.messages("warn"))
    assert key not in s.openclaw.host_env_previous, "there is nothing the kit owns to retry"


def _hand_move_fixture(sim):
    """A valid recorded hand move: the script under ~/.local/bin that the kit moved to the backup directory."""
    backups = sim.home / ".openclaw" / "backup" / "hand-units"
    ts = backups / "20260101T000000Z"
    ts.mkdir(parents=True)
    return backups, ts


def _moved_state(original: pathlib.Path, moved: pathlib.Path):
    s = _state()
    s.openclaw.host_hand_moved[str(original)] = str(moved)
    s.openclaw.host_hand_kit_sha[str(original)] = "0" * 64
    return s


def test_a_valid_hand_move_is_put_back_control(sim, script):
    _backups, ts = _hand_move_fixture(sim)
    dst = sim.home / ".local" / "bin" / host.HAND_SCRIPT_NAME
    dst.parent.mkdir(parents=True)
    src = ts / host.HAND_SCRIPT_NAME
    src.write_text("hand\n", encoding="utf-8")
    s = _moved_state(dst, src)
    section._remove_units(s.openclaw)
    assert dst.read_text(encoding="utf-8") == "hand\n" and not src.exists()
    assert s.openclaw.host_hand_moved == {}


def _violation_cases(sim, tmp_path):
    """(id, original, moved): each breaks exactly ONE condition of the confinement rule."""
    backups, ts = _hand_move_fixture(sim)
    bin_dir = sim.home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sim.units.mkdir(parents=True, exist_ok=True)
    script_name = host.HAND_SCRIPT_NAME
    cases = []

    def mk(path: pathlib.Path, text="payload\n"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # src.parent.parent is not the backup directory
    cases.append(("src-parent-parent", bin_dir / script_name,
                  mk(sim.home / ".openclaw" / "backup" / "other" / script_name)))
    # src.name != dst.name
    cases.append(("src-name", bin_dir / script_name, mk(ts / "something-else.sh")))
    # `..` in the source path (its parent.parent is still the backup directory)
    cases.append(("dotdot-in-src", bin_dir / script_name, mk(backups / ".." / script_name)))
    # the destination is neither the hand script nor a health-restart unit: a file in ~/.local/bin
    cases.append(("script-path", bin_dir / "some-other-tool.sh", mk(ts / "some-other-tool.sh")))
    # a unit the kit manages elsewhere, inside the unit dir, is not a hand health-restart unit
    cases.append(("kit-unit-name", sim.units / "openclaw-gateway.service", mk(ts / "openclaw-gateway.service")))
    # a health-restart unit name outside the user unit dir
    cases.append(("path-parent-is-unit-dir", tmp_path / "elsewhere" / "openclaw-health-restart.service",
                  mk(ts / "openclaw-health-restart.service")))
    return cases


_VIOLATION_IDS = ["src-parent-parent", "src-name", "dotdot-in-src", "script-path", "kit-unit-name",
                  "path-parent-is-unit-dir"]


@pytest.mark.parametrize("case_id", _VIOLATION_IDS)
def test_a_hand_move_that_breaks_one_confinement_condition_is_ignored(sim, script, tmp_path, case_id):
    cases = {c[0]: c for c in _violation_cases(sim, tmp_path)}
    _id, original, moved = cases[case_id]
    before_moved = moved.read_bytes()
    (original.parent).mkdir(parents=True, exist_ok=True)
    s = _moved_state(original, moved)
    section._remove_units(s.openclaw)
    assert not original.exists(), f"{case_id}: nothing was moved onto {original}"
    assert moved.read_bytes() == before_moved, f"{case_id}: the source was left where it was"
    assert s.openclaw.host_hand_moved == {} and s.openclaw.host_hand_kit_sha == {}, "the entry is dropped"
    assert any(str(original) in m for m in script.messages("warn")), f"{case_id}: warned by name"


def test_a_hand_move_onto_a_symlinked_destination_is_ignored(sim, script, tmp_path):
    _backups, ts = _hand_move_fixture(sim)
    victim = _victim(tmp_path)
    dst = sim.home / ".local" / "bin" / host.HAND_SCRIPT_NAME
    dst.parent.mkdir(parents=True)
    dst.symlink_to(victim)
    src = ts / host.HAND_SCRIPT_NAME
    src.write_text("payload\n", encoding="utf-8")
    s = _moved_state(dst, src)
    section._remove_units(s.openclaw)
    assert victim.read_text(encoding="utf-8") == "the operator's file\n" and src.exists()
    assert s.openclaw.host_hand_moved == {}


def test_the_verb_rerun_with_the_recorded_name_replaces_a_partial_record_with_the_full_one(monkeypatch, tmp_path):
    """L5 verb side: a configuration created and then left half-configured is adopted by a rerun."""
    import argparse
    g = FakeGcloud(configs={"default": {}, "claude-ro": {}, "up-1": {}})
    monkeypatch.setattr(host, "default_runner", g)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    s = state.SetupState()
    s.openclaw.host_offbox_config_created = {"name": "up-1", "account": "", "impersonate": "", "project": ""}
    monkeypatch.setattr(state, "load", lambda: s)
    monkeypatch.setattr(state, "save", lambda _s: None)
    ns = argparse.Namespace(dry_run=False, remove=False, yes=True, name="up-1", sa=host.OFFBOX_SA_DEFAULT,
                            account="me@example.org", project="")
    assert host.cmd_offbox_identity(ns) == 0
    assert s.openclaw.host_offbox_config_created == {
        "name": "up-1", "account": "me@example.org", "impersonate": host.OFFBOX_SA_DEFAULT, "project": "wildbit-iac"}
    assert g.configs["up-1"]["account"] == "me@example.org"


# --- kit 2.1.1 pass 5, P4: the teardown env allowlist is the fixed key set the kit writes -------------------------

@pytest.mark.parametrize("key", ["OPENCLAW_OFFBOX_LIST_CMD", "OPENCLAW_EXTRA_PATH", "OPENCLAW_BACKUP_HOOK",
                                 "OPENCLAW_PASS5_UNMANAGED"])
def test_teardown_never_writes_back_an_unmanaged_openclaw_key(sim, script, key):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous[key] = "/usr/local/bin/some-listing"
    openclaw.teardown(s)
    after = sim.env_file.read_text(encoding="utf-8") if sim.env_file.exists() else ""
    assert f"{key}=" not in after
    assert key in " ".join(script.messages("warn"))
    assert key not in s.openclaw.host_env_previous


def test_every_key_the_wizard_writes_to_the_host_env_is_in_the_teardown_allowlist(sim, script):
    s = _state()
    _run_wizard(s)
    written = set(host.read_host_env())
    assert written and written <= host.KIT_ENV_KEYS, sorted(written - host.KIT_ENV_KEYS)
    assert {"OPENCLAW_OFFBOX_GCLOUD_CONFIG", "OPENCLAW_OFFBOX_IMPERSONATE_SA"} <= host.KIT_ENV_KEYS


# --- kit 2.1.1 pass 5, P5: the state is saved atomically and the off-box record is updated under a lock ----------

def _xdg_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return state.state_path()


def test_state_save_goes_through_a_temp_file_in_the_same_directory_and_a_replace(monkeypatch, tmp_path):
    import os
    path = _xdg_state(monkeypatch, tmp_path)
    first = state.SetupState()
    first.openclaw.host_domain = "first.example.org"
    state.save(first)
    before = path.read_text(encoding="utf-8")
    assert before and oct(path.stat().st_mode & 0o777) == "0o600"
    path.chmod(0o644)                                          # an older run left it wider
    seen: list[tuple] = []
    real_replace = os.replace

    def replace(src, dst, *a, **k):
        # at this instant a concurrent reader still gets the complete previous file, never an empty one
        seen.append((pathlib.Path(src).parent == path.parent, pathlib.Path(dst) == path,
                     path.read_text(encoding="utf-8") == before, oct(pathlib.Path(src).stat().st_mode & 0o777)))
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(state.os, "replace", replace)
    second = state.SetupState()
    second.openclaw.host_domain = "second.example.org"
    state.save(second)
    assert seen == [(True, True, True, "0o600")]
    assert "second.example.org" in path.read_text(encoding="utf-8")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == [], "no temp file is left behind"


def test_a_failed_state_save_leaves_the_previous_file_and_no_temp_file(monkeypatch, tmp_path):
    path = _xdg_state(monkeypatch, tmp_path)
    state.save(state.SetupState())
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        state.save(state.SetupState())
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_two_interleaved_offbox_identity_runs_end_with_one_recorded_configuration(monkeypatch, tmp_path, capsys):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    g = FakeGcloud()
    nested: list[int] = []

    def base(**kw):
        d = dict(dry_run=False, remove=False, yes=True, name="first-cfg", sa=host.OFFBOX_SA_DEFAULT,
                 account="me@example.org", project="")
        d.update(kw)
        return argparse.Namespace(**d)

    def interleaving(argv, **kw):
        # the second run starts while the first is between `create` and its state save
        if list(argv[1:4]) == ["config", "configurations", "create"] and not nested:
            nested.append(host.cmd_offbox_identity(base(name="second-cfg")))
        return g(argv, **kw)
    monkeypatch.setattr(host, "default_runner", interleaving)
    assert host.cmd_offbox_identity(base()) == 0
    assert nested == [1], "the interleaved run is refused"
    assert "second-cfg" not in g.configs, "no orphan configuration"
    recorded = state.load().openclaw.host_offbox_config_created
    assert recorded.get("name") == "first-cfg" and "first-cfg" in g.configs
    assert "another offbox-identity run" in capsys.readouterr().out


def test_the_lock_is_released_after_a_run(monkeypatch, tmp_path):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    monkeypatch.setattr(host, "default_runner", FakeGcloud())
    ns = argparse.Namespace(dry_run=False, remove=False, yes=True, name="first-cfg", sa=host.OFFBOX_SA_DEFAULT,
                            account="me@example.org", project="")
    assert host.cmd_offbox_identity(ns) == 0
    assert host.cmd_offbox_identity(ns) == 0, "a second run after the first one is not refused"


def test_a_custom_named_record_does_not_make_the_default_plan_warn_every_run(monkeypatch):
    g = FakeGcloud(configs={"default": {}, "claude-ro": {}, "up-1": {
        "account": "me@example.org", "auth/impersonate_service_account": host.OFFBOX_SA_DEFAULT,
        "core/project": "wildbit-iac"}})
    warned: list[str] = []
    monkeypatch.setattr(section, "_runner", lambda: g)
    monkeypatch.setattr(ui, "warn", lambda m, *_a, **_k: warned.append(m))
    monkeypatch.setattr(ui, "info", lambda *_a, **_k: None)
    s = _state()
    o = s.openclaw
    o.host_offbox_identity = True
    o.host_offbox_config_created = {"name": "up-1", "account": "me@example.org",
                                    "impersonate": host.OFFBOX_SA_DEFAULT, "project": "wildbit-iac"}
    section._configure_offbox_identity(o, dry_run=True)
    assert warned == []
    assert g.writes() == []


@pytest.mark.parametrize("junk", [["up-1"], "up-1", 7, True])
def test_a_hand_edited_non_dict_offbox_record_does_not_crash(monkeypatch, tmp_path, capsys, junk):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    monkeypatch.setattr(section, "_runner", lambda: g)
    s = state.SetupState()
    s.openclaw.host_offbox_config_created = junk
    state.save(s)
    ns = argparse.Namespace(dry_run=False, remove=True, yes=True, name=None, sa=host.OFFBOX_SA_DEFAULT,
                            account="", project="")
    assert host.cmd_offbox_identity(ns) == 0                       # nothing recorded: nothing to remove
    assert "nothing to remove" in capsys.readouterr().out
    plan = host.offbox_identity_plan(g, created=junk)               # `.get` on a non-dict
    assert plan["problems"] == [] and plan["steps"]
    s = _state()
    s.openclaw.host = True
    s.openclaw.host_offbox_config_created = junk
    assert section.teardown(s, apply_patch=lambda *_a, **_k: (True, ""), oc=lambda *_a, **_k: (0, "")) in (True, False)


# --- kit 2.1.1 pass 5, P11: --remove names the recorded configuration; the non-string wording -------------------

def test_remove_with_a_different_name_is_refused_and_removes_nothing(monkeypatch, tmp_path, capsys):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)

    def run(**kw):
        d = dict(dry_run=False, remove=False, yes=True, name=None, sa=host.OFFBOX_SA_DEFAULT,
                 account="me@example.org", project="")
        d.update(kw)
        return host.cmd_offbox_identity(argparse.Namespace(**d))

    assert run(name="first-cfg") == 0
    capsys.readouterr()
    assert run(name="other-cfg", remove=True) == 1
    out = capsys.readouterr().out
    assert "the recorded configuration is first-cfg" in out and "without --name" in out
    assert "first-cfg" in g.configs, "nothing was deleted"
    assert state.load().openclaw.host_offbox_config_created.get("name") == "first-cfg"
    assert run(name="first-cfg", remove=True) == 0                 # the recorded name itself
    assert "first-cfg" not in g.configs


def test_remove_without_a_name_removes_the_recorded_configuration(monkeypatch, tmp_path):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    base = dict(dry_run=False, remove=False, yes=True, sa=host.OFFBOX_SA_DEFAULT, account="me@example.org",
                project="")
    assert host.cmd_offbox_identity(argparse.Namespace(name="first-cfg", **base)) == 0
    assert host.cmd_offbox_identity(argparse.Namespace(name=None, **{**base, "remove": True})) == 0
    assert "first-cfg" not in g.configs


@pytest.mark.parametrize("value", [["a"], 5, {"k": "v"}, True])
def test_a_non_string_previous_env_value_is_reported_as_not_a_string(sim, script, value):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous["OPENCLAW_OFFBOX_GCLOUD_CONFIG"] = value
    assert openclaw.teardown(s) == []
    warned = " ".join(script.messages("warn"))
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in warned and "not a string" in warned
    assert "space" not in warned.split("OPENCLAW_OFFBOX_GCLOUD_CONFIG", 1)[1]


# --- kit 2.1.1 pass 5, P9: pin the mutants that survived pass 4 ---------------------------------------------------

def test_a_dangling_symlinked_hand_destination_is_never_written_through(sim, script, tmp_path):
    """S6l, stand-alone: with the link dangling `dst.exists()` is False, so only the symlink check protects it."""
    _backups, ts = _hand_move_fixture(sim)
    target = tmp_path / "elsewhere" / "created-by-teardown.txt"
    target.parent.mkdir()
    dst = sim.home / ".local" / "bin" / host.HAND_SCRIPT_NAME
    dst.parent.mkdir(parents=True)
    dst.symlink_to(target)
    assert dst.is_symlink() and not dst.exists()
    src = ts / host.HAND_SCRIPT_NAME
    src.write_text("payload\n", encoding="utf-8")
    s = _moved_state(dst, src)
    section._remove_units(s.openclaw)
    assert not target.exists(), "nothing was written through the link"
    assert src.read_text(encoding="utf-8") == "payload\n"
    assert s.openclaw.host_hand_moved == {}


@pytest.mark.parametrize("protected", ["default", "claude-ro"])
def test_remove_never_deletes_a_protected_gcloud_configuration(protected):
    """S6e: the two configurations the kit never touches are refused even when a hand edit records them."""
    g = FakeGcloud(configs={"default": {}, "claude-ro": {}}, active="somewhere-else")   # not the ACTIVE refusal
    lines: list[str] = []
    assert host.offbox_identity_remove(g, {"name": protected}, out=lines.append) is False
    assert protected in g.configs
    assert not [c for c in g.calls if c[1:4] == ["config", "configurations", "delete"]]
    assert any(protected in ln and "left as it is" in ln for ln in lines)


def test_a_foreign_env_key_is_dropped_from_the_record_while_a_stuck_key_is_kept(sim, script):
    """S6c: a key the kit does not manage is not retried; only a key the kit owns but cannot write back is."""
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous.update({"LD_PRELOAD": "/home/u/evil.so", "OPENCLAW_OFFBOX_LIST_CMD": "x",
                                         "OPENCLAW_OFFBOX_GCLOUD_CONFIG": "has space"})
    assert openclaw.teardown(s) == []
    assert set(s.openclaw.host_env_previous) == {"OPENCLAW_OFFBOX_GCLOUD_CONFIG"}


def test_a_foreign_env_key_alone_does_not_hold_the_record_open(sim, script):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous["LD_PRELOAD"] = "/home/u/evil.so"
    openclaw.teardown(s)
    assert s.openclaw.host_env_previous == {} and s.openclaw.host_env_created is False
    after = sim.env_file.read_text(encoding="utf-8") if sim.env_file.exists() else ""
    assert "LD_PRELOAD" not in after


# --- pass 6, L2: a symlinked setup-state.yaml is replaced at its target, not turned into a plain file -------------

def test_state_save_writes_through_a_symlinked_state_file(monkeypatch, tmp_path):
    import os
    path = _xdg_state(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "dotfiles" / "setup-state.yaml"
    target.parent.mkdir()
    target.write_text("{}\n", encoding="utf-8")
    path.symlink_to(target)
    s = state.SetupState()
    s.openclaw.host_domain = "linked.example.org"
    state.save(s)
    assert path.is_symlink(), "the link is kept"
    assert "linked.example.org" in target.read_text(encoding="utf-8")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


# --- pass 6: an unusable lock file is one line, not a traceback ------------------------------------------------

@pytest.mark.parametrize("make", ["dir", "dangling"])
def test_an_unusable_state_lock_file_is_refused_in_one_line(monkeypatch, tmp_path, capsys, make):
    import argparse
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    lock = state.state_path().with_name(state.state_path().name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    if make == "dir":
        lock.mkdir()
    else:
        lock.symlink_to(tmp_path / "nowhere" / "x")
    kw = dict(dry_run=False, remove=False, yes=True, name="openclaw-backup", sa=host.OFFBOX_SA_DEFAULT,
              account="me@example.org", project="")
    assert host.cmd_offbox_identity(argparse.Namespace(**kw)) == 1
    out = capsys.readouterr().out
    assert "lock file cannot be used" in out and "Traceback" not in out


# =====================================================================================================================
# Kit 2.2, group C: state locks, stale temp files, unique temp names, atomic managed blocks
# =====================================================================================================================

def _hold_state_lock(monkeypatch, tmp_path):
    """Hold the setup-state lock the way another run does; returns the open descriptor."""
    import fcntl
    import os
    path = _xdg_state(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_second_setup_run_is_refused_with_one_line_while_another_holds_the_state_lock(monkeypatch, tmp_path,
                                                                                         capsys):
    import argparse
    import os
    fd = _hold_state_lock(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(ui, "require_deps", lambda: None)
        monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)    # pre-fix: "No terminal" and rc 1
        rc = wizard.run(argparse.Namespace(dry_run=False, non_interactive=False, profile=""))
    finally:
        os.close(fd)
    out = capsys.readouterr()
    text = out.out + out.err
    assert rc == 1
    assert "setup-state lock" in text and len([ln for ln in text.splitlines() if "lock" in ln]) == 1
    assert not state.state_path().exists(), "nothing was written"


def test_a_setup_dry_run_does_not_take_or_create_the_state_lock(monkeypatch, tmp_path):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    wizard.run(argparse.Namespace(dry_run=True, non_interactive=False, profile=""))
    assert not state.state_path().with_name(state.state_path().name + ".lock").exists()


@pytest.mark.parametrize("discard", [False, True])
def test_apply_pending_is_refused_with_one_line_while_the_state_is_locked(monkeypatch, tmp_path, capsys, discard):
    import argparse
    import os
    fd = _hold_state_lock(monkeypatch, tmp_path)
    try:
        rc = host.cmd_apply_pending(argparse.Namespace(discard_watch_restores=discard, yes=True))
    finally:
        os.close(fd)
    out = capsys.readouterr().out
    assert rc == 1
    assert "setup-state lock" in out and len(out.strip().splitlines()) == 1


def test_apply_pending_releases_the_lock_when_it_finishes(monkeypatch, tmp_path):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    ns = argparse.Namespace(discard_watch_restores=False, yes=True)
    host.cmd_apply_pending(ns)
    fd = _hold_state_lock(monkeypatch, tmp_path)         # would raise BlockingIOError if still held
    import os
    os.close(fd)


@pytest.mark.parametrize("remove", [False, True])
def test_an_offbox_identity_dry_run_creates_no_lock_file(monkeypatch, tmp_path, remove):
    import argparse
    _xdg_state(monkeypatch, tmp_path)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    monkeypatch.setattr(host, "default_runner", FakeGcloud())
    s = state.SetupState()
    if remove:
        s.openclaw.host_offbox_config_created = {"name": "up-1", "account": "me@example.org",
                                                 "impersonate": host.OFFBOX_SA_DEFAULT, "project": ""}
    state.save(s)
    lock = state.state_path().with_name(state.state_path().name + ".lock")
    lock.unlink(missing_ok=True)
    ns = argparse.Namespace(dry_run=True, remove=remove, yes=False, name=None, sa=host.OFFBOX_SA_DEFAULT,
                            account="me@example.org", project="")
    assert host.cmd_offbox_identity(ns) == 0
    assert not lock.exists()


def test_a_stale_state_temp_file_is_removed_at_the_next_save(monkeypatch, tmp_path):
    import os
    import time
    path = _xdg_state(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.save(state.SetupState())
    old = path.parent / f".{path.name}.abc123.tmp"
    old.write_text("half a save")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    fresh = path.parent / f".{path.name}.fresh1.tmp"
    fresh.write_text("a save in flight")
    other = path.parent / ".setup-state.yaml.lock"
    other.write_text("")
    os.utime(other, (two_hours_ago, two_hours_ago))
    unrelated = path.parent / ".other-file.tmp"
    unrelated.write_text("x")
    os.utime(unrelated, (two_hours_ago, two_hours_ago))
    link = path.parent / f".{path.name}.link12.tmp"
    link.symlink_to(path)
    os.utime(link, (two_hours_ago, two_hours_ago), follow_symlinks=False)
    before = path.read_bytes()
    state.save(state.SetupState())
    assert not old.exists(), "a temp file older than an hour is a SIGKILLed save"
    assert fresh.exists(), "a temp file younger than an hour may be a save in flight"
    assert other.exists() and unrelated.exists(), "only .setup-state.yaml.*.tmp files are candidates"
    assert link.is_symlink() and path.is_file(), "a symlink is never followed or removed, and nor is the state"
    assert path.read_bytes() != b"" and before


def test_write_host_env_never_writes_through_a_fixed_temp_name(monkeypatch, tmp_path):
    import os
    env_path = tmp_path / "kit-host.env"
    env_path.write_text("A=1\n", encoding="utf-8")
    env_path.chmod(0o640)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (tmp_path / "kit-host.env.tmp").symlink_to(victim)             # what a fixed name would follow
    seen: list[str] = []
    real = os.replace
    monkeypatch.setattr(host.os, "replace", lambda s, d, *a, **k: (seen.append(str(s)), real(s, d, *a, **k))[1])
    assert host.write_host_env({"OPENCLAW_RESTART_TZ": "UTC"}, env_path) is True
    assert victim.read_text(encoding="utf-8") == "keep"
    assert seen and pathlib.Path(seen[0]).parent == tmp_path and pathlib.Path(seen[0]).name != "kit-host.env.tmp"
    assert oct(env_path.stat().st_mode & 0o777) == "0o640", "the file mode is kept"
    assert "OPENCLAW_RESTART_TZ=UTC" in env_path.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kit-host.env", "kit-host.env.tmp", "victim.txt"]


def _freeze_backup_stamp(monkeypatch):
    from ai_resources.setup.cockpits import _shared

    class Frozen:
        @staticmethod
        def now(tz=None):
            import datetime as _dt
            return _dt.datetime(2026, 10, 12, 1, 2, 3, tzinfo=tz)
    monkeypatch.setattr(_shared, "datetime", Frozen)
    return _shared


def test_a_managed_block_write_is_atomic_and_keeps_the_mode(monkeypatch, tmp_path):
    import os
    from ai_resources.setup.cockpits import _shared
    target = tmp_path / "AGENTS.md"
    target.write_text("# mine\n", encoding="utf-8")
    target.chmod(0o640)
    seen: list[tuple[str, str]] = []
    real = os.replace
    monkeypatch.setattr(_shared.os, "replace", lambda s, d, *a, **k: (seen.append((str(s), str(d))), real(s, d))[1])
    assert _shared.write_managed_block(target, "kit text") is True
    assert seen and seen[0][1] == str(target) and pathlib.Path(seen[0][0]).parent == tmp_path
    assert oct(target.stat().st_mode & 0o777) == "0o640"
    assert "kit text" in target.read_text(encoding="utf-8")
    seen.clear()
    assert _shared.remove_managed_block(target) is True
    assert seen and seen[0][1] == str(target) and oct(target.stat().st_mode & 0o777) == "0o640"
    assert target.read_text(encoding="utf-8") == "# mine\n"


def test_a_managed_block_through_a_symlink_is_written_at_the_target(tmp_path):
    from ai_resources.setup.cockpits import _shared
    real = tmp_path / "dotfiles" / "AGENTS.md"
    real.parent.mkdir()
    real.write_text("# mine\n", encoding="utf-8")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)
    assert _shared.write_managed_block(link, "kit text") is True
    assert link.is_symlink() and "kit text" in real.read_text(encoding="utf-8"), "the link survives, the target holds it"
    assert _shared.remove_managed_block(link) is True
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "# mine\n"


def test_a_managed_block_through_a_dangling_symlink_is_refused_with_one_line(monkeypatch, tmp_path):
    from ai_resources.setup.cockpits import _shared
    link = tmp_path / "AGENTS.md"
    link.symlink_to(tmp_path / "no-such-dir" / "AGENTS.md")
    warned: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda m, *_a, **_k: warned.append(m))
    assert _shared.write_managed_block(link, "kit text") is False
    assert len(warned) == 1 and "AGENTS.md" in warned[0]
    assert not (tmp_path / "no-such-dir").exists()


def test_the_backup_of_an_old_kit_section_is_created_exclusively(monkeypatch, tmp_path):
    _shared = _freeze_backup_stamp(monkeypatch)
    target = tmp_path / "AGENTS.md"
    target.write_text("old kit text without markers\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    squatter = tmp_path / "AGENTS.md.ai-resources-backup-20261012T010203Z"
    squatter.symlink_to(victim)                                    # planted at the predictable name
    monkeypatch.setattr(ui, "warn", lambda *_a, **_k: None)
    monkeypatch.setattr(_shared, "_render_managed", lambda raw, body: ("new\n", True))
    assert _shared.write_managed_block(target, "kit text") is True
    assert victim.read_text(encoding="utf-8") == "keep", "the planted link was not written through"
    backups = sorted(p.name for p in tmp_path.iterdir() if "backup" in p.name)
    assert len(backups) == 2, backups
    other = next(tmp_path / n for n in backups if (tmp_path / n) != squatter)
    assert other.read_text(encoding="utf-8") == "old kit text without markers\n"


# =====================================================================================================================
# Kit 2.2, group D: teardown, adoption, restart-pending bookkeeping
# =====================================================================================================================

def test_teardown_removes_the_moved_aside_directory_the_kit_created_when_it_is_empty(sim, script):
    _put_hand_copy(sim)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    backups = sim.home / ".openclaw" / "backup" / "hand-units"
    assert [p.name for p in backups.iterdir()], "adoption made a time-stamped directory"
    openclaw.teardown(s)
    assert list(backups.iterdir()) == [], "everything went back, so the empty stamp directory went too"
    assert all(p.exists() for p in units)


def test_a_moved_aside_directory_that_still_holds_a_file_is_kept(sim, script):
    _put_hand_copy(sim)
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    next(iter(units)).write_text("# edited after adoption\n", encoding="utf-8")
    openclaw.teardown(s)
    backups = sim.home / ".openclaw" / "backup" / "hand-units"
    assert [p.name for d in backups.iterdir() for p in d.iterdir()], "the original that was not restored stays"


def test_a_dangling_symlinked_hand_unit_is_adopted_as_a_link_and_restored_as_a_link(sim, script, tmp_path):
    import os
    _put_hand_copy(sim)
    sim.units.mkdir(parents=True, exist_ok=True)
    link = sim.units / "openclaw-health-restart.service"
    link.symlink_to(tmp_path / "dotfiles" / "not-there.service")    # the dotfiles repo is not checked out
    s = _state()
    _run_wizard(s)
    moved = pathlib.Path(s.openclaw.host_hand_moved[str(link)])
    assert moved.is_symlink() and os.readlink(moved) == str(tmp_path / "dotfiles" / "not-there.service")
    assert not link.is_symlink() and link.is_file(), "the kit's unit sits there now"
    warned = [m for m in script.messages("warn") if "dangling" in m]
    assert len(warned) == 1 and link.name in warned[0], "warned once, by name"
    openclaw.teardown(s)
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "dotfiles" / "not-there.service")
    assert s.openclaw.host_hand_moved == {}


def test_a_symlink_planted_in_the_moved_aside_directory_is_not_moved_onto_a_unit_path(sim, script, tmp_path):
    import os
    _backups, ts = _hand_move_fixture(sim)
    victim = _victim(tmp_path)
    sim.units.mkdir(parents=True, exist_ok=True)
    dst = sim.units / "openclaw-health-restart.timer"
    src = ts / "openclaw-health-restart.timer"
    src.symlink_to(victim)                                        # planted: the kit never moved a link here
    s = _moved_state(dst, src)
    section._remove_units(s.openclaw)
    assert not os.path.lexists(dst), "nothing was moved onto the unit path"
    assert src.is_symlink() and victim.read_text(encoding="utf-8") == "the operator's file\n"
    assert s.openclaw.host_hand_moved == {}
    assert any(str(dst) in m for m in script.messages("warn"))


def test_a_hard_linked_unit_file_is_restored_through_a_temp_file(sim, script, tmp_path):
    import os
    _put_hand_copy(sim)
    s = _state()
    _run_wizard(s)
    unit = sim.units / "openclaw-backup-daily.timer"
    assert unit.is_file()
    other_link = tmp_path / "another-name-for-the-same-inode"
    os.link(unit, other_link)
    kit_text = other_link.read_text(encoding="utf-8")
    s.openclaw.host_units_previous[unit.name] = "# the text before the kit\n"
    section._remove_units(s.openclaw)
    assert unit.read_text(encoding="utf-8") == "# the text before the kit\n"
    assert other_link.read_text(encoding="utf-8") == kit_text, "the other link keeps the bytes it had"


def test_the_entries_a_drained_window_applied_leave_the_pending_list(monkeypatch):
    s = _state()
    o = s.openclaw
    o.host_restart_pending = [
        {"op": "profile", "path": "gateway.reload.mode", "action": "filled", "source": "setup", "at": "t"},
        {"op": "profile", "path": "agents.defaults.timeoutSeconds", "action": "filled", "source": "setup", "at": "t"},
    ]
    monkeypatch.setattr(section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(section, "_drained_apply", lambda patch, rp, apply_patch: (True, True))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(ui, "warn", lambda *_a, **_k: None)
    monkeypatch.setattr(ui, "info", lambda *_a, **_k: None)
    patch = {"gateway": {"reload": {"mode": "hybrid"}}}
    changes = [{"path": ["gateway", "reload", "mode"], "action": "filled"}]
    assert section._handle_restart_part(o, patch, [], changes, source="setup",
                                        apply_patch=lambda *a, **k: (True, "")) is True
    assert [e["path"] for e in o.host_restart_pending] == ["agents.defaults.timeoutSeconds"], \
        "only what this run applied is cleared; the other key is still owed"


@pytest.mark.parametrize("value", [1.5, False, 0, [], {}, [1, "a"], {"a": [1]}, 2 ** 70])
def test_every_json_type_that_is_not_a_string_is_reported_as_not_a_string(sim, script, value):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous["OPENCLAW_OFFBOX_GCLOUD_CONFIG"] = value
    assert openclaw.teardown(s) == []
    warned = " ".join(script.messages("warn"))
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in warned and "not a string" in warned


def test_an_unfinished_teardown_names_its_reason_once_and_not_on_every_run(sim, script):
    s = _state()
    _run_wizard(s)
    s.openclaw.host_env_previous["OPENCLAW_OFFBOX_GCLOUD_CONFIG"] = ["a"]
    assert openclaw.teardown(s) == []
    first = [m for m in script.messages("warn") if "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in m]
    assert len(first) == 1
    seen = len(script.messages("warn"))
    assert openclaw.teardown(s) == []
    second = [m for m in script.messages("warn")[seen:] if "OPENCLAW_OFFBOX_GCLOUD_CONFIG" in m]
    assert second == [], "the same reason is not repeated on the next run"
    s.openclaw.host_env_previous["OPENCLAW_OFFBOX_GCLOUD_CONFIG"] = "ok-now"
    assert openclaw.teardown(s) != [], "once the reason is gone the teardown finishes"


# =====================================================================================================================
# Kit 2.2b, section 5: teardown of hand units
# =====================================================================================================================

def test_a_symlinked_stamp_directory_is_never_a_source(sim, script, tmp_path):
    """5a: `hand-units/<stamp>` is a link to a directory outside the kit's backup tree; the recorded entry is
    lexically inside it, so only the real path says it is not."""
    import os
    backups = sim.home / ".openclaw" / "backup" / "hand-units"
    backups.mkdir(parents=True)
    outside = tmp_path / "outside-stamp"
    outside.mkdir()
    planted = outside / "openclaw-health-restart.timer"
    planted.write_text("planted by someone else\n", encoding="utf-8")
    stamp = backups / "20260101T000000Z"
    stamp.symlink_to(outside, target_is_directory=True)
    sim.units.mkdir(parents=True, exist_ok=True)
    dst = sim.units / "openclaw-health-restart.timer"
    s = _moved_state(dst, stamp / "openclaw-health-restart.timer")
    section._remove_units(s.openclaw)
    assert not os.path.lexists(dst), "nothing was moved in from outside the backup tree"
    assert planted.read_text(encoding="utf-8") == "planted by someone else\n"
    assert s.openclaw.host_hand_moved == {}
    assert any(str(dst) in m for m in script.messages("warn"))


def test_the_destination_is_never_missing_while_a_hand_unit_is_put_back(sim, script, monkeypatch):
    """5b: one `os.replace` over the kit's copy, never unlink-then-move."""
    import hashlib
    import os
    import pathlib
    _backups, ts = _hand_move_fixture(sim)
    sim.units.mkdir(parents=True, exist_ok=True)
    dst = sim.units / "openclaw-health-restart.timer"
    kit_text = "# the kit's copy\n"
    dst.write_text(kit_text, encoding="utf-8")
    src = ts / "openclaw-health-restart.timer"
    src.write_text("# the operator's original\n", encoding="utf-8")
    s = _moved_state(dst, src)
    s.openclaw.host_hand_kit_sha[str(dst)] = hashlib.sha256(kit_text.encode("utf-8")).hexdigest()

    seen: list[tuple[str, bool]] = []
    real_unlink, real_replace, real_path_unlink = os.unlink, os.replace, pathlib.Path.unlink

    def spy_unlink(path, *a, **k):
        if str(path) == str(dst):
            seen.append(("unlink", True))
        return real_unlink(path, *a, **k)

    def spy_path_unlink(self, *a, **k):
        if str(self) == str(dst):
            seen.append(("unlink", True))
        return real_path_unlink(self, *a, **k)

    def spy_replace(a, b, *args, **k):
        if str(b) == str(dst):
            seen.append(("replace", os.path.lexists(dst)))
        return real_replace(a, b, *args, **k)

    monkeypatch.setattr(os, "unlink", spy_unlink)
    monkeypatch.setattr(pathlib.Path, "unlink", spy_path_unlink)
    monkeypatch.setattr(os, "replace", spy_replace)
    section._remove_units(s.openclaw)
    assert dst.read_text(encoding="utf-8") == "# the operator's original\n"
    assert ("unlink", True) not in seen, "the destination was removed before the original was put back"
    assert seen == [("replace", True)], seen


def test_a_dangling_script_link_in_local_bin_is_adopted_and_restored_as_a_link(sim, script, tmp_path):
    import os
    hand = sim.home / ".local" / "bin" / "openclaw-health-restart.sh"
    hand.parent.mkdir(parents=True)
    hand.symlink_to(tmp_path / "dotfiles" / "not-checked-out.sh")
    s = _state()
    _run_wizard(s)
    moved = pathlib.Path(s.openclaw.host_hand_moved[str(hand)])
    assert moved.is_symlink() and os.readlink(moved) == str(tmp_path / "dotfiles" / "not-checked-out.sh")
    assert not hand.is_symlink(), "the kit's own script sits there now"
    openclaw.teardown(s)
    assert hand.is_symlink() and os.readlink(hand) == str(tmp_path / "dotfiles" / "not-checked-out.sh")
    assert s.openclaw.host_hand_moved == {}


def test_install_units_writes_through_a_temp_file_so_a_hard_link_keeps_its_bytes(sim, tmp_path):
    import os
    dest = sim.units
    dest.mkdir(parents=True, exist_ok=True)
    first = host.install_units(dest, runner=_noop_runner, adopt_hand_copy=False)
    name = sorted(first["changed"])[0]
    unit = dest / name
    other = tmp_path / "another-name"
    os.link(unit, other)
    kit = other.read_text(encoding="utf-8")
    unit.write_text(kit + "# drifted\n", encoding="utf-8")           # same inode as `other`: both drifted
    other_bytes = other.read_text(encoding="utf-8")
    assert other_bytes.endswith("# drifted\n")
    res = host.install_units(dest, runner=_noop_runner, adopt_hand_copy=False)
    assert name in res["changed"] and unit.read_text(encoding="utf-8") == kit
    assert other.read_text(encoding="utf-8") == other_bytes, "the other link keeps the bytes it had"


def _noop_runner(argv, **kw):
    return 0, ""


# =====================================================================================================================
# Kit 2.2b, section 8: a refused restart-required apply is not a finished setup; stale temp files of every writer
# =====================================================================================================================

def _confirmed_restart_part(monkeypatch):
    monkeypatch.setattr(section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(ui, "warn", lambda *_a, **_k: None)
    monkeypatch.setattr(ui, "info", lambda *_a, **_k: None)


def test_a_confirmed_apply_that_the_window_refuses_stays_pending_and_is_remembered(monkeypatch):
    s = _state()
    o = s.openclaw
    _confirmed_restart_part(monkeypatch)
    monkeypatch.setattr(section, "_drained_apply", lambda patch, rp, apply_patch: (False, False))
    patch = {"gateway": {"reload": {"mode": "hybrid"}}}
    changes = [{"path": ["gateway", "reload", "mode"], "action": "filled"}]
    assert section._handle_restart_part(o, patch, [], changes, source="setup",
                                        apply_patch=lambda *a, **k: (True, "")) is False
    assert [e["path"] for e in o.host_restart_pending] == ["gateway.reload.mode"]
    assert getattr(o, "_refused_applies", []) == ["gateway.reload.mode"]


def _drive_apply_run(monkeypatch, tmp_path, *, drained_written):
    """wizard.run with the apply step replaced by one that asks the operator for a restart-required apply."""
    import argparse
    from ai_resources.setup import state as st
    _step10_env(monkeypatch, [])
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "error", lambda *a, **k: errors.append(a[0]))
    errors: list[str] = []
    saved: list = []
    monkeypatch.setattr(st, "load", lambda: st.SetupState())
    monkeypatch.setattr(st, "save", lambda s: saved.append(s))
    monkeypatch.setattr(st, "is_first_run", lambda: True)
    for step in ("_step1_mode", "_step2_cockpits", "_step6_single_model_profile", "_step7_cockpit_config",
                 "_step_models", "_step_preset", "_step_summary"):
        monkeypatch.setattr(wizard, step, lambda *a, **k: 0)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    monkeypatch.setattr(section, "_drained_apply", lambda patch, rp, apply_patch: (drained_written, drained_written))
    _confirmed_restart_part(monkeypatch)

    def apply(s, dry_run=False):
        wizard._APPLIED_TARGETS = []
        section._handle_restart_part(s.openclaw, {"gateway": {"reload": {"mode": "hybrid"}}}, [],
                                     [{"path": ["gateway", "reload", "mode"], "action": "filled"}],
                                     source="setup", apply_patch=lambda *a, **k: (True, ""))
        return 0
    monkeypatch.setattr(wizard, "_step9_apply", apply)
    rc = wizard.run(argparse.Namespace(dry_run=False, non_interactive=False, profile=""))
    return rc, errors, saved


def test_setup_exits_non_zero_when_a_requested_restart_required_apply_was_refused(script, monkeypatch, tmp_path):
    rc, errors, saved = _drive_apply_run(monkeypatch, tmp_path, drained_written=False)
    assert rc == 1
    assert any("NOT complete" in e and "gateway.reload.mode" in e for e in errors)
    assert [e["path"] for e in saved[-1].openclaw.host_restart_pending] == ["gateway.reload.mode"], \
        "the entry stays pending in the saved state"


def test_setup_exits_zero_when_the_requested_apply_was_done(script, monkeypatch, tmp_path):
    rc, errors, saved = _drive_apply_run(monkeypatch, tmp_path, drained_written=True)
    assert rc == 0 and not any("NOT complete" in e for e in errors)


def _stale_temp_fixture(directory: pathlib.Path, base: str):
    import os
    import time
    old = time.time() - 7200
    stale = directory / f".{base}.old12345.tmp"
    fresh = directory / f".{base}.new12345.tmp"
    other = directory / ".somebody-elses-file.old12345.tmp"
    for p in (stale, fresh, other):
        p.write_text("x", encoding="utf-8")
    for p in (stale, other):
        os.utime(p, (old, old))
    link = directory / f".{base}.link1234.tmp"
    link.symlink_to(other)
    os.utime(link, (old, old), follow_symlinks=False)
    sub = directory / f".{base}.dir12345.tmp"
    sub.mkdir()
    os.utime(sub, (old, old))
    return stale, fresh, other, link, sub


def test_a_write_of_kit_host_env_removes_the_stale_temp_files_a_killed_write_left(tmp_path):
    env_file = tmp_path / "kit-host.env"
    stale, fresh, other, link, sub = _stale_temp_fixture(tmp_path, "kit-host.env")
    assert host.write_host_env({"OPENCLAW_A": "1"}, env_file) is True
    assert not stale.exists(), "older than an hour, a regular file of ours: removed"
    assert fresh.exists(), "a younger one may be a write in flight"
    assert other.exists() and link.is_symlink() and sub.is_dir(), "only kit-owned regular files"


def test_a_managed_block_write_removes_the_stale_temp_files_of_agents_md(tmp_path):
    from ai_resources.setup.cockpits import _shared
    target = tmp_path / "AGENTS.md"
    target.write_text("# mine\n", encoding="utf-8")
    stale, fresh, other, link, sub = _stale_temp_fixture(tmp_path, "AGENTS.md")
    assert _shared.write_managed_block(target, "kit text\n") is True
    assert not stale.exists() and fresh.exists() and other.exists() and link.is_symlink() and sub.is_dir()


def test_a_window_refused_inside_the_gateway_cgroup_says_t29_and_not_a_bare_exit_code(script, monkeypatch):
    monkeypatch.setattr(host, "apply_drained", lambda *a, **k: (host.EXIT_REFUSED_SELF, False))
    errors: list[str] = []
    monkeypatch.setattr(ui, "error", lambda m, *a, **k: errors.append(m))
    assert section._drained_apply({"a": 1}, [], lambda *a, **k: (True, "")) == (False, False)
    assert len(errors) == 1 and "T29" in errors[0] and "apply-pending" in errors[0]
    assert "ended with code" not in errors[0]


# =====================================================================================================================
# Kit 2.2c, 6a: a symlinked hand-units directory (or a parent of it) is never a source
# =====================================================================================================================

def _evil_stamp(tmp_path, name="openclaw-health-restart.timer"):
    evil = tmp_path / "evil-hand-units"
    stamp = evil / "20260101T000000Z"
    stamp.mkdir(parents=True)
    planted = stamp / name
    planted.write_text("planted by someone else\n", encoding="utf-8")
    return evil, stamp, planted


def test_a_symlinked_hand_units_directory_is_refused_with_one_clear_line(sim, script, tmp_path):
    import os
    evil, stamp, planted = _evil_stamp(tmp_path)
    backup = sim.home / ".openclaw" / "backup"
    backup.mkdir(parents=True)
    (backup / "hand-units").symlink_to(evil, target_is_directory=True)
    sim.units.mkdir(parents=True, exist_ok=True)
    dst = sim.units / "openclaw-health-restart.timer"
    original = "# the unit the operator may have\n"
    dst.write_text(original, encoding="utf-8")
    s = _moved_state(dst, backup / "hand-units" / stamp.name / planted.name)
    section._remove_units(s.openclaw)
    assert dst.read_text(encoding="utf-8") == original, "nothing was moved onto the unit path"
    assert planted.read_text(encoding="utf-8") == "planted by someone else\n" and planted.exists()
    warns = [m for m in script.messages("warn") if "hand-units" in m]
    assert len(warns) == 1 and "is a symlink" in warns[0]
    assert "not a unit the kit manages" not in warns[0]
    assert str(dst) in s.openclaw.host_hand_moved, "the record stays, so a fixed link can still be restored"


def test_a_symlinked_parent_of_the_hand_units_directory_is_refused_too(sim, script, tmp_path):
    import os
    real = tmp_path / "real-backup"
    (real / "hand-units" / "20260101T000000Z").mkdir(parents=True)
    planted = real / "hand-units" / "20260101T000000Z" / "openclaw-health-restart.timer"
    planted.write_text("planted\n", encoding="utf-8")
    (sim.home / ".openclaw").mkdir(parents=True)
    (sim.home / ".openclaw" / "backup").symlink_to(real, target_is_directory=True)
    sim.units.mkdir(parents=True, exist_ok=True)
    dst = sim.units / "openclaw-health-restart.timer"
    s = _moved_state(dst, sim.home / ".openclaw" / "backup" / "hand-units" / "20260101T000000Z" / planted.name)
    section._remove_units(s.openclaw)
    assert not os.path.lexists(dst)
    assert planted.exists()
    warns = [m for m in script.messages("warn") if "is a symlink" in m]
    assert len(warns) == 1 and str(sim.home / ".openclaw" / "backup") in warns[0]


def test_a_real_hand_units_directory_is_still_restored_from(sim, script):
    _backups, ts = _hand_move_fixture(sim)
    dst = sim.home / ".local" / "bin" / host.HAND_SCRIPT_NAME
    dst.parent.mkdir(parents=True)
    src = ts / host.HAND_SCRIPT_NAME
    src.write_text("hand\n", encoding="utf-8")
    s = _moved_state(dst, src)
    section._remove_units(s.openclaw)
    assert dst.read_text(encoding="utf-8") == "hand\n"
    assert not [m for m in script.messages("warn") if "is a symlink" in m]


# =====================================================================================================================
# Kit 2.2c, 6b: the kit-host.env target is validated before any gcloud call
# =====================================================================================================================

def _offbox_args(**kw):
    import argparse
    base = dict(dry_run=False, remove=False, yes=True, name="openclaw-backup", sa=host.OFFBOX_SA_DEFAULT,
                account="me@example.org", project="")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def offbox_env(monkeypatch, tmp_path):
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    saved: list[object] = []
    monkeypatch.setattr(state, "load", lambda: state.SetupState())
    monkeypatch.setattr(state, "save", lambda s: saved.append(s))
    return g, saved


@pytest.mark.parametrize("kind", ["directory", "dev-null-link", "dangling-link"])
def test_an_unusable_kit_host_env_refuses_before_gcloud_creates_anything(offbox_env, monkeypatch, tmp_path, capsys,
                                                                       kind):
    g, saved = offbox_env
    env = tmp_path / "openclaw" / "kit-host.env"
    env.parent.mkdir()
    if kind == "directory":
        env.mkdir()
    elif kind == "dev-null-link":
        env.symlink_to("/dev/null")
    else:
        env.symlink_to(tmp_path / "missing-dir" / "kit-host.env")
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    assert host.cmd_offbox_identity(_offbox_args()) == 1
    out = capsys.readouterr()
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("refusing:") and "nothing was created" in lines[0]
    assert g.calls == [], "no gcloud call at all, not even a read"
    assert saved == []
    assert "Traceback" not in out.err


def test_a_usable_kit_host_env_is_not_refused_and_a_missing_one_is_created(offbox_env, monkeypatch, tmp_path):
    g, saved = offbox_env
    env = tmp_path / "openclaw" / "kit-host.env"
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    assert host.host_env_problem(env) == ""
    assert host.cmd_offbox_identity(_offbox_args()) == 0
    assert "OPENCLAW_OFFBOX_GCLOUD_CONFIG=openclaw-backup" in env.read_text(encoding="utf-8")
    assert g.writes() != []


def test_a_dry_run_is_not_refused_for_an_unusable_env_file(offbox_env, monkeypatch, tmp_path):
    g, _saved = offbox_env
    env = tmp_path / "kit-host.env"
    env.mkdir()
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    assert host.cmd_offbox_identity(_offbox_args(dry_run=True, yes=False)) == 0
    assert g.writes() == []


def test_the_wizard_step_refuses_an_unusable_kit_host_env_before_gcloud(monkeypatch, tmp_path):
    g = FakeGcloud()
    monkeypatch.setattr(section, "_runner", lambda: g)
    env = tmp_path / "kit-host.env"
    env.mkdir()
    monkeypatch.setattr(host, "HOST_ENV_PATH", env)
    o = _state().openclaw
    o.host_offbox_identity = True
    section._configure_offbox_identity(o, dry_run=False)
    assert g.calls == [] and o.host_offbox_config_created == {}


def test_a_corrupt_setup_state_refuses_offbox_identity_before_gcloud_creates_anything(monkeypatch, tmp_path,
                                                                                       capsys):
    g = FakeGcloud()
    monkeypatch.setattr(host, "default_runner", g)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "openclaw" / "kit-host.env")
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("profile: [unclosed\n  name: {oops\n", encoding="utf-8")
    assert host.cmd_offbox_identity(_offbox_args()) == 1
    out = capsys.readouterr()
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("refusing:") and "not valid YAML" in lines[0]
    assert g.calls == [], "no gcloud configuration was created"
    assert "Traceback" not in out.err
    assert path.read_text(encoding="utf-8").startswith("profile: [unclosed")


def test_a_corrupt_setup_state_refuses_apply_pending_with_one_line(monkeypatch, tmp_path, capsys):
    import argparse
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("a: [broken\n", encoding="utf-8")
    ns = argparse.Namespace(discard_watch_restores=False, yes=True)
    assert host.cmd_apply_pending(ns) == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("refusing:")


# =====================================================================================================================
# Kit 2.2d, 3a: a refused hand-unit restore keeps its record and the teardown says it is unfinished
# =====================================================================================================================

def test_a_symlinked_hand_units_directory_leaves_the_teardown_unfinished_and_a_second_one_restores(
        sim, script, tmp_path):
    hand = _put_hand_copy(sim)
    hand.chmod(0o755)
    original = hand.read_text(encoding="utf-8")
    units = _put_hand_units(sim)
    s = _state()
    _run_wizard(s)
    recorded = dict(s.openclaw.host_hand_moved)
    shas = dict(s.openclaw.host_hand_kit_sha)
    assert str(hand) in recorded and shas
    backups = sim.home / ".openclaw" / "backup" / "hand-units"
    relocated = tmp_path / "relocated-hand-units"
    backups.rename(relocated)
    backups.symlink_to(relocated, target_is_directory=True)

    assert openclaw.teardown(s) == [], "unfinished: nothing was torn down"
    assert s.openclaw.host_hand_moved == recorded, "the record of what was moved aside is kept"
    assert s.openclaw.host_hand_kit_sha == shas
    assert s.openclaw.host_units_applied is True and section.applied_any(s.openclaw)
    assert not hand.exists(), "the operator's script is still in the (relocated) backup tree, not put back"
    for p in units:
        assert p.exists(), "the kit's own units stay installed"
    assert any("is a symlink" in m for m in script.messages("warn"))

    backups.unlink()                                   # the operator repairs the link
    relocated.rename(backups)
    assert openclaw.teardown(s) != [], "finished"
    assert hand.read_text(encoding="utf-8") == original
    assert s.openclaw.host_hand_moved == {}
    for p, text in units.items():
        assert p.read_bytes() == text


# =====================================================================================================================
# Kit 2.2d, 4: the branches of host_env_problem
# =====================================================================================================================

import os as _os  # noqa: E402


def test_host_env_problem_names_each_branch_in_one_clause(tmp_path):
    env = tmp_path / "kit-host.env"
    assert host.host_env_problem(env) == ""
    env.mkdir()
    assert host.host_env_problem(env) == f"{env} is a directory, not a file"
    env.rmdir()
    env.symlink_to("/dev/null")
    assert host.host_env_problem(env) == f"{env} (a link to /dev/null) is not a regular file"
    env.unlink()
    env.symlink_to(tmp_path / "gone" / "kit-host.env")
    assert host.host_env_problem(env) == (f"{env} is a symlink to {tmp_path / 'gone' / 'kit-host.env'}, "
                                          "whose directory does not exist")
    env.unlink()
    env.write_text("A=1\n", encoding="utf-8")
    assert host.host_env_problem(env) == ""


@pytest.mark.skipif(_os.geteuid() == 0, reason="root can write anywhere")
def test_host_env_problem_names_an_unwritable_parent(tmp_path):
    parent = tmp_path / "ro"
    parent.mkdir()
    env = parent / "kit-host.env"
    parent.chmod(0o555)
    try:
        assert host.host_env_problem(env) == f"{parent} is not writable, so {env} cannot be created"
        env_missing_parent = parent / "sub" / "kit-host.env"
        assert host.host_env_problem(env_missing_parent) == (
            f"{parent} is not writable, so {env_missing_parent} cannot be created")
    finally:
        parent.chmod(0o755)
    env.write_text("A=1\n", encoding="utf-8")
    parent.chmod(0o555)
    try:
        assert host.host_env_problem(env) == f"{parent} is not writable, so {env} cannot be replaced"
    finally:
        parent.chmod(0o755)
