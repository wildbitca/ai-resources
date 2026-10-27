"""Keep the suite away from the operator's real ~/.claude.

The kit writes hook registrations into ~/.claude/settings.json. A test that reaches that file with a
fake kit path (the suite uses "/kit") leaves a hook pointing at a file that does not exist, and a
UserPromptSubmit hook that fails blocks every prompt in every Claude Code session on the host.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Before the tmpdir plugin reads the option: a bare run must not fill the /tmp tmpfs.
    from basetemp_default import apply_default
    apply_default(config)
    config.addinivalue_line("markers", "timing: asserts on wall-clock time; runs in the serial pass of scripts/run-tests.sh")
    config.addinivalue_line("markers", "node_scripts: runs node on a repo workflow script through tests/kit_script_harness.mjs; the guard lets through only that harness invocation (see _allow_node_harness)")
    config.addinivalue_line("markers", "agy_waiver: sees the shipped operator waiver of the Antigravity gates")


# Host binaries no test may run. One unstubbed configure path ran `npm install -g openclaw@latest` and
# `systemctl --user enable --now` on the operator's machine on every full run; under xdist the concurrent
# installs swapped the live gateway's files mid-start and left it down (2026-10-23). A stub script under the
# test's own temp dir or the repo is still allowed, so tests that put a fake `openclaw` on PATH keep working.
HOST_BINARIES = frozenset({
    "systemctl", "loginctl", "journalctl", "crontab", "sudo",
    "npm", "npx", "pnpm", "node", "brew",
    "openclaw", "claude", "agy", "gemini", "engram",
    "tailscale", "gcloud", "kubectl", "flux", "helm", "docker",
    "gh", "curl", "wget", "ssh",
})
_REPO = Path(__file__).resolve().parents[1]


SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
# Programs that run another program given in their arguments.
WRAPPERS = frozenset({"env", "timeout", "nohup", "setsid", "xargs", "stdbuf", "nice", "ionice", "command", "exec",
                      "time", "chronic", "unbuffer", "flock", "script"})
_SHELL_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "exec"})
_OPERATOR_CHARS = frozenset("();&|<>")


def _word_refused(word, env, allowed_roots) -> str | None:
    """The word's name when it is a host binary that resolves outside the allowed roots."""
    import os
    import shutil
    if os.path.basename(word) not in HOST_BINARIES:
        return None
    path = word if os.sep in word else shutil.which(word, path=(env or os.environ).get("PATH"))
    if path is None:
        return None                                   # not installed: Popen raises FileNotFoundError by itself
    real = os.path.realpath(path)
    if any(real == root or real.startswith(root + os.sep) for root in allowed_roots):
        return None
    return os.path.basename(word)


def _shell_segments(text) -> list[list[str]]:
    """The simple commands of a shell string: split on newlines, backticks and every control operator."""
    import shlex
    text = text.replace("\n", ";").replace("`", ";")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()
    segments, current = [], []
    for tok in tokens:
        if tok and all(c in _OPERATOR_CHARS for c in tok):
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return [seg for seg in segments if seg]


def _words_refused(words, env, allowed_roots) -> str | None:
    import os
    # Leading assignments (FOO=1 npm i) and shell keywords (then npm i) do not change what runs.
    while words and (words[0] in _SHELL_KEYWORDS - {"exec"} or ("=" in words[0] and not words[0].startswith("-")
                                                              and os.sep not in words[0].split("=", 1)[0])):
        words = words[1:]
    if not words:
        return None
    blocked = _word_refused(words[0], env, allowed_roots)
    if blocked:
        return blocked
    base = os.path.basename(words[0])
    if base in SHELLS:
        for i, w in enumerate(words[1:], 1):
            if w.startswith("-") and not w.startswith("--") and "c" in w and i + 1 < len(words):
                return _string_refused(words[i + 1], env, allowed_roots)
        return None
    if base in WRAPPERS:
        # `timeout -s KILL 5 npm i`, `env -i FOO=1 bash -c ...`: try every later word as the start of the real command.
        for i in range(1, len(words)):
            blocked = _words_refused(words[i:], env, allowed_roots)
            if blocked:
                return blocked
    return None


def _string_refused(text, env, allowed_roots) -> str | None:
    for seg in _shell_segments(text):
        blocked = _words_refused(seg, env, allowed_roots)
        if blocked:
            return blocked
    return None


def host_binary_refused(args, env, allowed_roots, executable=None) -> str | None:
    """The blocked binary's name when `args` (or `executable`) would run a real host binary, else None.

    Looks through shells (`bash -c "cd x && npm i -g y"`), wrappers (env, timeout, nohup, setsid, xargs ...) and every
    command of a shell string, not just the first word."""
    import os
    if executable is not None:
        blocked = _word_refused(os.fsdecode(executable), env, allowed_roots)
        if blocked:
            return blocked
    if isinstance(args, (str, bytes)):
        return _string_refused(os.fsdecode(args), env, allowed_roots)
    return _words_refused([os.fsdecode(a) for a in args], env, allowed_roots)


def _allow_node_harness(monkeypatch):
    """Let exactly `node tests/kit_script_harness.mjs <repo workflow script> <config>` run, and no other node call.

    The harness only reads the script and the config and prints JSON (no install, no network); `node -e`, another
    script or a different argument count stay refused, so the guard keeps meaning for every other test."""
    import os
    real = host_binary_refused
    harness = str(_REPO / "tests" / "kit_script_harness.mjs")
    scripts = str(_REPO / "workflows" / "scripts") + os.sep

    def guarded(args, env, allowed_roots, executable=None):
        if (isinstance(args, (list, tuple)) and len(args) == 4 and executable is None
                and os.path.basename(os.fsdecode(args[0])) == "node" and os.fsdecode(args[1]) == harness
                and os.path.realpath(os.fsdecode(args[2])).startswith(scripts)):
            return None
        return real(args, env, allowed_roots, executable)

    monkeypatch.setitem(globals(), "host_binary_refused", guarded)


@pytest.fixture(autouse=True)
def _no_host_binaries(request, tmp_path_factory, monkeypatch):
    if request.node.get_closest_marker("node_scripts"):
        _allow_node_harness(monkeypatch)
    import errno
    import os
    import subprocess
    import tempfile
    allowed_roots = tuple(os.path.realpath(str(p)) for p in
                          (tmp_path_factory.getbasetemp(), tempfile.gettempdir(), _REPO))
    real_popen = subprocess.Popen

    def _refuse(blocked):
        # Exactly what a host without the binary raises, so callers take their "not installed" path.
        return FileNotFoundError(errno.ENOENT, f"host binary blocked in tests: {blocked}", blocked)

    class _GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            blocked = host_binary_refused(args, kw.get("env"), allowed_roots, kw.get("executable"))
            if blocked:
                raise _refuse(blocked)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)

    # The same refusal on the paths that never go through subprocess.Popen.
    def _guard_exec(name, argv_index, env_index=None):
        real = getattr(os, name, None)
        if real is None:
            return

        def guarded(*a, **kw):
            argv = a[argv_index] if len(a) > argv_index else ()
            env = a[env_index] if env_index is not None and len(a) > env_index else None
            blocked = (_word_refused(os.fsdecode(a[0]), env, allowed_roots) if a else None) \
                or host_binary_refused(list(argv) or [], env, allowed_roots)
            if blocked:
                raise _refuse(blocked)
            return real(*a, **kw)

        monkeypatch.setattr(os, name, guarded)

    for name, env_i in (("execv", None), ("execve", 2), ("execvp", None), ("execvpe", 2),
                        ("posix_spawn", 2), ("posix_spawnp", 2)):
        _guard_exec(name, 1, env_i)

    real_system = os.system

    def _guarded_system(command):
        blocked = host_binary_refused(os.fsdecode(command), None, allowed_roots)
        if blocked:
            raise _refuse(blocked)
        return real_system(command)

    monkeypatch.setattr(os, "system", _guarded_system)


@pytest.fixture(autouse=True)
def _isolate_claude_settings(tmp_path_factory, monkeypatch):
    try:
        from ai_resources.setup.cockpits import claude
    except Exception:  # a test that does not import the package needs no isolation
        return
    root = tmp_path_factory.mktemp("claude-home")
    # The setup state (and its lock file) resolve through XDG_CONFIG_HOME: a test that does not set it itself
    # must never reach the operator's real ~/.config/ai-resources.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-config"))
    # ADR-0015: the scoped OpenClaw skills links and the orchestrator runs live under the XDG state home; a test must
    # never create them under the operator's real ~/.local/state.
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    # The doctor's vendor pin check asks upstream with `git ls-remote`; the suite never reaches the network.
    monkeypatch.setenv("AI_RESOURCES_OFFLINE", "1")
    monkeypatch.setattr(claude, "CONFIG_ROOT", root, raising=False)
    monkeypatch.setattr(claude, "SETTINGS_PATH", root / "settings.json", raising=False)
    # CLAUDE_MD_PATH, AGENTS_DIR and WORKFLOWS_DIR are also computed from the real HOME at import time, so a test that
    # called `claude.configure()` rewrote the operator's real ~/.claude/CLAUDE.md managed block on every full run.
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", root / "CLAUDE.md", raising=False)
    monkeypatch.setattr(claude, "AGENTS_DIR", root / "agents", raising=False)
    monkeypatch.setattr(claude, "WORKFLOWS_DIR", root / "workflows", raising=False)
    # The same for ~/.openclaw: the host section writes kit-host.env and the drained doctor
    # creates watchdog.off, and both paths are resolved from the real HOME at import time.
    from ai_resources import openclaw_host
    monkeypatch.setattr(openclaw_host, "HOST_ENV_PATH", root / "openclaw" / "kit-host.env", raising=False)
    # 2.8.0: the shared-MCP token step looks for sentry/figma in the real ~/.claude.json and ~/.openclaw/openclaw.json
    # (and the token in ~/.openclaw/.env); a test that wants either passes it in.
    from ai_resources import shared_mcp
    monkeypatch.setattr(shared_mcp, "in_use_here", lambda: set(), raising=False)
    monkeypatch.setattr(shared_mcp, "_found_elsewhere", lambda var, openclaw_env=None: "", raising=False)
    monkeypatch.setattr(openclaw_host, "WATCHDOG_OFF", root / "openclaw" / "watchdog.off", raising=False)
    # ADR-0004: the graceful restart's state, lock and snapshots live under ~/.openclaw too.
    monkeypatch.setattr(openclaw_host, "STATE_PATH", root / "openclaw" / "health-restart.state", raising=False)
    monkeypatch.setattr(openclaw_host, "LOCK_PATH", root / "openclaw" / "health-restart.lock", raising=False)
    # Whether the test run itself sits in the gateway cgroup must not decide the outcome of a restart test:
    # the self-cgroup refusal (T29) reads this path, and the tests that exercise it pass their own file.
    monkeypatch.setattr(openclaw_host, "SELF_CGROUP_PATH", str(root / "openclaw" / "self-cgroup"), raising=False)
    monkeypatch.setattr(openclaw_host, "SNAPSHOT_ROOT", root / "openclaw" / "logs" / "restart-snapshots",
                        raising=False)
    # And the OpenClaw cockpit's own root. A teardown test whose config names no
    # `agents.defaults.workspace` resolves the workspace to `CONFIG_ROOT / "workspace"`, which was
    # the operator's real ~/.openclaw/workspace: its teardown removed the kit block from the live
    # main AGENTS.md on every test run, and that was the "block missing from main" regression
    # v1.9.7 reported as fixed (1.10.2).
    from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
    monkeypatch.setattr(openclaw_cockpit, "CONFIG_ROOT", root / "openclaw-home", raising=False)
    # The kit's trim of the Engram plugin hook rewrites a file inside ~/.claude/plugins: a test that calls
    # `claude.configure()` must never reach the operator's real plugin directory. Tests of the trim pass `home`.
    # `claude.configure()` also runs `claude plugin marketplace add` and `claude plugin install engram` through the host's
    # real `claude` when both binaries exist (a network call and a write into ~/.claude/plugins): no test may do that.
    # A test that exercises the installer itself passes its own stub.
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False, raising=False)
    from ai_resources.setup import engram_patch
    _real_plugin_dirs = engram_patch.plugin_dirs
    monkeypatch.setattr(engram_patch, "plugin_dirs",
                        lambda home=None: _real_plugin_dirs(home) if home is not None else [])
    # And the model pins overlay (~/.config/ai-resources/model-pins.json): a host overlay must never
    # leak into a test, and a test must never write the operator's real one.
    from ai_resources import agy_gate_evidence, model_pins
    monkeypatch.setattr(model_pins, "overlay_path", lambda: root / "model-pins.json")
    # ADR-0016 D3: the gate-record module has no host overlay any more; there is nothing to switch off.
    # And the restart guard's two systemd reads: an unstubbed `restart_gateway()` would ask the
    # live unit whether agent turns are running, and a busy host would defer a restart a test
    # expects to happen. The guard's own tests pass a fake probe explicitly.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_busy", lambda: (0, []))
    # And the restart-required gate (ADR-0003): it asks `openclaw --version` and the cgroup. Unstubbed, a fake
    # `_openclaw` would answer no version (so every key reads restart-required) and a real probe would read the
    # live gateway. test_openclaw_restart_gate.py sets both itself.
    from ai_resources import openclaw_reload_rules
    from reload_rules_fixture import FIXTURE_VERSION, fixture_ruleset
    monkeypatch.setattr(openclaw_reload_rules, "installed_openclaw_version",
                        lambda runner, **kw: FIXTURE_VERSION)
    # The rules come from a checked-in dist excerpt, never from the host's OpenClaw (C5). A test that needs the
    # "cannot read the rules" case overrides this; the version is whatever `installed_openclaw_version` says now.
    monkeypatch.setattr(
        openclaw_reload_rules, "current_ruleset",
        lambda runner, **kw: fixture_ruleset(version=openclaw_reload_rules.installed_openclaw_version(runner, **kw)))
    # E3: the package.json lookup would read the operator's installed OpenClaw; a test never depends on it.
    monkeypatch.setattr(openclaw_reload_rules, "_find_package_json", lambda: None)
    from ai_resources.setup.cockpits import _openclaw_host as host_section
    monkeypatch.setattr(host_section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(openclaw_cockpit, "_live_reload_mode", lambda: None)
    monkeypatch.setattr(openclaw_cockpit, "_gateway_main_pid", lambda: "")
    # The image question lists which providers OpenClaw can already sign in to by running `openclaw models status
    # --json`; a test never runs the host's openclaw. The image tests pass their own accounts.
    monkeypatch.setattr(openclaw_cockpit, "_image_accounts", lambda: ({}, []))
    # And the start-time read behind `plugin_is_stale`'s time rule: a tmp plugin dir is always newer
    # than the live gateway, so an unstubbed call would make every staleness test report stale.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_started_at", lambda: None)


@pytest.fixture
def previous_generation_pins(monkeypatch):
    """Kit defaults pinned to the generation the recorded model fixtures were written against."""
    from pin_generation import pin_previous_generation
    pin_previous_generation(monkeypatch)


@pytest.fixture(autouse=True)
def _no_host_command_snapshots(monkeypatch):
    """`lanes` snapshots `crontab -l` and the user timer list: tests never run the host's own commands."""
    try:
        from ai_resources import lanes_cmd
    except Exception:
        return
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", lambda argv: (127, "", ""), raising=False)


EXTRA_TOOL_DETECTORS = (("detect_cursor_agent", "Cursor Agent CLI"), ("detect_gemini_cli", "Gemini CLI"),
                        ("detect_codex", "Codex CLI"), ("detect_aider", "Aider"),
                        ("detect_copilot", "GitHub Copilot CLI"), ("detect_continue", "Continue.dev CLI"),
                        ("detect_opencode", "OpenCode"), ("detect_windsurf", "Windsurf"))


@pytest.fixture
def extra_tools_present(monkeypatch):
    """The tool selector knows more CLIs than claude/agy/openclaw. Tests written for those three keep the other
    detectors off the host (no real `--version` subprocess) by reporting them installed, so they never change
    what is selectable. Tests about the extra CLIs set their own detectors after this fixture."""
    from ai_resources.setup import detection
    for fn, name in EXTRA_TOOL_DETECTORS:
        monkeypatch.setattr(detection, fn, lambda n=name: detection.Detected(n, True, "1.0", "/usr/bin/x"))


@pytest.fixture(autouse=True)
def _agy_not_installed_by_default(monkeypatch):
    """Whether the host has `agy` must not decide a wizard test: the Antigravity visibility rows (Custom list and
    preset tables) read `detection.agy_installed`, which looks the binary up and never runs it. A test that
    needs the installed case sets it itself."""
    try:
        from ai_resources.setup import detection
    except Exception:
        return
    monkeypatch.setattr(detection, "agy_installed", lambda: False, raising=False)


@pytest.fixture(autouse=True)
def _not_an_openclaw_spawned_session(monkeypatch):
    """Whether the suite itself runs inside an OpenClaw session must not decide its outcome.

    `hygiene.is_headless` treats OPENCLAW_CLI=1 (set for every process an OpenClaw agent spawns) as "nothing can
    answer a prompt", so setup skipped its interactive steps and the scripted-wizard tests failed there. A test
    that wants the headless path sets the variable (or patches `is_headless`) itself.
    """
    monkeypatch.delenv("OPENCLAW_CLI", raising=False)


@pytest.fixture(autouse=True)
def _reset_assume_yes():
    """`wizard.run` stores `--yes` in ui._ASSUME_YES (and a test may set it directly): no test may leak it to the next."""
    yield
    try:
        from ai_resources.setup import ui
    except Exception:
        return
    ui._ASSUME_YES = False
    ui.set_assume_yes(False)


@pytest.fixture(autouse=True)
def _isolate_home_derived_paths(tmp_path_factory, monkeypatch):
    """Round 2 (ADR-0014): every reader or writer that resolves a path from the real HOME is pointed at a scratch
    directory before its first test: the billing reader (~/.claude.json, credits.json, the statusline cache)."""
    root = tmp_path_factory.mktemp("home-derived")
    # The cost snapshots (doctor's poll-share check reads the newest one) live under XDG_STATE_HOME.
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    try:
        from ai_resources import billing
    except Exception:
        return
    monkeypatch.setattr(billing, "claude_json_path", lambda: root / ".claude.json")
    monkeypatch.setattr(billing, "credits_path", lambda: root / "config" / "ai-resources" / "credits.json")
    monkeypatch.setattr(billing, "sidecar_path", lambda: root / "cache" / "claude-rate-limits.json")
    monkeypatch.setattr(billing, "sidecar_candidates", lambda: [root / "cache" / "claude-rate-limits.json"])
    from ai_resources import audit_snapshot
    monkeypatch.setattr(audit_snapshot, "openclaw_json_path", lambda home=None: (home or root / "home") / ".openclaw" / "openclaw.json")


@pytest.fixture(autouse=True)
def _isolate_usage_index(tmp_path_factory, monkeypatch):
    """The usage index reads every Claude Code transcript and caches under ~/.cache/ai-resources (ADR-0014 S5)."""
    root = tmp_path_factory.mktemp("usage-index")
    try:
        from ai_resources import usage_index
    except Exception:
        return
    monkeypatch.setattr(usage_index, "cache_path", lambda: root / "cache" / "ai-resources" / "usage-index.json")
    monkeypatch.setattr(usage_index, "default_roots", lambda: [])


@pytest.fixture(autouse=True)
def _isolate_hygiene(tmp_path_factory, monkeypatch):
    """The hygiene step runs the `claude` CLI and reads ~/.claude.json (ADR-0014 D3.2): a test never reaches either."""
    root = tmp_path_factory.mktemp("hygiene")
    try:
        from ai_resources.setup import hygiene
    except Exception:
        return
    monkeypatch.setattr(hygiene, "default_runner", lambda argv: (127, "claude is not run by tests"))
    monkeypatch.setattr(hygiene, "backup_dir", lambda: root / "hygiene")


@pytest.fixture(autouse=True)
def _isolate_gemini_home(tmp_path_factory, monkeypatch):
    """~/.gemini (agy's settings and MCP file) is resolved from the real HOME at import time and by `agy_mcp.mcp_path`:
    ADR-0014 D3.4 lets the kit write the MCP file, so no test may reach it."""
    root = tmp_path_factory.mktemp("gemini-home")
    try:
        from ai_resources.setup import agy_mcp
        from ai_resources.setup.cockpits import gemini
    except Exception:
        return
    monkeypatch.setattr(agy_mcp, "mcp_path", lambda: root / ".gemini" / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "CONFIG_ROOT", root / ".gemini", raising=False)
    monkeypatch.setattr(gemini, "SETTINGS_PATH", root / ".gemini" / "settings.json", raising=False)
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", root / ".gemini" / "config" / "mcp_config.json", raising=False)
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", root / ".gemini" / "GEMINI.md", raising=False)


@pytest.fixture(autouse=True)
def _isolate_round4(tmp_path_factory, monkeypatch):
    """ADR-0016: the live readers, the native sign-in handovers and the operator-rules file never reach the host.

    - `live_auth.default_runner` answers "not installed" instead of running the operator's `claude`, `openclaw` or `agy`;
    - `live_auth.handoff` FAILS the test when a native flow (`claude`, `openclaw configure`, `agy`) is reached without
      the test installing its own recording stub (a native flow must only ever run on an explicit operator choice);
    - `live_auth.agy_home` and `operator_rules.path` resolve under a scratch directory.
    Modules that do not exist yet in an older checkout are skipped, so this fixture never hides an import error.
    """
    root = tmp_path_factory.mktemp("round4-home")
    # The live Claude reader looks at these first; a developer's own shell must not decide a test.
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.delenv(name, raising=False)
    # `openclaw` is never run by a test: the module-level seam answers "not installed" unless the test installs its own
    # fake (every test that needs an answer already does).
    from ai_resources.setup.cockpits import openclaw as _oc_cockpit
    monkeypatch.setattr(_oc_cockpit, "_openclaw", lambda args, stdin=None, timeout=120: (127, "openclaw is not run by tests"))
    try:
        from ai_resources.setup import live_auth
    except ImportError:
        live_auth = None
    if live_auth is not None:
        def _fake_runner(argv):
            # The default host the suite pretends to be on: Claude Code on a subscription (the observed
            # `claude auth status` shape, tests/fixtures/claude_auth), nothing else installed.
            if list(argv[:3]) == ["claude", "auth", "status"]:
                return 0, ('{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", '
                           '"subscriptionType": "max"}')
            return 127, f"{argv[0]} is not run by tests"

        monkeypatch.setattr(live_auth, "default_runner", _fake_runner)

        def _no_native_flow(argv):
            raise AssertionError(f"a native flow was started by a test without a stub: {argv}")

        monkeypatch.setattr(live_auth, "handoff", _no_native_flow)
        monkeypatch.setattr(live_auth, "agy_home", lambda: root / ".gemini")
    try:
        from ai_resources.setup import operator_rules
    except ImportError:
        operator_rules = None
    if operator_rules is not None:
        monkeypatch.setattr(operator_rules, "path", lambda: root / "config" / "ai-resources" / "operator-rules.md")


@pytest.fixture(autouse=True)
def _agy_gate_waiver_off(request, monkeypatch):
    """The gate machinery (G0..G7, the evidence record, the held messages) is tested with the operator waiver off, so it
    keeps failing closed when the waiver is withdrawn. A test of the waiver itself is marked `agy_waiver` and sees the
    shipped `WAIVED_BY_OPERATOR` (ADR-0007 section 11, operator waiver of 2026-10-24)."""
    if request.node.get_closest_marker("agy_waiver"):
        return
    try:
        from ai_resources import agy_lane_gates
    except Exception:
        return
    monkeypatch.setattr(agy_lane_gates, "WAIVED_BY_OPERATOR", frozenset())


@pytest.fixture(autouse=True)
def _ui_auto_off():
    # Auto mode is a module global: a test that reaches the wizard's run-mode question without its `finally`
    # would leave it on, and every later prompt in that worker would take its default instead of asking.
    from ai_resources.setup import ui
    ui.set_auto(False)
    yield
    ui.set_auto(False)
