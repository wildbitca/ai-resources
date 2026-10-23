"""ADR-0011 (plan I8, I9, I11): the answer mapping at Apply, the sign-in hand-off and the teardown of the model.

A fake `openclaw` (a scratch config plus a recorder) stands in for the CLI; nothing starts a process."""
from __future__ import annotations

import json

import pytest

from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_image as img
from ai_resources.setup.cockpits import openclaw
from openclaw_image_fakes import FakeOC

REAL_IMAGE_ACCOUNTS = openclaw._image_accounts            # captured before conftest stubs it for every test
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
    # ADR-0016 D1: an image route is written only for a provider OpenClaw reports usable. The default rig has a sign-in
    # for the routes the tests use; the tests about a missing one replace this.
    fake.accounts = {"openrouter": {"kind": "profiles", "profiles": 1, "source": ""},
                     "openai": {"kind": "profiles", "profiles": 1, "source": ""},
                     "minimax": {"kind": "profiles", "profiles": 1, "source": ""}}
    monkeypatch.setattr(openclaw, "_image_accounts", lambda: (fake.accounts, []))
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


@pytest.fixture
def tty_yes(rig, monkeypatch):
    """An interactive run on a terminal where the operator says yes."""
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    rig.answer = True
    return rig


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


def test_disable_applies_only_the_deny_and_never_patches_the_media_model(rig, tty_yes):
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    s = S(mode="disable", model="", image_applied=True)
    changed, _w = run(s, rig)
    assert changed and deny_of(rig) == ["bundle-mcp", "image_generate"]
    assert all("mediaModels" not in json.dumps(p) for _a, p in rig.patches())
    assert s.openclaw.image_applied is True, "a kit-applied model is left as it is"
    assert len(rig.confirms) == 1 and rig.confirms[0][1] is False, "the deny add has its own default-No confirm"


def test_disable_with_a_covering_operator_entry_writes_nothing_and_says_so(rig, tty_yes):
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


# --- ADR-0016 D1: no kit sign-in; a route is written only for a provider OpenClaw reports usable ------------------------------

from ai_resources.setup import credentials as kit_credentials

MISSING = {}                                                # no provider has a credential in OpenClaw


def _no_auth_calls(fake):
    return [a for a, _s in fake.calls if a[:2] == ["models", "auth"] and a[2:3] != ["list"]]


def test_a_route_without_a_usable_sign_in_writes_nothing_and_prints_openclaws_own_flow(rig, monkeypatch):
    monkeypatch.setattr(openclaw, "_image_accounts", lambda: (MISSING, []))
    s = S(mode="fal", model="fal/krea/v2/medium/text-to-image")
    changed, written = run(s, rig)
    assert changed is False and written == [] and rig.live_patches() == []
    assert any("openclaw configure --section model" in m for m in rig.messages["warn"])
    assert s.openclaw.image_applied is False
    assert _no_auth_calls(rig) == [] and rig.confirms == []


def test_no_run_ever_calls_a_models_auth_command_or_a_native_flow(rig, monkeypatch):
    from ai_resources.setup import live_auth
    monkeypatch.setattr(live_auth, "handoff", lambda argv: pytest.fail(f"a native flow ran: {argv}"))
    for accounts in (MISSING, rig.accounts):
        monkeypatch.setattr(openclaw, "_image_accounts", lambda a=accounts: (a, []))
        run(S(), rig)
        run(S(), rig, dry_run=True)
    assert _no_auth_calls(rig) == []


def test_a_dry_run_names_the_missing_sign_in_and_still_shows_the_patch_verdict(rig, monkeypatch):
    monkeypatch.setattr(openclaw, "_image_accounts", lambda: (MISSING, []))
    changed, _w = run(S(), rig, dry_run=True)
    assert changed is False and rig.live_patches() == []
    assert any("openclaw configure --section model" in m for m in rig.messages["warn"])
    lines = img.dry_run_lines(S(), {"accounts": MISSING, "kit_env": []})
    assert any("not written" in line and "openclaw configure --section model" in line for line in lines)
    assert not any("paste-api-key" in line or "would run" in line for line in lines)


def test_a_usable_sign_in_writes_the_route_as_before(rig):
    s = S()
    changed, _w = run(s, rig)
    assert changed and s.openclaw.image_applied
    assert not any("openclaw configure" in m for m in rig.messages["warn"])


def test_the_image_catalog_has_no_sign_in_fields_or_helpers():
    assert not any(hasattr(r, "login_provider") or hasattr(r, "key_provider") for r in img.CATALOG)
    for gone in ("ensure_account", "sign_in_for", "SignInResult", "SIGN_IN_TIMEOUT_S"):
        assert not hasattr(img, gone), gone
    assert not hasattr(openclaw, "_image_auth_run") and not hasattr(openclaw, "_auth_profile_ids")


def test_teardown_of_a_profile_an_earlier_kit_signed_in_only_prints_the_logout(rig, monkeypatch):
    from ai_resources.setup import live_auth
    monkeypatch.setattr(live_auth, "handoff", lambda argv: pytest.fail("teardown ran a native flow"))
    s = S(image_auth_profile_created="fal:manual")
    openclaw._teardown_image(s)
    assert s.openclaw.image_auth_profile_created == ""
    assert any("openclaw models auth logout fal:manual" in m for m in rig.messages["info"])
    assert rig.confirms == [] and _no_auth_calls(rig) == []


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


# --- I11 / AC-18: teardown of the model ------------------------------------------------------------------

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


def test_teardown_a_second_teardown_is_a_no_op(rig):
    s = _applied(rig)
    openclaw._teardown_image(s)
    n = len(rig.calls)
    assert openclaw._teardown_image(s) is True and len(rig.calls) == n


def test_the_openclaw_teardown_undoes_the_model_then_the_deny_in_that_order(rig, monkeypatch):
    rig.write({"tools": {"deny": ["bundle-mcp", "image_generate"]}})
    s = S(image_deny_added=True)
    run(s, rig)                                                  # route applied; the kit deny comes out
    assert deny_of(rig) == ["bundle-mcp"]
    s2 = S(mode="disable", image_deny_added=False)
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)       # the deny add needs a terminal and a yes
    rig.answer = True
    run(s2, rig)
    assert s2.openclaw.image_deny_added is True
    s2.openclaw.image_applied, s2.openclaw.image_previous_absent, s2.openclaw.image_model = True, True, REF
    openclaw.teardown(s2)
    assert deny_of(rig) == ["bundle-mcp"] and media_of(rig) is None
    assert s2.openclaw.image_applied is False and s2.openclaw.image_deny_added is False
    kinds = ["media" if "mediaModels" in json.dumps(p) else "deny" for _a, p in rig.live_patches()[-2:]]
    assert kinds == ["media", "deny"]


FAL = dict(mode="fal", model="fal/krea/v2/medium/text-to-image")


# --- review M2: the global tools.deny add needs an interactive terminal and a yes ----------------------------------------

def _disable_writes_nothing(rig, s=None):
    s = s or S(mode="disable", model="")
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    before = rig.cfg.read_bytes()
    changed, _w = run(s, rig)
    assert changed is False and rig.patches() == [] and rig.cfg.read_bytes() == before
    assert s.openclaw.image_deny_added is False
    return s


def test_m2_non_interactive_and_yes_never_write_the_deny(rig, monkeypatch):
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: True)
    rig.answer = True
    _disable_writes_nothing(rig)
    assert rig.confirms == [] and any("NOT disabled" in m for m in rig.messages["warn"])


def test_m2_yes_flag_with_a_tty_never_writes_the_deny(rig, monkeypatch):
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(openclaw.ui, "_ASSUME_YES", True)
    rig.answer = True
    _disable_writes_nothing(rig)
    assert rig.confirms == [] and any("NOT disabled" in m for m in rig.messages["warn"])


def test_m2_without_a_tty_the_deny_is_not_written_even_if_a_confirm_would_say_yes(rig, monkeypatch):
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: False)
    rig.answer = True
    _disable_writes_nothing(rig)
    assert rig.confirms == [] and any("NOT disabled" in m for m in rig.messages["warn"])


def test_m2_a_declined_confirm_writes_nothing(rig, monkeypatch):
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    rig.answer = False
    _disable_writes_nothing(rig)
    assert len(rig.confirms) == 1 and rig.confirms[0][1] is False


def test_m2_a_saved_disable_is_reapplied_only_interactively_with_its_confirm(rig, monkeypatch):
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    s = S(mode="disable", model="")                              # the saved answer from an earlier run
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    rig.answer = True
    assert run(s, rig)[0] is True and deny_of(rig) == ["bundle-mcp", "image_generate"]
    assert len(rig.confirms) == 1 and rig.confirms[0][1] is False


def test_m2_a_covering_entry_or_a_dry_run_asks_nothing(rig, tty_yes):
    rig.write({"tools": {"deny": ["group:media"]}})
    run(S(mode="disable", model=""), rig)
    run(S(mode="disable", model=""), rig, dry_run=True)
    assert rig.confirms == [] and rig.patches(dry=False) == []


def test_m2_the_token_the_operator_took_out_is_not_added_back_and_tracking_stops(rig, tty_yes):
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    s = S(mode="disable", model="", image_deny_added=True, image_deny_key_absent=True)
    changed, _w = run(s, rig)
    assert changed is False and rig.patches() == [] and deny_of(rig) == ["bundle-mcp"]
    assert s.openclaw.image_deny_added is False and s.openclaw.image_deny_key_absent is False
    assert rig.confirms == [] and any("your choice" in m for m in rig.messages["info"])


def test_m2_the_operators_removal_is_honoured_even_with_a_covering_entry_and_a_dry_run_keeps_state(rig, tty_yes):
    rig.write({"tools": {"deny": ["group:media"]}})
    s = S(mode="disable", model="", image_deny_added=True)
    run(s, rig, dry_run=True)
    assert s.openclaw.image_deny_added is True, "a dry run changes no state"
    run(s, rig)
    assert s.openclaw.image_deny_added is False and rig.patches(dry=False) == [] and deny_of(rig) == ["group:media"]


# --- review L2: the live value is the kit's own only if it names the ref the kit wrote -----------------------------------

def test_l2_a_kit_shaped_value_with_another_ref_is_the_operators_and_asks_default_no(rig):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": "fal/fal-ai/flux/dev", "timeoutMs": 180000}}}}})
    s = S(image_applied=True, image_applied_ref=REF)
    assert run(s, rig)[0] is False and rig.live_patches() == []
    assert len(rig.confirms) == 1 and rig.confirms[0][1] is False


def test_l2_the_kits_own_ref_is_replaced_without_a_confirm_and_an_old_state_falls_back_to_image_model(rig):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": REF, "timeoutMs": 180000}}}}})
    new = "openrouter/other/model"
    assert run(S(model=new, image_applied=True, image_applied_ref=REF), rig)[0] is True
    assert rig.confirms == []
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": REF, "timeoutMs": 180000}}}}})
    assert run(S(model=REF, image_applied=True), rig)[0] is True and rig.confirms == []     # no applied ref saved


def test_l2_after_a_confirmed_replacement_of_the_operators_value_teardown_restores_that_value(rig):
    rig.write({"agents": {"defaults": {"mediaModels": {"image": {"primary": REF, "timeoutMs": 180000}}}}})
    mine = {"primary": "fal/fal-ai/flux/dev", "timeoutMs": 180000}
    rig.write({"agents": {"defaults": {"mediaModels": {"image": mine}}}})
    rig.answer = True
    s = S(image_applied=True, image_applied_ref=REF, image_previous=None, image_previous_absent=True)
    assert run(s, rig)[0] is True
    assert s.openclaw.image_previous == mine and s.openclaw.image_previous_absent is False


@pytest.mark.parametrize("live", [{"primary": "fal/fal-ai/flux/dev", "timeoutMs": 180000}, "fal/fal-ai/flux/dev", None])
def test_l2_teardown_leaves_a_value_that_is_not_the_kits_written_ref_untouched(rig, live):
    doc = {"agents": {"defaults": {"mediaModels": {"image": live}}}} if live is not None else {}
    rig.write(doc)
    s = S(image_applied=True, image_applied_ref=REF, image_previous={"primary": "old/x"}, image_previous_absent=False)
    assert openclaw._teardown_image(s) is True
    assert rig.live_patches() == [] and rig.doc() == doc
    assert s.openclaw.image_applied is False and s.openclaw.image_applied_ref == ""
    assert any("changed after setup applied it" in m for m in rig.messages["warn"])




# --- review L5: only the NAMES of the kit .env are kept, never a value -----------------------------------------------------------

LEAK = "sk-LEAK-0123456789-SHOULD-NEVER-APPEAR"


def test_l5_no_kit_env_value_or_credential_field_survives_in_any_state_or_output(rig, monkeypatch):
    status = {"auth": {"providers": [{"provider": "fal", "effective": {"kind": "none", "apiKey": LEAK},
                                      "profiles": {"count": 0, "token": LEAK}, "env": {"source": "", "value": LEAK}}]},
              "apiKey": LEAK}
    listing = {"profiles": [{"id": "openrouter:default", "provider": "openrouter", "token": LEAK, "key": LEAK}]}
    rig.extra[("models", "status")] = lambda a, s: (0, json.dumps(status))
    rig.extra[("models", "auth", "list")] = lambda a, s: (0, json.dumps(listing))
    monkeypatch.setattr(kit_credentials, "load_env", lambda: {"FAL_KEY": LEAK, "EMPTY_ONE": "", "OPENAI_API_KEY": LEAK})
    accounts, names = REAL_IMAGE_ACCOUNTS()
    assert names == ["FAL_KEY", "OPENAI_API_KEY"], "names only, sorted, empty values dropped"
    ctx = img.make_ctx({}, accounts, names, deny_hot=True)
    blob = json.dumps(accounts) + json.dumps(names) + json.dumps(ctx, default=str)
    for rid, route in img.ROUTES.items():
        if route.kind != "route":
            continue
        s = S(mode=rid, model=route.models[0] if route.models else "", image_account=route.auth[0] if route.auth else "")
        rows = img.review_rows(s, ctx)
        blob += json.dumps(rows) + json.dumps(img.dry_run_lines(s, ctx)) + json.dumps(state._to_dict(s))
    setattr(s, img.CTX_ATTR, ctx)
    blob += json.dumps(state._to_dict(s))
    assert LEAK not in blob and "apiKey" not in blob and "token" not in blob
    assert "FAL_KEY" in blob, "the variable NAME is what Review may show"



# --- end to end: `setup --yes` with a saved disable reaches the deny path and refuses -----------------------------------

def test_yes_through_wizard_run_with_a_saved_disable_refuses_without_writing(rig, monkeypatch):
    import argparse

    from ai_resources.setup import wizard
    monkeypatch.setattr(openclaw.ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(openclaw.ui, "is_non_interactive", lambda: False)
    rig.answer = True
    rig.write({"tools": {"deny": ["bundle-mcp"]}})
    before = rig.cfg.read_bytes()
    s = S(mode="disable", model="")                              # the saved answer from an earlier run
    result = {}

    def body(_args):
        result["changed"], _w = run(s, rig)
        return 0
    monkeypatch.setattr(wizard, "_run", body)
    args = argparse.Namespace(yes=True, dry_run=False, lanes_dry_run=False)
    assert wizard.run(args) == 0
    assert openclaw.ui.is_assume_yes() is True, "the flag reached the gate through wizard.run, not a test setattr"
    assert result["changed"] is False and rig.patches() == [] and rig.cfg.read_bytes() == before
    assert rig.confirms == [] and s.openclaw.image_deny_added is False
    assert any("NOT disabled" in m for m in rig.messages["warn"])
