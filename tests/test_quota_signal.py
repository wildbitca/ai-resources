"""The provider quota-block signal: StopFailure payload -> sidecar file -> headroom reading.

Payload shape from the Claude Code hooks reference (StopFailure): `error` (rate_limit, overloaded, billing_error, ...),
optional `error_details`, optional `last_assistant_message`. There is no structured reset time in it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ai_resources.scoring import headroom as hr
from ai_resources.scoring import headroom_sources as hs
from ai_resources.scoring import quota_signal as qs

REPO = Path(__file__).resolve().parents[1]
NOW = 1_790_000_000
DOC_PAYLOAD = {"session_id": "abc123", "transcript_path": "/x/y.jsonl", "cwd": "/x", "hook_event_name": "StopFailure",
               "error": "rate_limit", "error_details": "429 Too Many Requests", "last_assistant_message": "API Error: Rate limit reached"}


def test_the_documented_rate_limit_payload_becomes_a_block_with_an_assumed_ttl():
    block = qs.block_from_payload(DOC_PAYLOAD, NOW)
    assert block["error"] == "rate_limit" and block["observed_at"] == NOW and block["source"] == qs.SOURCE
    assert block["reset_source"] == "assumed-ttl" and block["resets_at"] == NOW + qs.DEFAULT_TTL_S


def test_only_quota_errors_of_a_stopfailure_are_blocks():
    assert qs.block_from_payload({**DOC_PAYLOAD, "error": "billing_error"}, NOW)["error"] == "billing_error"
    for error in ("overloaded", "authentication_failed", "server_error", "unknown", None):
        assert qs.block_from_payload({**DOC_PAYLOAD, "error": error}, NOW) is None
    assert qs.block_from_payload({**DOC_PAYLOAD, "hook_event_name": "Stop"}, NOW) is None
    assert qs.block_from_payload("junk", NOW) is None


def test_a_reset_is_used_only_when_the_text_states_one():
    iso = qs.block_from_payload({**DOC_PAYLOAD, "last_assistant_message": "usage limit reached, resets 2026-10-27T18:00:00Z"}, 1_793_113_200)
    assert iso["reset_source"] == "details" and iso["resets_at"] == 1_793_124_000
    assert qs.parse_reset("try again in 90 seconds", NOW) == NOW + 90
    assert qs.parse_reset("Retry after 2 hours", NOW) == NOW + 7200
    assert qs.parse_reset('{"resets_at": 1790003600}', NOW) == 1_790_003_600
    assert qs.parse_reset("resets at 2001-01-01T00:00:00Z", NOW) is None, "a time in the past is not believed"
    assert qs.parse_reset("retry in 400 days", NOW) is None, "neither is one beyond a week"
    assert qs.parse_reset("nothing here", NOW) is None and qs.parse_reset("", NOW) is None


def test_the_sidecar_is_merged_atomically_and_drops_expired_blocks(tmp_path):
    path = tmp_path / "cache" / "quota-blocks.json"
    qs.record(path, "old", {"resets_at": NOW - 5, "observed_at": NOW - 900}, NOW)
    qs.record(path, "claude-subscription", qs.block_from_payload(DOC_PAYLOAD, NOW), NOW)
    assert sorted(qs.load(path)) == ["claude-subscription"] and json.loads(path.read_text())["version"] == qs.FORMAT_VERSION
    assert [p.name for p in path.parent.iterdir()] == ["quota-blocks.json"], "no temp file is left behind"
    assert qs.active_block(path, "claude-subscription", NOW + 10)["error"] == "rate_limit"
    assert qs.active_block(path, "claude-subscription", NOW + qs.DEFAULT_TTL_S) is None
    assert qs.active_block(path, "other", NOW) is None and qs.active_block(None, "x", NOW) is None


def test_a_foreign_or_corrupt_sidecar_is_no_blocks(tmp_path):
    path = tmp_path / "b.json"
    for text in ("not json", json.dumps({"version": 99, "blocks": {"p": {}}}), json.dumps([1]), json.dumps({"version": 1, "blocks": []})):
        path.write_text(text, encoding="utf-8")
        assert qs.load(path) == {}
    assert qs.load(tmp_path / "missing.json") == {}


def _block_file(tmp_path, **over):
    path = tmp_path / "quota-blocks.json"
    qs.record(path, "claude-subscription", {**qs.block_from_payload(DOC_PAYLOAD, NOW), **over}, NOW)
    return path


def test_an_active_block_reads_as_an_exhausted_pool_until_it_lifts(tmp_path):
    path = _block_file(tmp_path)
    r = hs.read_quota_block(path, NOW + 30)
    assert (r.state, r.used_fraction, r.resets_at) == (hr.KNOWN, 1.0, NOW + qs.DEFAULT_TTL_S)
    assert r.source == "quota-block:rate_limit:assumed-ttl" and r.read_at == NOW
    eff = hr.effective_weight(1.0, r, NOW + 30)
    assert eff.basis == "live" and eff.weight == hr.EPSILON
    later = hs.read_quota_block(path, NOW + qs.DEFAULT_TTL_S + 1)
    assert later.state == hr.UNKNOWN and later.source == "quota-block:none-active"


def test_signal_unavailable_is_reported_as_unknown_never_as_headroom(tmp_path):
    assert hs.read_quota_block(tmp_path / "absent.json", NOW).source == "quota-block:signal-unavailable"
    assert hs.read_quota_block(None, NOW).source == "quota-block:no-path"
    empty = tmp_path / "e.json"
    empty.write_text(json.dumps({"version": 1, "blocks": {}}), encoding="utf-8")
    r = hs.read_quota_block(empty, NOW)
    assert r.state == hr.UNKNOWN and r.used_fraction is None
    assert hr.effective_weight(0.7, r, NOW).weight == 0.7, "no signal leaves the static weight alone"


def test_the_claude_pool_prefers_a_live_block_over_an_older_percentage(tmp_path):
    sidecar = tmp_path / "rl.json"
    sidecar.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": NOW + 3600}}}), encoding="utf-8")
    deps = {"claude_sidecar_path": sidecar, "quota_blocks_path": _block_file(tmp_path)}
    assert hs.read_for_pool({"id": "claude-subscription", "headroom_source": "statusline-sidecar"}, True, deps, NOW + 30).used_fraction == 1.0
    assert hs.lane_weights(["claude-subscription"], deps, NOW + 30) == {"claude-subscription": hr.EPSILON}
    after = NOW + qs.DEFAULT_TTL_S + 1
    assert hs.read_for_pool({"id": "claude-subscription", "headroom_source": "statusline-sidecar"}, True, deps, after).used_fraction == 0.2
    assert hs.read_for_pool({"id": "claude-subscription", "headroom_source": "statusline-sidecar"}, False, deps, NOW).source == "not-requested"


def test_default_lane_deps_name_the_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_QUOTA_BLOCKS", raising=False)
    assert hs.default_lane_deps(tmp_path)["quota_blocks_path"] == tmp_path / ".cache" / "ai-resources" / "quota-blocks.json"
    monkeypatch.setenv("AI_RESOURCES_QUOTA_BLOCKS", str(tmp_path / "x.json"))
    assert hs.default_lane_deps(tmp_path)["quota_blocks_path"] == tmp_path / "x.json"


def _run_hook(payload, path, **env):
    return subprocess.run([sys.executable, str(REPO / "hooks" / "kit_quota_signal.py")], input=payload if isinstance(payload, str) else json.dumps(payload),
                          text=True, capture_output=True, env={"AI_RESOURCES_QUOTA_BLOCKS": str(path), "PATH": "/usr/bin:/bin", **env})


def test_the_hook_records_a_rate_limit_and_stays_silent(tmp_path):
    path = tmp_path / "q.json"
    r = _run_hook(DOC_PAYLOAD, path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert qs.load(path)["claude-subscription"]["error"] == "rate_limit"
    assert _run_hook(DOC_PAYLOAD, path, AI_RESOURCES_QUOTA_POOL="other-pool").returncode == 0
    assert sorted(qs.load(path)) == ["claude-subscription", "other-pool"]


def test_the_hook_ignores_other_errors_and_bad_input(tmp_path):
    path = tmp_path / "q.json"
    for payload in ({**DOC_PAYLOAD, "error": "overloaded"}, {"hook_event_name": "Stop"}, "not json", "[]"):
        r = _run_hook(payload, path)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not path.exists()


def test_the_hook_is_registered_for_the_quota_errors_only():
    doc = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    [entry] = doc["hooks"]["StopFailure"]
    assert entry["matcher"] == "rate_limit|billing_error" and "kit_quota_signal.py" in entry["hooks"][0]["command"]
