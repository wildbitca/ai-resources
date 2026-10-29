"""A bare legacy `mode` of `lite` (no scores) means LITE/inline in the scorecard CLI and in both workflow scripts.

Before 2.9.0 the Python helper read it as LITE while `/kit-plan` and `/kit-implement` read it as light, because their
resolver only knew the script names. The single table is `size_flags` in skills/kit-orchestration/scorecard.json: `lite`
is the same spelling as `--lite`, i.e. inline. Every other spelling resolves exactly as it did.
"""
from __future__ import annotations

import shutil

import pytest

import sizing_snapshot as snap
from ai_resources import scorecard

# Integration: touches or fakes an external tool (openclaw, node, git, installers...); skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

# bare mode -> size, with no scores: the same answer in Python and in both scripts
EXPECTED = {"inline": "inline", "lite": "inline", "light": "light", "standard": "light", "full": "full", "bogus": "light", "": "light"}


@pytest.mark.parametrize("spelling", ["inline", "lite", "light", "standard", "full"])
def test_python_and_size_flags_agree(spelling):
    rules = scorecard.load_rules()
    assert scorecard.score_or_full(None, spelling)["mode"] == scorecard.mode_of_spelling(spelling)
    assert rules["sizes"][scorecard.mode_of_spelling(spelling)] == rules["size_flags"][spelling] == EXPECTED[spelling]


def test_python_unknown_spelling_is_full_not_light():
    # unchanged: the Python helper has no failed classifier to default to light
    assert scorecard.score_or_full(None, "bogus")["mode"] == "full"


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("mode", sorted(EXPECTED))
def test_kit_plan_resolves_a_bare_mode_like_python(mode):
    out = snap.run_js(snap.PLAN_JS, {"goal": "g"}, {**snap.PLAN_RESPONSES, "classify": {"mode": mode, "reason": "r"}})
    assert out["result"]["size"] == EXPECTED[mode]


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("mode", sorted(EXPECTED))
def test_kit_implement_resolves_a_bare_mode_like_python(mode):
    # no risk items, so an inline size is not bumped to light
    out = snap.run_js(snap.IMPL_JS, {"goal": "g", "plan_path": "p.md"}, {**snap.IMPL_RESPONSES, "read-sizing": {"found": True, "mode": mode}})
    assert out["result"]["size"] == EXPECTED[mode]


def test_scripts_embed_the_generated_lookup():
    inc = (snap.REPO / "workflows" / "scripts" / "sizing-core.js.inc").read_text(encoding="utf-8")
    assert "Object.hasOwn(SCORECARD.size_flags, answer.mode)" in inc
    for script in (snap.PLAN_JS, snap.IMPL_JS):
        assert "Object.hasOwn(SCORECARD.size_flags, answer.mode)" in script.read_text(encoding="utf-8")
