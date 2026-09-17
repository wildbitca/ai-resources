"""_agy_quota.py: parsing agy's two weekly pools and grading the applied one.

Run with: pytest tests/test_agy_quota.py -q
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup.cockpits import _agy_quota as q  # noqa: E402

FIXTURE = (REPO / "tests" / "fixtures" / "agy_usage.txt").read_text()


def test_parse_usage_reads_the_captured_fixture():
    pools = q.parse_usage(FIXTURE)
    assert len(pools) == 2
    gemini, claude_gpt = pools
    assert gemini.name == q.POOL_GEMINI
    assert gemini.remaining_pct == 0
    assert gemini.resets_at == "2026-09-24T15:45:01Z"
    assert claude_gpt.name == q.POOL_CLAUDE_GPT
    # Remaining, not consumed: a healthy pool must not come back as near-zero.
    assert claude_gpt.remaining_pct is not None
    assert claude_gpt.remaining_pct > 50
    assert claude_gpt.resets_at == "2026-09-24T21:01:59Z"


def test_parse_usage_empty_string():
    assert q.parse_usage("") == []


def test_parse_usage_garbage():
    assert q.parse_usage("not a usage table at all\n\n???") == []


def test_parse_usage_row_missing_timestamp():
    pools = q.parse_usage("Gemini Models\tWeekly Limit Remaining\t42%")
    assert len(pools) == 1
    assert pools[0].remaining_pct == 42
    assert pools[0].resets_at == ""


def test_parse_usage_unknown_pool_label_is_reported_verbatim_not_dropped():
    pools = q.parse_usage("Some New Pool\tWeekly Limit Remaining\t55%\t2027-01-01T00:00:00Z")
    assert len(pools) == 1
    assert pools[0].name == "Some New Pool"
    assert pools[0].remaining_pct == 55


def test_parse_usage_non_numeric_percent():
    pools = q.parse_usage("Gemini Models\tWeekly Limit Remaining\tN/A\t2027-01-01T00:00:00Z")
    assert len(pools) == 1
    assert pools[0].remaining_pct is None


def test_parse_usage_ignores_blank_lines_and_log_noise():
    text = "\n[info] refreshing token\nGemini Models\tWeekly Limit Remaining\t10%\t2027-01-01T00:00:00Z\n\n"
    pools = q.parse_usage(text)
    assert len(pools) == 1
    assert pools[0].remaining_pct == 10


def test_read_usage_missing_binary():
    def run(args):
        return 127, "agy: command not found"
    pools, reason = q.read_usage(run=run)
    assert pools == []
    assert reason


def test_read_usage_timeout():
    def run(args):
        return 124, "timed out"
    pools, reason = q.read_usage(run=run)
    assert pools == []
    assert "timed out" in reason
    assert "124" in reason


def test_read_usage_success_uses_injected_runner_no_subprocess_mocking():
    def run(args):
        assert args[-2:] == ["-p", "/usage"]
        return 0, FIXTURE
    pools, reason = q.read_usage(run=run)
    assert reason == ""
    assert len(pools) == 2


def test_read_usage_unparsable_output():
    def run(args):
        return 0, "nothing usable here"
    pools, reason = q.read_usage(run=run)
    assert pools == []
    assert reason


def test_read_usage_never_raises_on_runner_exception():
    def run(args):
        raise RuntimeError("boom")
    pools, reason = q.read_usage(run=run)
    assert pools == []
    assert "boom" in reason


def test_pool_for_model_covers_every_id_in_the_s2_tuple():
    cases = {
        "gemini-3.8-flash-low": q.POOL_GEMINI,
        "gemini-3.8-flash-high": q.POOL_GEMINI,
        "gemini-3.1-pro-low": q.POOL_GEMINI,
        "claude-sonnet-4-6": q.POOL_CLAUDE_GPT,
        "claude-opus-4-6-thinking": q.POOL_CLAUDE_GPT,
        "gpt-oss-120b-medium": q.POOL_CLAUDE_GPT,
    }
    for model_id, expected_pool in cases.items():
        assert q.pool_for_model(model_id) == expected_pool, model_id


def test_pool_for_model_unknown_prefix_returns_empty_string():
    assert q.pool_for_model("some-other-vendor-model") == ""
    assert q.pool_for_model("") == ""


def test_report_error_when_applied_pool_is_exhausted():
    pools = q.parse_usage(FIXTURE)
    lines = q.report(pools, applied_model="gemini-3.8-flash-low")
    severities = dict((msg.split(":")[0], sev) for sev, msg in lines)
    assert severities[q.POOL_GEMINI] == "error"
    assert severities[q.POOL_CLAUDE_GPT] == "ok"


def test_report_warn_near_limit_on_applied_pool():
    pools = [q.Pool(q.POOL_GEMINI, 5, "2027-01-01T00:00:00Z")]
    lines = q.report(pools, applied_model="gemini-3.8-flash-low", low_pct=10)
    assert lines == [("warn", "Gemini Models: 5% remaining (resets 2027-01-01T00:00:00Z)")]


def test_report_ok_when_applied_pool_is_healthy():
    pools = [q.Pool(q.POOL_GEMINI, 50, "2027-01-01T00:00:00Z")]
    lines = q.report(pools, applied_model="gemini-3.8-flash-low", low_pct=10)
    assert lines[0][0] == "ok"


def test_report_not_my_pool_never_raises_severity_even_at_zero():
    pools = q.parse_usage(FIXTURE)  # Gemini at 0%
    lines = q.report(pools, applied_model="claude-sonnet-4-6")
    severities = dict((msg.split(":")[0], sev) for sev, msg in lines)
    # The applied model is on the Claude/GPT pool; Gemini being at 0% must not warn.
    assert severities[q.POOL_GEMINI] == "ok"
    assert severities[q.POOL_CLAUDE_GPT] == "ok"


def test_report_with_no_applied_model_never_raises_severity():
    pools = q.parse_usage(FIXTURE)
    lines = q.report(pools)
    assert all(sev == "ok" for sev, _ in lines)
