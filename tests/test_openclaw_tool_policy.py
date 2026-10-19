"""ADR-0011 (plan I3): the pure `image_generate` deny logic. No I/O: plain lists in, plain lists out."""
from __future__ import annotations

import pytest

from ai_resources import openclaw_tool_policy as tp

T = tp.IMAGE_TOOL


def test_constants():
    assert T == "image_generate" and tp.REPLACE_PATH == "tools.deny"


# --- check_shape ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["image_generate", {"a": 1}, [1], ["a", None], ["a", ["b"]], 5, True])
def test_check_shape_refuses_anything_but_absent_or_a_list_of_strings(bad):
    with pytest.raises(ValueError):
        tp.check_shape(bad)


def test_check_shape_accepts_absent_and_string_lists_and_copies():
    assert tp.check_shape(None) is None
    src = ["bundle-mcp", "browser"]
    out = tp.check_shape(src)
    assert out == src and out is not src and tp.check_shape([]) == []


# --- covering_token -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["image_generate", "IMAGE_GENERATE", "Image_Generate", "group:media", "GROUP:MEDIA",
                                   "group:openclaw", "*", "image*", "*image*", "*_generate", "image_*e", "i*"])
def test_cover_detects_exact_group_star_and_pinned_wildcards(entry):
    assert tp.covering_token(["bundle-mcp", entry, "browser"], T) == entry


@pytest.mark.parametrize("entry", ["bundle-mcp", "browser", "view_image", "group:fs", "group:web", "group:plugins",
                                   "image", "image?generate", "image_generat", "images*", "group:*", "music_generate",
                                   "image_generate2", ""])
def test_cover_does_not_trust_other_spellings(entry):
    assert tp.covering_token([entry], T) is None


def test_cover_returns_the_first_covering_entry_and_ignores_non_strings():
    assert tp.covering_token(["x", 3, "group:media", "image_generate"], T) == "group:media"
    assert tp.covering_token(None, T) is None and tp.covering_token([], T) is None


# --- deny_add -------------------------------------------------------------------------------------------------------

def test_add_appends_last_and_keeps_operator_entries_in_order_with_duplicates():
    live = ["bundle-mcp", "browser", "bundle-mcp"]
    assert tp.deny_add(live, T) == ["bundle-mcp", "browser", "bundle-mcp", "image_generate"]
    assert live == ["bundle-mcp", "browser", "bundle-mcp"], "the input is never mutated"


def test_add_over_an_absent_key_sends_the_token_alone():
    assert tp.deny_add(None, T) == ["image_generate"]
    assert tp.deny_add([], T) == ["image_generate"]


def test_add_is_no_change_when_an_entry_covers_and_idempotent_after_its_own_write():
    assert tp.deny_add(["group:media"], T) is tp.NO_CHANGE
    assert tp.deny_add(["x", "*"], T) is tp.NO_CHANGE
    after = tp.deny_add(["bundle-mcp"], T)
    assert tp.deny_add(after, T) is tp.NO_CHANGE


def test_add_appends_even_for_an_unpinned_wildcard_fail_safe():
    assert tp.deny_add(["image?generate"], T) == ["image?generate", "image_generate"]


def test_add_refuses_a_bad_shape():
    with pytest.raises(ValueError):
        tp.deny_add([1], T)
    with pytest.raises(ValueError):
        tp.deny_add("image_generate", T)


# --- deny_remove ----------------------------------------------------------------------------------------------------

def test_remove_takes_exactly_one_occurrence_and_keeps_the_rest_in_order():
    assert tp.deny_remove(["a", "image_generate", "b"], T, key_absent_before=False) == ["a", "b"]
    assert tp.deny_remove(["a", "image_generate", "b", "image_generate"], T, key_absent_before=False) == [
        "a", "image_generate", "b"], "the last one: the kit appends"


def test_remove_empty_result_is_null_only_when_the_key_was_absent_before():
    assert tp.deny_remove(["image_generate"], T, key_absent_before=True) is None
    assert tp.deny_remove(["image_generate"], T, key_absent_before=False) == []
    assert tp.deny_remove(["a", "image_generate"], T, key_absent_before=True) == ["a"]


@pytest.mark.parametrize("live", [None, [], ["a", "b"], ["IMAGE_GENERATE"], ["group:media"]])
def test_remove_is_no_change_when_the_exact_token_is_gone(live):
    assert tp.deny_remove(live, T, key_absent_before=False) is tp.NO_CHANGE


def test_remove_never_touches_operator_group_or_wildcard_entries():
    live = ["group:media", "image*", "bundle-mcp", "image_generate"]
    assert tp.deny_remove(live, T, key_absent_before=False) == ["group:media", "image*", "bundle-mcp"]


# --- assert_single_token_delta --------------------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    (["a"], ["a", "image_generate"]), (None, ["image_generate"]), ([], ["image_generate"]),
    (["a", "image_generate"], ["a"]), (["image_generate"], None), (["image_generate"], []),
    (["image_generate", "a"], ["a"]), (["a", "image_generate", "b"], ["a", "b"]),
])
def test_delta_accepts_one_token_added_or_removed(before, after):
    tp.assert_single_token_delta(before, after, T)


@pytest.mark.parametrize("before,after", [
    (["a"], ["image_generate"]),                       # an operator entry dropped
    (["a"], ["image_generate", "a"]),                  # token not appended last
    (["a"], ["a", "image_generate", "image_generate"]),
    (["a"], ["a", "b"]), (["a"], ["a"]), (["a", "b"], ["b", "a"]),
    (["a", "image_generate"], ["image_generate"]),     # removed the wrong entry
    (["a", "b"], ["a"]), (["a", "image_generate"], []), (None, None), (["a"], None),
    (["a", "image_generate"], ["a", "image_generate", "x"]),
    (["a"], ["a", "group:media"]),
])
def test_delta_raises_for_anything_else(before, after):
    with pytest.raises(ValueError):
        tp.assert_single_token_delta(before, after, T)
