"""ADR-0011 (plan I8, I9, I11): the answer mapping at Apply, the sign-in hand-off and the teardown of the model.

A fake `openclaw` (a scratch config plus a recorder) stands in for the CLI; nothing starts a process."""
from __future__ import annotations

import json

import pytest

from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_image as img
from ai_resources.setup.cockpits import openclaw
from openclaw_image_fakes import FakeOC

REF = "openrouter/google/gemini-3.1-flash-image-preview"
MEDIA = ("agents", "defaults", "mediaModels", "image")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    fake = FakeOC(cfg, {"gateway": {"port": 1}})
    monkeypatch.setattr(openclaw, "_openclaw", fake)
    monkeypatch.setattr(openclaw, "_before_first_write", None)
    fake.messages = {"warn": [], "info": [], "error": [], "ok": []}
    for level in fake.messages:
        monkeypatch.setattr(openclaw.ui, level, lambda m, *a, _l=level, **k: fake.messages[_l].append(str(m)))
    fake.confirms = []
    fake.answer = False

    def confirm(msg, default=True):
        fake.confirms.append((msg, default))
        return fake.answer
    monkeypatch.setattr(openclaw.ui, "confirm", confirm)
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: False)
    return fake


def S(mode="openrouter", model=REF, **kw) -> state.SetupState:
    s = state.SetupState()
    s.openclaw.image_mode = mode
    s.openclaw.image_model = model
    for k, v in kw.items():
        setattr(s.openclaw, k, v)
    return s


def run(s, fake, *, dry_run=False):
    written: list = []
    changed = openclaw._configure_image(s, openclaw.read_config(fake.cfg), fake.cfg, written, dry_run=dry_run)
    return changed, written


def media_of(fake):
    cur = fake.doc()
    for k in MEDIA:
        cur = cur.get(k) if isinstance(cur, dict) else None
    return cur


def deny_of(fake):
    return (fake.doc().get("tools") or {}).get("deny", "<absent>")


# --- keep / disable / cockpit gate -------------------------------------------------------------------------------------

def test_keep_and_a_never_answered_state_do_no_io(rig):
    for mode in ("keep", ""):
        s = S(mode=mode)
        assert run(s, rig) == (False, [])
    assert rig.calls == []


def test_cockpit_only_the_openclaw_configurator_calls_the_image_writer():
    import inspect
    from ai_resources.setup import wizard
    from ai_resources.setup.cockpits import claude
    assert "_configure_image" not in inspect.getsource(claude) and "image_generate" not in inspect.getsource(claude)
    assert "_configure_image(" in inspect.getsource(openclaw._configure)
    assert "image_mode" not in inspect.getsource(wizard._step9_apply)


def test_disable_applies_only_the_deny_and_never_patches_the_media_model(rig):
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    s = S(mode="disable", model="", image_applied=True)
    changed, _w = run(s, rig)
    assert changed and deny_of(rig) == ["bundle-mcp", "image_generate"]
    assert all("mediaModels" not in json.dumps(p) for _a, p in rig.patches())
    assert s.openclaw.image_applied is True, "a kit-applied model is left as it is"


def test_disable_with_a_covering_operator_entry_writes_nothing_and_says_so(rig):
    rig.write({"tools": {"deny": ["group:media"]}})
    changed, _w = run(S(mode="disable"), rig)
    assert not changed and rig.patches() == []
    assert any("already denied by your tools.deny entry 'group:media'" in m for m in rig.messages["info"])


# --- a route: the model write -----------------------------------------------------------------------------------------

def test_a_route_runs_a_dry_run_then_one_live_patch_and_records_the_state(rig):
    s = S()
    changed, written = run(s, rig)
    assert changed and written == [rig.cfg]
    dry, live = rig.patches(dry=True), rig.live_patches()
    assert len(dry) == 1 and len(live) == 1
    assert live[0][1] == {"agents": {"defaults": {"mediaModels": {"image": {"primary": REF, "timeoutMs": 180000}}}}}
    assert "--replace-path" in live[0][0] and "agents.defaults.mediaModels.image" in live[0][0]
    assert media_of(rig) == {"primary": REF, "timeoutMs": 180000}
    o = s.openclaw
    assert o.image_applied and o.image_model == REF and o.image_previous is None and o.image_previous_absent is True


def test_no_patch_carries_an_agents_entries_key_or_a_secret(rig):
    run(S(), rig)
    for _a, patch in rig.patches():
        assert set(patch) == {"agents"} and set(patch["agents"]) == {"defaults"}
        assert "entries" not in patch["agents"]
        assert "KEY" not in json.dumps(patch)


def test_dry_run_sends_only_dry_run_patches_and_changes_nothing(rig):
    rig.write({"tools": {"deny": ["a", "image_generate"]}})
    before = rig.cfg.read_bytes()
    s = S(image_deny_added=True)
    changed, _w = run(s, rig, dry_run=True)
    assert changed is False and rig.live_patches() == []
    assert rig.cfg.read_bytes() == before and s.openclaw.image_applied is False and s.openclaw.image_deny_added is True
    assert all("--dry-run" in a for a, _p in rig.patches())
    dry_disable = S(mode="disable")
    run(dry_disable, rig, dry_run=True)
    assert rig.live_patches() == [] and rig.cfg.read_bytes() == before
    assert all(a[:2] == ["config", "patch"] and "--dry-run" in a for a in rig.argvs())


def test_reject_a_rejected_dry_run_means_no_live_patch_and_one_error(rig):
    rig.reject_dry = True
    s = S()
    assert run(s, rig)[0] is False
    assert rig.live_patches() == [] and s.openclaw.image_applied is False and len(rig.messages["error"]) == 1


def test_reject_a_rejected_live_patch_records_nothing(rig):
    rig.reject_live = True
    s = S()
    assert run(s, rig)[0] is False and s.openclaw.image_applied is False and s.openclaw.image_previous_absent is False


def test_restart_unreadable_rules_or_a_restart_key_write_nothing_and_say_why(rig, monkeypatch):
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: "off")
    s = S()
    assert run(s, rig)[0] is False
    assert rig.live_patches() == [] and s.openclaw.image_applied is False
    assert any("needs a gateway restart" in m for m in rig.messages["warn"])
    assert s.openclaw.host_restart_pending == [] and s.openclaw.gateway_restart_pending == {}


def test_restart_unreadable_rules_leave_a_kit_deny_in_place(rig, monkeypatch):
    from reload_rules_fixture import make_rules_unavailable
    make_rules_unavailable(monkeypatch)
    rig.write({"tools": {"deny": ["image_generate"]}})
    s = S(image_deny_added=True)
    run(s, rig)
    assert rig.live_patches() == [] and deny_of(rig) == ["image_generate"] and s.openclaw.image_deny_added
    assert any("stays OFF" in m for m in rig.messages["warn"])


# --- operator values ---------------------------------------------------------------------------------------------------

def test_operator_set_model_is_kept_when_the_run_is_non_interactive(rig, monkeypatch):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": "fal/fal-ai/flux/dev"}}}}})
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: True)
    s = S()
    assert run(s, rig)[0] is False and rig.patches() == []
    assert media_of(rig) == {"primary": "fal/fal-ai/flux/dev"} and rig.confirms == []


def test_operator_set_model_is_kept_when_the_confirm_is_declined_and_it_defaults_to_no(rig):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": "fal/fal-ai/flux/dev"}}}})
    rig.answer = False
    s = S()
    assert run(s, rig)[0] is False and rig.live_patches() == []
    assert len(rig.confirms) == 1 and rig.confirms[0][1] is False
    assert media_of(rig) == "fal/fal-ai/flux/dev"


def test_operator_set_model_is_replaced_after_a_yes_and_remembered_for_teardown(rig):
    prev = {"primary": "fal/fal-ai/flux/dev", "fallbacks": ["google/gemini-3.1-flash-image"], "timeoutMs": 5}
    rig.write({"agents": {"defaults": {"mediaModels": {"image": prev}}}})
    rig.answer = True
    s = S()
    assert run(s, rig)[0] is True
    assert s.openclaw.image_previous == prev and s.openclaw.image_previous_absent is False
    assert media_of(rig)["primary"] == REF


def test_previous_holds_the_value_before_the_first_kit_write_across_two_runs(rig):
    prev = "fal/fal-ai/flux/dev"
    rig.write({"agents": {"defaults": {"mediaModels": {"image": prev}}}})
    rig.answer = True
    s = S()
    run(s, rig)
    s.openclaw.image_model = REF
    s.openclaw.image_mode = "openrouter"
    other = "openrouter/other/model"
    s.openclaw.image_model = other
    run(s, rig)                                   # the kit's own value is replaced without a second confirm
    assert s.openclaw.image_previous == prev and s.openclaw.image_previous_absent is False
    assert media_of(rig)["primary"] == other and len(rig.confirms) == 1


def test_an_invalid_or_missing_model_changes_nothing(rig):
    for model in ("", "not a ref", "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
        assert run(S(model=model), rig)[0] is False
    assert rig.patches() == [] and rig.messages["warn"]


def test_comfyui_without_a_workflow_is_refused_at_apply(rig):
    assert run(S(mode="comfyui", model="comfy/workflow"), rig)[0] is False and rig.patches() == []


# --- AC-D13: order and the kit deny ------------------------------------------------------------------------------------

def test_order_the_model_patch_goes_first_and_the_deny_removal_second(rig):
    rig.write({"tools": {"deny": ["bundle-mcp", "image_generate"]}})
    s = S(image_deny_added=True, image_deny_key_absent=False)
    assert run(s, rig)[0] is True
    live = rig.live_patches()
    assert len(live) == 2
    assert "mediaModels" in json.dumps(live[0][1]) and live[1][1] == {"tools": {"deny": ["bundle-mcp"]}}
    assert deny_of(rig) == ["bundle-mcp"] and s.openclaw.image_deny_added is False


def test_order_a_rejected_model_write_leaves_the_deny_in_place(rig):
    rig.write({"tools": {"deny": ["image_generate"]}})
    rig.reject_live = True
    s = S(image_deny_added=True)
    run(s, rig)
    assert deny_of(rig) == ["image_generate"] and s.openclaw.image_deny_added is True
    assert any("still present" in m for m in rig.messages["warn"])


def test_order_a_rejected_dry_run_leaves_the_deny_in_place(rig):
    rig.write({"tools": {"deny": ["image_generate"]}})
    rig.reject_dry = True
    s = S(image_deny_added=True)
    run(s, rig)
    assert rig.live_patches() == [] and s.openclaw.image_deny_added is True


def test_order_an_operator_covering_entry_is_kept_and_warned_about(rig):
    rig.write({"tools": {"deny": ["image*"]}})
    s = S()
    assert run(s, rig)[0] is True
    assert deny_of(rig) == ["image*"]
    assert any("still blocks image_generate; setup does not remove it" in m for m in rig.messages["warn"])


def test_order_with_no_kit_deny_the_deny_is_not_touched(rig):
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    run(S(), rig)
    assert deny_of(rig) == ["bundle-mcp"] and len(rig.live_patches()) == 1


def test_a_value_the_operator_changed_after_the_kit_applied_asks_again(rig):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": "fal/fal-ai/flux/dev", "timeoutMs": 5}}}}})
    s = S(image_applied=True)
    assert run(s, rig)[0] is False and len(rig.confirms) == 1 and rig.live_patches() == []


# --- I9 / AC-14, AC-15: the sign-in hand-off ----------------------------------------------------------------------------------

import subprocess

from ai_resources.setup import credentials as kit_credentials

MISSING = {}                                                # no provider has a credential in OpenClaw


class Auth:
    """Records `openclaw models auth ...` runs; stands in for `_image_auth_run`."""
    def __init__(self, rc=0):
        self.rc, self.argvs = rc, []

    def __call__(self, argv):
        self.argvs.append(list(argv))
        return self.rc


@pytest.fixture
def signin(rig, monkeypatch):
    auth = Auth()
    monkeypatch.setattr(openclaw, "_image_auth_run", auth)
    monkeypatch.setattr(openclaw, "_image_accounts", lambda: (MISSING, []))
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    rig.answer = True
    rig.auth = auth
    return rig


def test_account_a_missing_profile_and_a_confirmed_interactive_apply_make_exactly_one_auth_call(signin):
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    assert run(s, signin)[0] is True
    assert signin.auth.argvs == [["openclaw", "models", "auth", "paste-api-key", "--provider", "fal"]]
    assert s.openclaw.image_auth_profile_created == "fal:manual"
    assert signin.confirms and signin.confirms[0][1] is False
    assert len(signin.live_patches()) == 1, "the model is written after the sign-in"


def test_account_oauth_routes_use_login_and_claim_no_profile_id(signin):
    s = S(mode="openai-codex", model="openai/gpt-image-2")
    run(s, signin)
    assert signin.auth.argvs == [["openclaw", "models", "auth", "login", "--provider", "openai"]]
    assert s.openclaw.image_auth_profile_created == ""


def test_account_minimax_follows_the_chosen_method(signin):
    run(S(mode="minimax", model="minimax/image-01", image_account="oauth"), signin)
    run(S(mode="minimax", model="minimax/image-01", image_account="api-key"), signin)
    assert signin.auth.argvs == [["openclaw", "models", "auth", "login", "--provider", "minimax-portal"],
                                 ["openclaw", "models", "auth", "paste-api-key", "--provider", "minimax"]]


@pytest.mark.parametrize("why", ["dry_run", "yes", "no_tty", "declined", "present"])
def test_account_no_auth_call_runs_and_the_command_prints(signin, monkeypatch, why):
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    if why == "yes":
        monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: True)
    elif why == "no_tty":
        monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: False)
    elif why == "declined":
        signin.answer = False
    elif why == "present":
        monkeypatch.setattr(openclaw, "_image_accounts", lambda: ({"fal": {"kind": "profiles", "profiles": 1, "source": ""}}, []))
    run(s, signin, dry_run=(why == "dry_run"))
    assert signin.auth.argvs == []
    text = " ".join(signin.messages["info"])
    if why != "present":
        assert "openclaw models auth paste-api-key --provider fal" in text
    assert s.openclaw.image_auth_profile_created == ""


def test_account_a_failed_sign_in_writes_no_config_and_reports_only_the_exit_code(signin):
    signin.auth.rc = 3
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    assert run(s, signin)[0] is False
    assert signin.live_patches() == [] and s.openclaw.image_applied is False and s.openclaw.image_auth_profile_created == ""
    warn = " ".join(signin.messages["warn"])
    assert "exit 3" in warn


def test_account_foundry_prints_the_docs_pointer_and_runs_nothing(signin):
    run(S(mode="microsoft-foundry", model="microsoft-foundry/mai-image-2"), signin)
    assert signin.auth.argvs == [] and any("sign in through OpenClaw yourself" in m for m in signin.messages["info"])


def test_account_the_real_runner_inherits_the_terminal_and_pipes_nothing(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(openclaw.shutil, "which", lambda name: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw.subprocess, "run", fake_run)
    assert openclaw._image_auth_run(["openclaw", "models", "auth", "login", "--provider", "openai"]) == 0
    assert seen["argv"] == ["/usr/bin/openclaw", "models", "auth", "login", "--provider", "openai"]
    for forbidden in ("capture_output", "input", "stdin", "stdout", "stderr", "text", "env"):
        assert forbidden not in seen["kw"], forbidden
    monkeypatch.setattr(openclaw.shutil, "which", lambda name: None)
    assert openclaw._image_auth_run(["openclaw", "x"]) == 127


def test_account_no_secret_is_ever_stored_or_printed_and_the_kit_env_is_untouched(signin, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-SECRET")
    monkeypatch.setattr(kit_credentials, "load_env", lambda: {"FAL_KEY": "sk-test-SECRET"})
    monkeypatch.setattr(kit_credentials, "update_env", lambda *a, **k: pytest.fail("the kit .env was written"))
    monkeypatch.setattr(kit_credentials, "save_env", lambda *a, **k: pytest.fail("the kit .env was written"))
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    run(s, signin)
    blob = json.dumps(state._to_dict(s)) + json.dumps(signin.messages) + json.dumps(signin.calls)
    assert "sk-test-SECRET" not in blob


def test_account_the_image_modules_never_touch_the_kit_credential_store():
    """Plan I9: the wizard gains no image key entries. The credentials module does not know the image routes, and the image
    module (checked on its syntax tree, not its prose) imports no credential store and calls no env writer or password
    prompt."""
    import ast
    import pathlib
    root = pathlib.Path(openclaw.__file__).parents[1]
    assert "_openclaw_image" not in (root / "credentials.py").read_text()
    tree = ast.parse((root / "cockpits" / "_openclaw_image.py").read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not any("credentials" in name or "getpass" in name or "subprocess" in name for name in imported)
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not called & {"save_env", "update_env", "load_env", "password", "getpass", "environ", "getenv", "system", "Popen"}


# --- I11 / AC-18: teardown of the model and the sign-out offer ------------------------------------------------------------------

def _applied(rig, **kw):
    s = S(**kw)
    assert run(s, rig)[0] is True
    return s


def test_teardown_over_an_absent_key_sends_one_null_restore(rig):
    s = _applied(rig)
    n = len(rig.live_patches())
    assert openclaw._teardown_image(s) is True
    args, patch = rig.live_patches()[-1]
    assert len(rig.live_patches()) == n + 1
    assert patch == {"agents": {"defaults": {"mediaModels": {"image": None}}}} and "--replace-path" not in args
    assert media_of(rig) is None
    assert s.openclaw.image_applied is False and s.openclaw.image_previous_absent is False


def test_teardown_restores_a_previous_value_whole_with_a_replace_path(rig):
    prev = {"primary": "fal/fal-ai/flux/dev", "fallbacks": ["google/gemini-3.1-flash-image"], "timeoutMs": 5}
    rig.write({"agents": {"defaults": {"mediaModels": {"image": prev}}}})
    rig.answer = True
    s = _applied(rig)
    openclaw._teardown_image(s)
    args, patch = rig.live_patches()[-1]
    assert patch["agents"]["defaults"]["mediaModels"]["image"] == prev
    assert args[-2:] == ["--replace-path", "agents.defaults.mediaModels.image"]
    assert media_of(rig) == prev


def test_teardown_with_image_applied_false_runs_nothing(rig):
    s = S()
    assert openclaw._teardown_image(s) is True and rig.calls == []


def test_teardown_leaves_a_value_the_operator_changed_after_the_kit_applied_it(rig):
    s = _applied(rig)
    rig.write({"agents": {"defaults": {"mediaModels": {"image": "fal/fal-ai/flux/dev"}}}})
    n = len(rig.live_patches())
    assert openclaw._teardown_image(s) is True and len(rig.live_patches()) == n
    assert media_of(rig) == "fal/fal-ai/flux/dev" and s.openclaw.image_applied is False


def test_teardown_that_would_need_a_restart_keeps_its_fields_for_a_retry(rig, monkeypatch):
    s = _applied(rig)
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: "off")
    n = len(rig.live_patches())
    assert openclaw._teardown_image(s) is False and len(rig.live_patches()) == n
    assert s.openclaw.image_applied is True and s.openclaw.image_previous_absent is True


def test_teardown_logout_only_for_a_created_profile_and_only_on_confirm(signin):
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    run(s, signin)
    assert s.openclaw.image_auth_profile_created == "fal:manual"
    signin.answer = False
    signin.auth.argvs.clear()
    signin.confirms.clear()
    openclaw._teardown_image(s)
    assert signin.auth.argvs == [] and len(signin.confirms) == 1 and signin.confirms[0][1] is False
    assert any("openclaw models auth logout fal:manual" in m for m in signin.messages["info"])
    assert s.openclaw.image_auth_profile_created == ""


def test_teardown_logout_runs_after_a_yes_with_the_profile_id_only(signin):
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    run(s, signin)
    signin.auth.argvs.clear()
    signin.answer = True
    openclaw._teardown_image(s)
    assert signin.auth.argvs == [["openclaw", "models", "auth", "logout", "fal:manual"]]


def test_teardown_no_logout_is_offered_when_setup_signed_nothing_in(signin, monkeypatch):
    monkeypatch.setattr(openclaw, "_image_accounts",
                        lambda: ({"openrouter": {"kind": "profiles", "profiles": 1, "source": ""}}, []))
    s = S()
    run(s, signin)
    assert s.openclaw.image_auth_profile_created == ""
    signin.confirms.clear()
    openclaw._teardown_image(s)
    assert signin.confirms == [] and signin.auth.argvs == []


def test_teardown_non_interactive_never_signs_out(signin, monkeypatch):
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    run(s, signin)
    signin.auth.argvs.clear()
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: True)
    openclaw._teardown_image(s)
    assert signin.auth.argvs == []


def test_teardown_a_second_teardown_is_a_no_op(rig):
    s = _applied(rig)
    openclaw._teardown_image(s)
    n = len(rig.calls)
    assert openclaw._teardown_image(s) is True and len(rig.calls) == n


def test_the_openclaw_teardown_undoes_the_model_then_the_deny_in_that_order(rig):
    rig.write({"tools": {"deny": ["bundle-mcp", "image_generate"]}})
    s = S(image_deny_added=True)
    run(s, rig)                                                  # route applied; the kit deny comes out
    assert deny_of(rig) == ["bundle-mcp"]
    s2 = S(mode="disable", image_deny_added=False)
    run(s2, rig)
    assert s2.openclaw.image_deny_added is True
    s2.openclaw.image_applied, s2.openclaw.image_previous_absent, s2.openclaw.image_model = True, True, REF
    openclaw.teardown(s2)
    assert deny_of(rig) == ["bundle-mcp"] and media_of(rig) is None
    assert s2.openclaw.image_applied is False and s2.openclaw.image_deny_added is False
    kinds = ["media" if "mediaModels" in json.dumps(p) else "deny" for _a, p in rig.live_patches()[-2:]]
    assert kinds == ["media", "deny"]
