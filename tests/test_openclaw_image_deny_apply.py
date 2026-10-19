"""ADR-0011 (plan I4): the `image_generate` deny writer and its reversal, driven by state, not by the prompt.

The runner is a fake that logs argv and stdin; the config is a scratch file. Nothing starts a process."""
from __future__ import annotations

import json

import pytest

from ai_resources import openclaw_reload_rules as rr
from ai_resources.setup import state
from ai_resources.setup.cockpits import openclaw
from openclaw_image_fakes import FakeOC

TOOL = "image_generate"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    fake = FakeOC(cfg, {"gateway": {"port": 1}})
    monkeypatch.setattr(openclaw, "_openclaw", fake)
    monkeypatch.setattr(openclaw, "_before_first_write", None)
    warnings: list[str] = []
    monkeypatch.setattr(openclaw.ui, "warn", lambda m, *a, **k: warnings.append(str(m)))
    fake.warnings = warnings
    return fake


def S(**kw) -> state.SetupState:
    s = state.SetupState()
    for k, v in kw.items():
        setattr(s.openclaw, k, v)
    return s


def deny_of(fake):
    return (fake.doc().get("tools") or {}).get("deny", "<absent>")


# --- merge (AC-D2) ----------------------------------------------------------------------------------------------------

def test_merge_keeps_operator_entries_in_order_and_sends_one_dry_run_then_one_live_patch(rig):
    rig.write({"tools": {"deny": ["bundle-mcp", "browser"]}})
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "applied" and res.changed
    live = rig.live_patches()
    assert len(live) == 1 and len(rig.patches(dry=True)) == 1
    args, patch = live[0]
    assert patch == {"tools": {"deny": ["bundle-mcp", "browser", "image_generate"]}}
    assert args == ["config", "patch", "--stdin", "--replace-path", "tools.deny"]
    assert rig.argvs().index(rig.patches(dry=True)[0][0]) < rig.argvs().index(args)
    assert deny_of(rig) == ["bundle-mcp", "browser", "image_generate"]
    assert s.openclaw.image_deny_added is True and s.openclaw.image_deny_key_absent is False


def test_merge_over_an_absent_key_records_the_key_was_absent(rig):
    s = S()
    assert openclaw._apply_image_deny(s).status == "applied"
    assert deny_of(rig) == [TOOL]
    assert s.openclaw.image_deny_added is True and s.openclaw.image_deny_key_absent is True


def test_duplicates_and_odd_operator_entries_are_resent_byte_for_byte(rig):
    live = ["bundle-mcp", "bundle-mcp", "Browser", "outlook__*", "group:fs"]
    rig.write({"tools": {"deny": live}})
    openclaw._apply_image_deny(S())
    assert deny_of(rig) == live + [TOOL]


def test_no_patch_ever_carries_an_agents_key_or_a_forbidden_token(rig):
    rig.write({"tools": {"deny": ["x"]}})
    openclaw._apply_image_deny(S())
    for _a, patch in rig.patches():
        assert set(patch) == {"tools"} and set(patch["tools"]) == {"deny"}
        assert "group:media" not in patch["tools"]["deny"] and "*" not in patch["tools"]["deny"]


# --- covering entries (AC-D3) ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["image_generate", "Image_Generate", "*", "group:media", "group:openclaw", "image*", "*image*"])
def test_cover_a_covering_entry_means_zero_patch_calls_and_no_state(rig, entry):
    rig.write({"tools": {"deny": ["bundle-mcp", entry]}})
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "covered" and res.detail == entry and not res.changed
    assert rig.patches() == [] and s.openclaw.image_deny_added is False


def test_cover_an_unpinned_wildcard_is_not_trusted_so_the_token_is_appended(rig):
    rig.write({"tools": {"deny": ["image?generate"]}})
    assert openclaw._apply_image_deny(S()).status == "applied"
    assert deny_of(rig) == ["image?generate", TOOL]


# --- idempotence (AC-D4) ----------------------------------------------------------------------------------------------

def test_idempotent_a_kit_deny_already_applied_sends_nothing(rig):
    rig.write({"tools": {"deny": ["a", TOOL]}})
    s = S(image_deny_added=True)
    res = openclaw._apply_image_deny(s)
    assert res.status == "already-applied" and rig.patches() == []


def test_idempotent_two_runs_write_once(rig):
    s = S()
    openclaw._apply_image_deny(s)
    openclaw._apply_image_deny(s)
    assert len(rig.live_patches()) == 1


# --- shape (AC-D5) ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["image_generate", {"a": 1}, ["a", 1], ["a", None], 5])
def test_shape_a_live_value_that_is_not_a_list_of_strings_writes_nothing(rig, bad):
    rig.write({"tools": {"deny": bad}})
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "bad-shape" and rig.patches() == []
    assert s.openclaw.image_deny_added is False and len(rig.warnings) == 1


def test_shape_a_tools_block_that_is_not_an_object_writes_nothing(rig):
    rig.write({"tools": "full"})
    assert openclaw._apply_image_deny(S()).status == "bad-shape" and rig.patches() == []


def test_an_unreadable_config_writes_nothing(rig):
    rig.cfg.write_text("{ not json", encoding="utf-8")
    s = S()
    assert openclaw._apply_image_deny(s).status == "unreadable" and rig.patches() == []
    rig.cfg.unlink()
    assert openclaw._apply_image_deny(s).status == "unreadable" and rig.patches() == []
    assert s.openclaw.image_deny_added is False


# --- rejected dry-run (AC-D6) -------------------------------------------------------------------------------------------

def test_reject_a_rejected_dry_run_means_no_live_patch_and_no_state(rig):
    rig.write({"tools": {"deny": ["a"]}})
    rig.reject_dry = True
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "rejected" and rig.live_patches() == [] and s.openclaw.image_deny_added is False
    assert deny_of(rig) == ["a"] and rig.warnings


def test_reject_a_rejected_live_patch_records_nothing(rig):
    rig.reject_live = True
    s = S()
    assert openclaw._apply_image_deny(s).status == "failed"
    assert s.openclaw.image_deny_added is False and deny_of(rig) == "<absent>"


# --- dry-run flag ------------------------------------------------------------------------------------------------------

def test_dry_run_sends_only_a_dry_run_patch_and_changes_nothing(rig, tmp_path):
    rig.write({"tools": {"deny": ["a"]}})
    before = rig.cfg.read_bytes()
    s = S()
    res = openclaw._apply_image_deny(s, dry_run=True)
    assert res.status == "dry-run" and res.before == ("a",) and res.after == ("a", TOOL)
    assert rig.live_patches() == [] and len(rig.patches(dry=True)) == 1
    assert rig.cfg.read_bytes() == before and s.openclaw.image_deny_added is False


# --- race (AC-D7) -----------------------------------------------------------------------------------------------------

def test_race_a_list_that_changes_after_the_dry_run_aborts_the_write(rig):
    rig.write({"tools": {"deny": ["a"]}})
    rig.on_dry_run = lambda: rig.write({"tools": {"deny": ["a", "operator-added"]}})
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "race" and rig.live_patches() == []
    assert deny_of(rig) == ["a", "operator-added"] and s.openclaw.image_deny_added is False


# --- hot only (AC-D8) -------------------------------------------------------------------------------------------------

def test_not_hot_reload_mode_off_writes_nothing_records_nothing_and_says_so(rig, monkeypatch):
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: "off")
    s = S()
    res = openclaw._apply_image_deny(s)
    assert res.status == "not-hot" and rig.live_patches() == []
    assert s.openclaw.image_deny_added is False and s.openclaw.host_restart_pending == []
    assert s.openclaw.gateway_restart_pending == {}
    assert any(openclaw.DENY_NOT_HOT in w for w in rig.warnings)
    assert "restart" not in " ".join(" ".join(a) for a in rig.argvs() if a[:1] != ["config"]).lower()


def test_not_hot_unreadable_rules_write_nothing(rig, monkeypatch):
    from reload_rules_fixture import make_rules_unavailable
    make_rules_unavailable(monkeypatch)
    s = S()
    assert openclaw._apply_image_deny(s).status == "not-hot" and rig.live_patches() == []
    assert s.openclaw.image_deny_added is False


def test_not_hot_a_restart_class_tools_deny_writes_nothing(rig, monkeypatch):
    monkeypatch.setattr(openclaw, "_lanes_classify", lambda patch: ["tools.deny"])
    assert openclaw._apply_image_deny(S()).status == "not-hot" and rig.live_patches() == []


def test_not_hot_removal_keeps_the_flags_and_says_so(rig, monkeypatch):
    rig.write({"tools": {"deny": ["a", TOOL]}})
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: "off")
    s = S(image_deny_added=True)
    res = openclaw._remove_image_deny(s)
    assert res.status == "not-hot" and rig.live_patches() == [] and s.openclaw.image_deny_added is True
    assert any(openclaw.DENY_REMOVAL_NOT_HOT in w for w in rig.warnings)


# --- teardown (AC-D9) --------------------------------------------------------------------------------------------------

def test_teardown_an_absent_key_gets_null(rig):
    s = S()
    openclaw._apply_image_deny(s)
    assert deny_of(rig) == [TOOL]
    res = openclaw._remove_image_deny(s)
    assert res.status == "removed"
    args, patch = rig.live_patches()[-1]
    assert patch == {"tools": {"deny": None}} and "--replace-path" not in args
    assert deny_of(rig) == "<absent>"
    assert s.openclaw.image_deny_added is False and s.openclaw.image_deny_key_absent is False


def test_teardown_an_operator_list_loses_only_the_kit_token_in_order(rig):
    live = ["bundle-mcp", "browser", "bundle-mcp"]
    rig.write({"tools": {"deny": live}})
    s = S()
    openclaw._apply_image_deny(s)
    openclaw._remove_image_deny(s)
    args, patch = rig.live_patches()[-1]
    assert patch == {"tools": {"deny": live}} and "--replace-path" in args
    assert deny_of(rig) == live and s.openclaw.image_deny_added is False


def test_teardown_the_key_existed_empty_before_is_restored_as_an_empty_list(rig):
    rig.write({"tools": {"deny": []}})
    s = S()
    openclaw._apply_image_deny(s)
    assert s.openclaw.image_deny_key_absent is False
    openclaw._remove_image_deny(s)
    assert deny_of(rig) == []


def test_teardown_with_the_flag_false_runs_nothing(rig):
    rig.write({"tools": {"deny": [TOOL]}})                  # an operator entry the kit never added
    res = openclaw._remove_image_deny(S())
    assert res.status == "none" and rig.calls == [] and deny_of(rig) == [TOOL]


def test_teardown_a_token_the_operator_already_removed_writes_nothing_and_clears_the_flags(rig):
    rig.write({"tools": {"deny": ["a"]}})
    s = S(image_deny_added=True, image_deny_key_absent=True)
    res = openclaw._remove_image_deny(s)
    assert res.status == "gone" and rig.live_patches() == []
    assert s.openclaw.image_deny_added is False and s.openclaw.image_deny_key_absent is False


def test_teardown_a_second_teardown_is_a_no_op(rig):
    s = S()
    openclaw._apply_image_deny(s)
    openclaw._remove_image_deny(s)
    n = len(rig.calls)
    assert openclaw._remove_image_deny(s).status == "none" and len(rig.calls) == n


def test_teardown_never_removes_an_operator_group_or_wildcard_entry(rig):
    rig.write({"tools": {"deny": ["group:media", "image*", TOOL]}})
    s = S(image_deny_added=True)           # the kit's token sits last; group:media / image* are the operator's
    openclaw._remove_image_deny(s)
    assert deny_of(rig) == ["group:media", "image*"]


def test_openclaw_teardown_calls_the_removal_and_keeps_the_flags_when_it_cannot_land(rig, monkeypatch):
    rig.write({"tools": {"deny": ["a", TOOL]}})
    s = S(image_deny_added=True)
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: "off")
    openclaw.teardown(s)
    assert s.openclaw.image_deny_added is True and deny_of(rig) == ["a", TOOL]
    monkeypatch.setattr(openclaw, "_live_reload_mode", lambda: None)
    openclaw.teardown(s)
    assert s.openclaw.image_deny_added is False and deny_of(rig) == ["a"]


# --- the writer is the only way in: a tampered patch is stopped by the guard -----------------------------------------

def test_a_delta_check_failure_blocks_before_any_subprocess(rig, monkeypatch):
    rig.write({"tools": {"deny": ["a"]}})
    monkeypatch.setattr(openclaw.openclaw_tool_policy, "deny_add", lambda live, tool: ["image_generate"])   # drops "a"
    res = openclaw._apply_image_deny(S())
    assert res.status == "bad-shape" and rig.patches() == []
