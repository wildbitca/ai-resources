"""The team-narration hook and its registration in ~/.claude/settings.json.

The hook runs inside Claude Code and publishes to Telegram through `openclaw message send`, so
every test here replaces `subprocess.Popen` with a recorder: nothing is ever sent. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
FIXTURES = REPO / "tests" / "fixtures" / "hook_payloads"
HOOK = REPO / "hooks" / "openclaw_team_progress.py"

from ai_resources.setup.cockpits import claude  # noqa: E402

CHAT = "-1001"


def _load_hook():
    spec = importlib.util.spec_from_file_location("openclaw_team_progress", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Popens:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))

    def sends(self) -> list[str]:
        """The --message argument of every `openclaw message send` call."""
        return [c[c.index("--message") + 1] for c in self.calls if "send" in c and "--message" in c]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A host with two agents (one workspace nested in the other), a topic map and no watcher."""
    root = tmp_path / "dev"
    (root / "pacha" / "api").mkdir(parents=True)
    (root / "elinvo").mkdir()
    cfg = {
        "agents": {"defaults": {"model": {"primary": "anthropic/haiku"}},
                   "entries": {
                       "main": {"workspace": str(root)},
                       "pacha": {"workspace": str(root / "pacha"),
                                 "model": {"primary": "claude-kit/sonnet-5"}},
                       "api": {"workspace": str(root / "pacha" / "api")},
                   }},
        "channels": {"telegram": {"groups": {CHAT: {"topics": {
            "1": {"agentId": "main"}, "8": {"agentId": "pacha"}, "9": {"agentId": "api"}}}}}},
    }
    cfgfile = tmp_path / "openclaw.json"
    cfgfile.write_text(json.dumps(cfg), encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "planner.md").write_text("---\nname: planner\nmodel: opus\n---\nbody\n", encoding="utf-8")

    hook = _load_hook()
    popen = _Popens()
    monkeypatch.setattr(hook, "OPENCLAW_JSON", str(cfgfile))
    monkeypatch.setattr(hook, "KIT_HOST_ENV", str(tmp_path / "kit-host.env"))
    monkeypatch.setattr(hook, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(hook, "LOG_PAYLOADS", str(tmp_path / "logs" / "team-hook.jsonl"))
    monkeypatch.setattr(hook, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "watcher_path", lambda: "")
    monkeypatch.setattr(hook, "openclaw_bin", lambda: "/bin/openclaw")
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    monkeypatch.setenv("OPENCLAW_CLI", "1")

    class Env:
        pass

    e = Env()
    e.hook, e.popen, e.root, e.tmp, e.cfg = hook, popen, root, tmp_path, cfgfile

    def run(name_or_payload, **over):
        if isinstance(name_or_payload, str):
            raw = (FIXTURES / f"{name_or_payload}.json").read_text(encoding="utf-8").replace("WS", str(root))
            payload = json.loads(raw)
        else:
            payload = dict(name_or_payload)
        payload.update(over)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        hook.main()

    def set_level(level):
        (tmp_path / "kit-host.env").write_text(f"OPENCLAW_NARRATION={level}\n", encoding="utf-8")

    e.run, e.set_level = run, set_level
    return e


# --- AC-2.2: gate ------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ["prompt_submit", "agent_start", "member_stop", "stop"])
def test_without_openclaw_cli_nothing_is_spawned(env, monkeypatch, payload):
    monkeypatch.delenv("OPENCLAW_CLI", raising=False)
    env.run(payload)
    assert env.popen.calls == []


def test_a_malformed_payload_exits_quietly(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json {"))
    env.hook.main()
    assert env.popen.calls == []
    assert capsys.readouterr().out == ""


# --- AC-2.3 / 2.4: where the message goes ------------------------------------------------------

def test_the_longest_workspace_prefix_picks_the_topic(env):
    env.run("prompt_submit")  # cwd = pacha/api: both `pacha` and `api` (and main) are prefixes
    call = env.popen.calls[0]
    assert call[call.index("--thread-id") + 1] == "9"
    assert call[call.index("--target") + 1] == CHAT


def test_a_workspace_without_its_own_topic_reports_in_the_main_topic(env):
    (env.root / "elinvo" / "x").mkdir()
    env.run("prompt_submit", cwd=str(env.root / "elinvo" / "x"))
    call = env.popen.calls[0]
    assert call[call.index("--thread-id") + 1] == "1"


def test_a_cwd_outside_every_workspace_is_silent(env):
    env.run("prompt_submit", cwd=str(env.tmp / "elsewhere"))
    assert env.popen.calls == []


def test_a_missing_config_is_silent(env):
    env.cfg.unlink()
    env.run("prompt_submit")
    assert env.popen.calls == []


# --- AC-2.5: nothing raw is ever published --------------------------------------------------------

@pytest.mark.parametrize("payload", ["prompt_submit", "agent_start", "member_edit", "member_bash",
                                     "parent_bash", "member_stop", "stop", "workflow"])
@pytest.mark.parametrize("level", ["milestones", "every-step"])
def test_no_secret_from_the_payload_is_ever_published(env, payload, level):
    env.set_level(level)
    env.run(payload)
    for text in env.popen.sends():
        assert "SECRET" not in text
    for call in env.popen.calls:
        assert not any("SECRET" in part for part in call)


def test_effort_arriving_as_an_object_prints_its_level_not_its_repr(env):
    env.run("prompt_submit")
    (text,) = env.popen.sends()
    assert "effort high" in text and "{" not in text


# --- detail level ----------------------------------------------------------------------------------

def test_milestones_publish_request_team_start_handoff_and_close(env):
    for name in ("prompt_submit", "agent_start", "member_stop", "stop"):
        env.run(name)
    assert len(env.popen.sends()) == 4


def test_milestones_stay_silent_about_edits_and_pulses(env):
    for _ in range(9):
        env.run("member_edit")
        env.run("member_bash")
    assert env.popen.calls == []


def test_every_step_narrates_edits_up_to_the_per_member_ceiling(env):
    env.set_level("every-step")
    for _ in range(9):
        env.run("member_edit")
    texts = env.popen.sends()
    assert len(texts) == env.hook.EDITS_PER_MEMBER + 1
    assert "keeps editing" in texts[-1]


def test_every_step_pulses_every_third_tool_when_there_is_no_watcher(env):
    env.set_level("every-step")
    for _ in range(6):
        env.run("member_bash")
    assert len(env.popen.sends()) == 6 // env.hook.PULSE_EVERY


def test_parent_tools_are_never_narrated(env):
    env.set_level("every-step")
    env.run("parent_bash")
    assert env.popen.calls == []


def test_an_unknown_detail_level_falls_back_to_milestones(env):
    env.set_level("chatty")
    assert env.hook.narration_level() == "milestones"


def test_a_session_stops_publishing_at_the_message_ceiling(env):
    for _ in range(env.hook.MESSAGES_PER_SESSION + 10):
        env.run("stop")
    assert len(env.popen.sends()) == env.hook.MESSAGES_PER_SESSION


def test_the_team_start_names_the_role_model_from_its_frontmatter(env):
    env.run("agent_start")
    (text,) = env.popen.sends()
    assert "planner" in text and "opus" in text


def test_a_role_without_a_model_reports_the_inherited_one(env):
    env.run("agent_start", tool_input={"subagent_type": "no-such-role", "description": "d"})
    (text,) = env.popen.sends()
    assert "inherited" in text and "sonnet-5" in text


# --- AC-2.6: the watcher contract ----------------------------------------------------------------------

def test_the_watcher_is_launched_with_the_documented_argv(env, monkeypatch):
    watcher = env.tmp / "openclaw-team-watch.py"
    watcher.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(env.hook, "watcher_path", lambda: str(watcher))
    env.set_level("every-step")
    env.run("member_bash")
    (spawn,) = [c for c in env.popen.calls if str(watcher) in c]
    assert spawn[0] == "python3"
    flags = dict(zip(spawn[2::2], spawn[3::2]))
    assert flags["--agent-id"] == "a1" and flags["--role"] == "implementer"
    assert flags["--transcript"] == "/tmp/proj/sess/subagents/agent-a1.jsonl"
    assert flags["--chat"] == CHAT and flags["--thread"] == "8"


def test_the_watcher_is_launched_once_per_member(env, monkeypatch):
    watcher = env.tmp / "openclaw-team-watch.py"
    watcher.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(env.hook, "watcher_path", lambda: str(watcher))
    env.set_level("every-step")
    for _ in range(4):
        env.run("member_bash")
    assert len([c for c in env.popen.calls if str(watcher) in c]) == 1


def test_milestones_never_launch_a_watcher(env, monkeypatch):
    watcher = env.tmp / "openclaw-team-watch.py"
    watcher.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(env.hook, "watcher_path", lambda: str(watcher))
    env.run("member_bash")
    assert env.popen.calls == []


def test_a_missing_watcher_does_not_stop_the_hook(env):
    env.set_level("every-step")
    for _ in range(3):
        env.run("member_bash")  # watcher_path() == "" in this fixture
    assert len(env.popen.sends()) == 1


def test_the_watcher_resolves_inside_the_kit_before_the_legacy_location(env, tmp_path, monkeypatch):
    hook = _load_hook()
    kit = tmp_path / "kit"
    (kit / "scripts" / "openclaw").mkdir(parents=True)
    (kit / "scripts" / "openclaw" / "openclaw-team-watch.py").write_text("#", encoding="utf-8")
    monkeypatch.setenv("AGENT_KIT", str(kit))
    assert hook.watcher_path() == str(kit / "scripts" / "openclaw" / "openclaw-team-watch.py")


# --- source hygiene ----------------------------------------------------------------------------------------

def test_the_hook_source_is_english_and_has_no_host_literals():
    text = HOOK.read_text(encoding="utf-8")
    assert not [c for c in text if c.isalpha() and ord(c) > 127], "non-ASCII letters (Spanish prose?)"
    for literal in ("/home/bitgandtter", "7961376547", "wildbit.dev"):
        assert literal not in text


# --- registration in settings.json (AC-2.1) ----------------------------------------------------------------

LEGACY = 'python3 "$HOME/.claude/hooks/openclaw-team-progress.py"'
KUBECTL = '/usr/bin/python3 "$HOME/.claude/hooks/kubectl-context-guard.py"'


def _legacy_settings() -> dict:
    def h(cmd, timeout):
        return {"type": "command", "command": cmd, "timeout": timeout}
    return {"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [h(KUBECTL, 10)]},
                       {"matcher": "*", "hooks": [h(LEGACY, 30)]}],
        "SubagentStop": [{"hooks": [h(LEGACY, 30)]}],
        "UserPromptSubmit": [{"hooks": [h(LEGACY, 20)]}],
        "Stop": [{"hooks": [h(LEGACY, 20)]}],
    }}


def _commands(settings: dict) -> dict[str, list[str]]:
    return {ev: [h["command"] for e in entries for h in e["hooks"]]
            for ev, entries in settings["hooks"].items()}


def test_install_replaces_the_legacy_registration_and_keeps_the_users_own(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_legacy_settings()), encoding="utf-8")
    assert claude.install_openclaw_hooks("/kit", team=True, guard=False, settings_path=path)
    cmds = _commands(json.loads(path.read_text(encoding="utf-8")))
    for event in ("PreToolUse", "SubagentStop", "UserPromptSubmit", "Stop"):
        kit = [c for c in cmds[event] if "/kit/hooks/openclaw_team_progress.py" in c]
        legacy = [c for c in cmds[event] if "openclaw-team-progress.py" in c]
        assert len(kit) == 1, event
        assert legacy == [], event
    assert KUBECTL in cmds["PreToolUse"]


def test_install_is_a_byte_level_no_op_the_second_time(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_legacy_settings()), encoding="utf-8")
    claude.install_openclaw_hooks("/kit", team=True, guard=False, settings_path=path)
    first = path.read_bytes()
    assert claude.install_openclaw_hooks("/kit", team=True, guard=False, settings_path=path) is False
    assert path.read_bytes() == first


def test_declining_the_team_hook_leaves_a_hand_installed_copy_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_legacy_settings()), encoding="utf-8")
    claude.install_openclaw_hooks("/kit", team=False, guard=False, settings_path=path)
    assert LEGACY in _commands(json.loads(path.read_text(encoding="utf-8")))["Stop"]


def test_remove_takes_out_exactly_what_install_added(tmp_path):
    path = tmp_path / "settings.json"
    original = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": KUBECTL, "timeout": 10}]}]}, "model": "opus"}
    path.write_text(json.dumps(original), encoding="utf-8")
    claude.install_openclaw_hooks("/kit", team=True, guard=False, settings_path=path)
    assert claude.remove_openclaw_hooks(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_the_always_on_kit_merge_does_not_touch_the_openclaw_hooks(tmp_path):
    path = tmp_path / "settings.json"
    claude.install_openclaw_hooks("/kit", team=True, guard=False, settings_path=path)
    before = path.read_bytes()
    claude._shared.merge_kit_hooks(path, {}, claude._is_kit_hook_command)
    assert path.read_bytes() == before
