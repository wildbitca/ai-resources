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
