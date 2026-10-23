"""The return block parser (ADR-0015 D3): tolerant of decoration, bounded, never executes anything."""
from __future__ import annotations

import pytest

from ai_resources.orchestrator import reply


def test_plain_and_decorated_fields_are_read():
    r = reply.parse("""Some prose.

**Status:** blocked
> Blocked: true
- **Return_to_step**: implement
Block_reason:   the suite is red   because of X
Verdict: REQUIRES_CHANGES
""")
    assert r.blocked and r.return_to == "implement" and r.reason == "the suite is red because of X"
    assert r.get("status") == "blocked" and r.verdict_word == "REQUIRES_CHANGES" and r.blocking_verdict


def test_keys_are_case_and_separator_insensitive_and_the_last_one_wins():
    r = reply.parse("blocked: true\nBLOCKED: false\nreturn-to-step: plan\nReturn to step: test\n")
    assert not r.blocked and r.return_to == "test"


@pytest.mark.parametrize("text,words", [("Verdict: PASS", {"PASS"}), ("Code_review_verdict: APPROVED", {"APPROVED"}),
                                        ("Security_audit: FAIL", {"FAIL"}), ("Status: READY", {"READY"})])
def test_verdict_words(text, words):
    assert reply.parse(text).words() == words


def test_signoff_and_blocking_are_told_apart():
    assert reply.parse("Verdict: READY").signoff and not reply.parse("Verdict: PASS").signoff
    assert reply.parse("Verdict: GAPS").blocking_verdict and not reply.parse("Verdict: PASS").blocking_verdict
    assert reply.parse("Architect_approval: no").blocking_verdict and not reply.parse("Architect_approval: yes").blocking_verdict


@pytest.mark.parametrize("junk", ["", None, 42, "no fields here at all", "::::", "Blocked", "x" * 500_000])
def test_hostile_or_empty_text_yields_no_fields_and_does_not_raise(junk):
    r = reply.parse(junk)
    assert not r.blocked and r.return_to == "" and r.verdict_word == ""


def test_values_are_cut_and_unknown_keys_ignored():
    r = reply.parse("Block_reason: " + "y" * 5000 + "\nRun: rm -rf /\nPlan_ref: a/b.md\n")
    assert len(r.reason) <= reply.MAX_VALUE and r.get("plan_ref") == "a/b.md" and "run" not in r.fields


def test_the_block_is_read_from_the_end_of_a_long_reply():
    body = "noise\n" * 100_000 + "Blocked: true\nReturn_to_step: plan\n"
    r = reply.parse(body)
    assert r.blocked and r.return_to == "plan"
