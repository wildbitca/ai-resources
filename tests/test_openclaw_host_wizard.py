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
    "Enable the workboard plugin (a shared": True,
    "Write an AGENTS.md into every agent workspace": True,
    "Check this host against the documented setup": False,
}
GATES = {
    "Install the openclaw-* systemd units and enable": True,
    "Apply this patch to the running gateway's config": True,
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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(h.cfg))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host, "HOST_ENV_PATH", h.env_file)
    monkeypatch.setattr(openclaw, "CONFIG_ROOT", home / ".openclaw")
    monkeypatch.setattr(openclaw, "_openclaw", h.oc)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(section, "_runner", lambda: h.systemd)
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
    assert len(defaults) == 12, sorted(defaults)


def test_declining_the_master_question_asks_nothing_else(sim, script):
    script.answers["Configure this machine as an OpenClaw host"] = False
    s = _state()
    openclaw.prompt(s)
    assert s.openclaw.host is False
    assert len(script.asked) == 1


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
                   "OPENCLAW_NARRATION": "milestones", "OPENCLAW_GUARD": "1"}

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
    [(dry_args, dry_patch)] = sim.oc.dry_patches()
    [(args, patch)] = sim.oc.real_patches()
    assert dry_patch == patch and "--stdin" in args
    assert sim.oc.calls.index((dry_args, dry_patch)) < sim.oc.calls.index((args, patch))
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

    # AGENTS.md: a template where there was none; an existing file, and the engine-owned agent, untouched.
    for aid in ("main", "infra", "docs"):
        assert (sim.workspaces[aid] / "AGENTS.md").is_file()
    assert (sim.workspaces["app"] / "AGENTS.md").read_text(encoding="utf-8") == "# mine\n"
    assert not (sim.workspaces["claude"] / "AGENTS.md").exists()
    assert len(o.host_agents_md_written) == 3

    # The unpinned MCP server is reported, never rewritten; the gateway is never restarted.
    assert any("supa" in m and "@latest" in m for m in script.messages("warn"))
    assert doc["mcp"] == json.loads(sim.original_config)["mcp"]
    assert section.RESTART_NOTE in script.messages("warn")
    for argv in [a for a, _ in sim.oc.calls] + sim.systemd.calls:
        assert "restart" not in argv and "doctor" not in argv
        assert not (argv[:3] == ["systemctl", "--user", "stop"] or "openclaw-gateway.service" in argv)


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
    assert sim.files() == before and not sim.oc.real_patches() and not sim.systemd.calls
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
        + [c for c in sim.systemd.calls if c[2] in ("enable", "daemon-reload", "disable")]
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
