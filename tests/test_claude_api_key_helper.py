"""ADR-0016 D1 (AC-6, AC-7, AC-11): apiKeyHelper is the only kit extra for Claude Code's sign-in.

The kit writes a command or `cat <file>` into settings.json, refuses a key file that is not 0600, never reads or stores the
key, and never claims "API key" without a key or helper being live.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from ai_resources.setup import cockpit_auth, live_auth, profiles, state, ui
from ai_resources.setup.cockpits import _shared, claude
from wizard_fakes import Script

SENTINEL = "sk-ant-api03-SENTINEL-NEVER-READ-OR-STORED"
REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    for attr, sub in (("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"), ("AGENTS_DIR", "agents"),
                      ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, root / sub)
    monkeypatch.setattr(claude, "CONFIG_ROOT", root)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_trim_engram_hooks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    root.mkdir()
    return root


def keyfile(tmp_path, mode):
    p = tmp_path / "anthropic.key"
    p.write_text(SENTINEL + "\n", encoding="utf-8")
    p.chmod(mode)
    return p


def apply(s):
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})


def settings(root):
    return json.loads((root / "settings.json").read_text(encoding="utf-8"))


def state_with_helper(value):
    s = state.SetupState()
    s.profile.name = "balanced"
    cockpit_auth.set_method(s, "claude", "api_key")
    cockpit_auth.set_helper(s, "claude", value)
    setattr(s, claude.HELPER_OK_ATTR, True)
    return s


# --- AC-7: the file mode ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_a_key_file_with_group_or_other_access_is_refused(tmp_path, mode):
    p = keyfile(tmp_path, mode)
    setting, problem = claude.helper_setting(str(p))
    assert setting == "" and "0600" in problem and str(p) in problem


def test_a_0400_key_file_is_refused_because_only_0600_is_accepted(tmp_path):
    setting, problem = claude.helper_setting(str(keyfile(tmp_path, 0o400)))
    assert setting == "" and "0600" in problem


def test_a_0600_key_file_becomes_a_cat_command_and_the_key_is_never_read(tmp_path, monkeypatch):
    p = keyfile(tmp_path, 0o600)
    reads = []
    real_open = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, *a, **k: reads.append(str(self)) or real_open(self, *a, **k))
    setting, problem = claude.helper_setting(str(p))
    assert problem == "" and setting == f"cat {p}"
    assert str(p) not in reads
    assert SENTINEL not in setting


def test_a_private_script_is_used_as_is(tmp_path):
    p = tmp_path / "helper.sh"
    p.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    p.chmod(0o700)
    assert claude.helper_setting(str(p)) == (str(p), "")
    p.chmod(0o755)
    assert claude.helper_setting(str(p))[0] == ""


def test_a_command_is_kept_and_a_missing_path_or_a_pasted_key_is_refused(tmp_path):
    assert claude.helper_setting("pass show anthropic/api") == ("pass show anthropic/api", "")
    assert "does not exist" in claude.helper_setting(str(tmp_path / "nope"))[1]
    assert claude.helper_setting(SENTINEL)[0] == "" and "looks like a key" in claude.helper_setting(SENTINEL)[1]
    assert claude.helper_setting("   ")[1] == "nothing was entered"
    with pytest.raises(ValueError):
        cockpit_auth.set_helper(state.SetupState(), "claude", SENTINEL)


def test_a_directory_is_not_a_helper(tmp_path):
    assert "regular file" in claude.helper_setting(str(tmp_path))[1]


# --- the command forms: arguments, quoting, file references, symlinks --------------------------------------------------

def program(tmp_path, name="get-key", mode=0o700):
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    p.chmod(mode)
    return p


def test_an_absolute_path_command_keeps_its_arguments(tmp_path):
    exe = program(tmp_path)
    setting, problem = claude.helper_setting(f"{exe} --vault team")
    assert problem == "" and setting == f"{exe} --vault team"


def test_a_tilde_command_with_arguments_is_expanded_and_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    program(tmp_path)
    setting, problem = claude.helper_setting("~/get-key arg")
    assert problem == "" and setting == f"{tmp_path}/get-key arg"


def test_the_executable_path_is_shell_quoted(tmp_path):
    exe = program(tmp_path, "get key; id")
    setting, problem = claude.helper_setting(f"'{exe}' --x")
    assert problem == ""
    import shlex
    assert shlex.split(setting) == [str(exe), "--x"]          # one program, not `get` then `key` then `id`


def test_a_program_other_users_can_replace_is_refused(tmp_path):
    exe = program(tmp_path, mode=0o722)
    assert "not writable by others" in claude.helper_setting(f"{exe} a")[1]
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    inner = program(shared)
    assert "can be changed by other users" in claude.helper_setting(f"{inner} a")[1]


def test_a_cat_command_gets_the_same_owner_and_mode_check_as_a_bare_path(tmp_path):
    ok = keyfile(tmp_path, 0o600)
    assert claude.helper_setting(f"cat {ok}") == (f"cat {ok}", "")
    bad = keyfile(tmp_path, 0o644)
    setting, problem = claude.helper_setting(f"cat {bad}")
    assert setting == "" and "0600" in problem


def test_a_file_argument_of_any_command_is_checked(tmp_path):
    exe = program(tmp_path)
    bad = keyfile(tmp_path, 0o640)
    assert "0600" in claude.helper_setting(f"{exe} --file {bad}")[1]
    ok = keyfile(tmp_path, 0o600)
    assert claude.helper_setting(f"{exe} --file {ok}")[1] == ""
    assert "does not exist" in claude.helper_setting(f"{exe} --file {tmp_path}/missing")[1]


def test_what_cannot_be_checked_is_refused_rather_than_stored(tmp_path):
    for text in ("cat ~/missing-key", "cat key", "cat ./key", "./get-key", "cat", f"cat {tmp_path}/k | tr a b"):
        setting, problem = claude.helper_setting(text)
        assert setting == "" and problem, text


def test_a_symlink_is_resolved_and_its_target_is_what_is_checked(tmp_path):
    target = keyfile(tmp_path, 0o644)
    link = tmp_path / "link.key"
    link.symlink_to(target)
    assert "0600" in claude.helper_setting(str(link))[1]
    assert "0600" in claude.helper_setting(f"cat {link}")[1]
    target.chmod(0o600)
    assert claude.helper_setting(f"cat {link}")[1] == ""


# --- AC-7: settings.json gets only the path or command, idempotently, and the state holds no key -------------------------

def test_apply_writes_only_the_command_idempotently_and_no_key_reaches_state_or_settings(home, tmp_path):
    p = keyfile(tmp_path, 0o600)
    setting, _ = claude.helper_setting(str(p))
    s = state_with_helper(setting)
    apply(s)
    first = (home / "settings.json").read_bytes()
    assert settings(home)["apiKeyHelper"] == f"cat {p}"
    assert SENTINEL not in first.decode("utf-8")
    apply(s)
    assert (home / "settings.json").read_bytes() == first                       # idempotent
    dumped = json.dumps(state._to_dict(s))
    assert SENTINEL not in dumped and cockpit_auth.secret_findings(s.cockpit_auth) == []


def test_apply_refuses_a_helper_whose_file_became_readable_by_others(home, tmp_path):
    p = keyfile(tmp_path, 0o600)
    s = state_with_helper(f"cat {p}")
    p.chmod(0o644)
    s2 = state_with_helper(str(p))                       # the stored value is the path form
    apply(s2)
    assert "apiKeyHelper" not in settings(home)


def test_an_existing_different_helper_is_kept_unless_chosen_in_this_run(home):
    (home / "settings.json").write_text(json.dumps({"apiKeyHelper": "my-own-helper"}), encoding="utf-8")
    s = state_with_helper("pass show anthropic")
    delattr(s, claude.HELPER_OK_ATTR)
    apply(s)
    assert settings(home)["apiKeyHelper"] == "my-own-helper"
    setattr(s, claude.HELPER_OK_ATTR, True)
    apply(s)
    assert settings(home)["apiKeyHelper"] == "pass show anthropic"


def test_a_saved_helper_is_not_re_applied_from_saved_state(home, tmp_path):
    """ADR-0016: only a helper the operator chose in this run reaches settings.json; a saved one never turns API-key
    billing back on by itself."""
    p = keyfile(tmp_path, 0o600)
    s = state_with_helper(f"cat {p}")
    delattr(s, claude.HELPER_OK_ATTR)
    apply(s)
    assert "apiKeyHelper" not in settings(home)


def test_apply_checks_the_cat_form_again(home, tmp_path):
    p = keyfile(tmp_path, 0o600)
    s = state_with_helper(f"cat {p}")
    p.chmod(0o644)
    apply(s)
    assert "apiKeyHelper" not in settings(home)


def test_choosing_another_sign_in_forgets_the_saved_helper(monkeypatch, tmp_path):
    p = keyfile(tmp_path, 0o600)
    s = state_with_helper(f"cat {p}")
    run_ask(monkeypatch, s, {"Claude Code sign-in": "change", "How should Claude Code sign in": "login"})
    assert cockpit_auth.helper(s, "claude") == "" and not getattr(s, claude.HELPER_OK_ATTR, False)
    assert "helper" not in s.cockpit_auth["claude"]


def test_a_helper_that_is_not_the_live_sign_in_is_forgotten_on_keep(monkeypatch, tmp_path):
    p = keyfile(tmp_path, 0o600)
    s = state.SetupState()
    cockpit_auth.set_helper(s, "claude", f"cat {p}")             # saved earlier; settings.json no longer has it
    run_ask(monkeypatch, s, {"Claude Code sign-in": "keep"})
    assert cockpit_auth.helper(s, "claude") == ""


def test_no_helper_means_no_apikeyhelper_key(home):
    s = state.SetupState()
    s.profile.name = "balanced"
    apply(s)
    assert "apiKeyHelper" not in settings(home)


# --- the ask: AC-6 (never a false API key) and AC-11 (handover only on an explicit choice) -------------------------------

def run_ask(monkeypatch, s, answers, *, non_interactive=False, dry_run=False, status=None):
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    if status is not None:
        monkeypatch.setattr(live_auth, "default_runner", lambda argv: status)
    script = Script(**answers)
    script.install(monkeypatch, non_interactive=non_interactive)
    rc = claude.ask_auth(ui, s, None, dry_run)
    return rc, script, handed


def test_picking_the_helper_with_nothing_usable_records_the_live_kind_never_api_key(monkeypatch, tmp_path):
    """AC-6: no ANTHROPIC_API_KEY, no helper: whatever the operator answers, Review cannot say API key."""
    s = state.SetupState()
    p = keyfile(tmp_path, 0o644)                          # refused: nothing becomes live
    rc, script, handed = run_ask(monkeypatch, s, {"Claude Code sign-in": "change",
                                                  "How should Claude Code sign in": "api_key_helper",
                                                  "apiKeyHelper": str(p)})
    assert rc == 0 and handed == []
    assert cockpit_auth.live_kind(s, "claude") == "subscription" and cockpit_auth.method(s, "claude") == "subscription"
    assert cockpit_auth.helper(s, "claude") == ""
    assert any("apiKeyHelper not set" in m for m in script.shown)


def test_a_valid_helper_is_recorded_as_a_path_or_command_only(monkeypatch, tmp_path):
    s = state.SetupState()
    p = keyfile(tmp_path, 0o600)
    rc, _script, handed = run_ask(monkeypatch, s, {"Claude Code sign-in": "change",
                                                   "How should Claude Code sign in": "api_key_helper",
                                                   "apiKeyHelper": str(p)})
    assert rc == 0 and handed == []
    assert cockpit_auth.helper(s, "claude") == f"cat {p}"
    assert cockpit_auth.live_kind(s, "claude") == "api_key_helper" and cockpit_auth.method(s, "claude") == "api_key"
    assert SENTINEL not in json.dumps(state._to_dict(s))


def test_the_native_login_runs_only_on_an_explicit_change(monkeypatch):
    s = state.SetupState()
    rc, _script, handed = run_ask(monkeypatch, s, {"Claude Code sign-in": "keep"})
    assert rc == 0 and handed == []
    s2 = state.SetupState()
    rc, _script, handed = run_ask(monkeypatch, s2, {"Claude Code sign-in": "change",
                                                    "How should Claude Code sign in": "login"})
    assert rc == 0 and handed == [["claude", "auth", "login"]]


def test_no_native_flow_in_a_non_interactive_run_or_a_dry_run(monkeypatch):
    s = state.SetupState()
    rc, script, handed = run_ask(monkeypatch, s, {}, non_interactive=True)
    assert rc == 0 and handed == [] and not [m for m in script.asked if m.startswith("select")]
    assert cockpit_auth.live_kind(s, "claude") == "subscription"
    s2 = state.SetupState()
    rc, script, handed = run_ask(monkeypatch, s2, {"Claude Code sign-in": "change",
                                                   "How should Claude Code sign in": "login"}, dry_run=True)
    assert rc == 0 and handed == [] and any("Dry run" in m for m in script.shown)


def test_the_live_kind_is_re_read_after_the_handover(monkeypatch):
    s = state.SetupState()
    calls = {"n": 0}

    def runner(argv):
        calls["n"] += 1
        # before the login Claude Code is signed out; after it, a subscription
        return (0, '{"loggedIn": false}') if calls["n"] == 1 else \
            (0, '{"loggedIn": true, "authMethod": "claude.ai"}')
    monkeypatch.setattr(live_auth, "default_runner", runner)
    monkeypatch.setattr(live_auth, "handoff", lambda argv: 0)
    Script(**{"Claude Code sign-in": "change", "How should Claude Code sign in": "login"}).install(monkeypatch)
    assert claude.ask_auth(ui, s, None, False) == 0
    assert cockpit_auth.live_kind(s, "claude") == "subscription"


def test_a_signed_out_claude_is_shown_as_not_signed_in_not_as_a_key(monkeypatch):
    s = state.SetupState()
    rc, script, _ = run_ask(monkeypatch, s, {"Claude Code sign-in": "keep"}, status=(0, '{"loggedIn": false}'))
    assert rc == 0 and cockpit_auth.live_kind(s, "claude") == "none"
    assert any("not signed in" in m for m in script.shown) and not any("API key" in m for m in script.shown)
