"""ADR-0011 (plan I2): the image and image-deny state fields are optional; a state saved before them loads unchanged."""
from __future__ import annotations

import pathlib

import yaml

from ai_resources.setup import state

FIX = pathlib.Path(__file__).parent / "fixtures" / "setup_state"
IMAGE_FIELDS = ("image_mode", "image_model", "image_account", "image_previous", "image_previous_absent",
                "image_applied", "image_auth_profile_created", "image_deny_added", "image_deny_key_absent")


def test_the_nine_fields_exist_with_their_defaults():
    o = state.OpenClawState()
    assert (o.image_mode, o.image_model, o.image_account, o.image_previous, o.image_previous_absent,
            o.image_applied, o.image_auth_profile_created, o.image_deny_added, o.image_deny_key_absent) == (
        "", "", "", None, False, False, "", False, False)
    assert set(IMAGE_FIELDS) == set(state.IMAGE_STATE_DEFAULTS)


def test_a_state_saved_before_the_image_fields_loads_with_their_defaults_and_no_other_key_changes():
    old = yaml.safe_load((FIX / "openclaw-pre-image.yaml").read_text())
    assert not any(k in old["openclaw"] for k in IMAGE_FIELDS)
    s = state._from_dict(state.SetupState, old)
    for key, default in state.IMAGE_STATE_DEFAULTS.items():
        assert getattr(s.openclaw, key) == default
    again = state._to_dict(s)
    assert again["openclaw"] == old["openclaw"] and again["cockpits"] == old["cockpits"]
    assert not any(k in again["openclaw"] for k in IMAGE_FIELDS), "additive: absent while at their defaults"


def test_non_default_values_round_trip_and_are_names_only():
    s = state.SetupState()
    s.openclaw.image_mode = "disable"
    s.openclaw.image_deny_added = True
    s.openclaw.image_deny_key_absent = True
    s.openclaw.image_previous = {"primary": "openai/gpt-image-2"}
    s.openclaw.image_account = "OPENAI_API_KEY"
    doc = state._to_dict(s)
    assert doc["openclaw"]["image_mode"] == "disable" and doc["openclaw"]["image_deny_added"] is True
    back = state._from_dict(state.SetupState, doc)
    assert back.openclaw.image_deny_key_absent is True and back.openclaw.image_previous == {"primary": "openai/gpt-image-2"}
    assert "image_model" not in doc["openclaw"], "default fields stay out of the file"


def test_the_pre_image_fixture_is_a_copy_of_a_real_old_state():
    assert (FIX / "openclaw-pre-image.yaml").read_text() == (FIX / "openclaw_host_voice.yaml").read_text()
