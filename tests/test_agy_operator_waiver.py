"""The operator waiver of 2026-10-24 (ADR-0007 section 11): G0..G7 no longer gate admission, the acknowledgement does."""
from __future__ import annotations

import pytest

from ai_resources import agy_lane_gates as g
from ai_resources.setup import lanes_render, team_view
from ai_resources.setup.cockpits import openclaw_lanes

pytestmark = pytest.mark.agy_waiver

ON = {"antigravity": True}


def _ack(roles=("code-reviewer", "tester")):
    return {"google": lanes_render.make_ack("google", list(roles), "2026-10-24T00:00:00+00:00",
                                            account="operator@example.com")}


def test_every_gate_is_waived_and_none_is_claimed_proven():
    assert g.WAIVED_BY_OPERATOR == {x.id for x in g.GATES}
    assert g.all_proven() and g.refusal() is None and g.unproven() == []
    assert all(row["status"] != g.PROVEN for row in g.status_table().values())


def test_the_kit_owned_agy_home_stays_off_so_agy_keeps_the_real_login():
    assert not g.gate_proven("G5")
    assert not openclaw_lanes.agy_home_wanted()


def test_a_valid_acknowledgement_listing_the_role_admits_the_lane():
    assert lanes_render.agy_role_reason("code-reviewer", ON, _ack()) is None
    assert openclaw_lanes.AGY_BACKEND in openclaw_lanes.writable_backends()


def test_without_a_valid_acknowledgement_the_lane_is_still_held():
    assert lanes_render.agy_role_reason("code-reviewer", ON, None) == lanes_render.HELD_NO_ACK_REASON
    stale = _ack()
    stale["google"]["text_sha256"] = "0" * 64
    assert lanes_render.agy_role_reason("code-reviewer", ON, stale) == lanes_render.HELD_NO_ACK_REASON


def test_an_unlisted_role_and_the_flag_off_are_still_held():
    assert lanes_render.agy_role_reason("implementer", ON, _ack()).startswith("held: the Google acknowledgement")
    assert lanes_render.agy_role_reason("code-reviewer", {}, _ack()) == lanes_render.HELD_FLAG_REASON


def test_the_summary_says_waived_and_the_next_step_is_the_signature():
    assert "waived by the operator" in g.summary_line()
    assert g.summary()["on"] and g.summary()["proven"] == []
    assert "sign the Google acknowledgement" in team_view.next_action(
        gates_on=True, record_status="absent", agy_ticked=True, missing_roles=[], acked=False)
    assert team_view.next_action(gates_on=True, record_status="absent", agy_ticked=True, missing_roles=[]) == ""
