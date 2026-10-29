"""Real-agent tests: a skipped one is UNVERIFIED, never green (tests/real_agent_gate.py); a provider outage never enters the default team."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import real_agent_gate as gate

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

SAMPLE = '''
import pytest
import real_agent_gate

@pytest.mark.real_agent
def test_needs_a_model():
    assert True

@pytest.mark.real_agent
def test_provider_is_down():
    real_agent_gate.provider_unavailable("quota block until 18:00")

def test_ordinary():
    assert True
'''


def run(tmp_path, *args, env=None):
    (tmp_path / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    full_env = {k: v for k, v in os.environ.items() if k != gate.STRICT_ENV}
    full_env.update({"PYTHONPATH": str(TESTS), **(env or {})})
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "real_agent_gate", "-p", "no:cacheprovider", "-q", "-rs", str(tmp_path / "test_sample.py"), *args],
                          capture_output=True, text=True, cwd=tmp_path, env=full_env)


def test_by_default_real_agent_tests_are_skipped_and_reported_unverified_without_failing(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0 and "1 passed, 2 skipped" in r.stdout
    assert "UNVERIFIED: 2 real-agent test(s) did not run" in r.stdout and gate.NOT_REQUESTED in r.stdout
    assert "test_ordinary" not in r.stdout.split("UNVERIFIED:")[1]


def test_when_requested_a_provider_outage_is_unverified_and_fails_the_run(tmp_path):
    r = run(tmp_path, "--real-agent")
    assert r.returncode == 1, r.stdout
    assert "2 passed, 1 skipped" in r.stdout and "UNVERIFIED: 1 real-agent test(s)" in r.stdout
    assert "UNVERIFIED: quota block until 18:00" in r.stdout


def test_the_gate_works_under_xdist_where_workers_do_not_share_state(tmp_path):
    pytest.importorskip("xdist")
    r = run(tmp_path, "--real-agent", "-n", "2")
    assert r.returncode == 1 and "UNVERIFIED: 1 real-agent test(s)" in r.stdout and "quota block until 18:00" in r.stdout


def test_the_environment_can_make_unverified_fail_the_default_run(tmp_path):
    assert run(tmp_path, env={gate.STRICT_ENV: "1"}).returncode == 1
    assert run(tmp_path, env={gate.STRICT_ENV: "0"}).returncode == 0


def test_the_decisions_are_plain_functions():
    assert gate.exit_status(0, 1, True) == 1 and gate.exit_status(0, 0, True) == 0
    assert gate.exit_status(0, 3, False, {}) == 0 and gate.exit_status(0, 3, False, {gate.STRICT_ENV: "1"}) == 1
    assert gate.exit_status(1, 3, True) == 1 and gate.exit_status(2, 3, True) == 2, "a real failure keeps its own status"
    assert gate.summary_lines([]) == [] and gate.summary_lines([("a::b", "why")])[1] == "  a::b: why"
    with pytest.raises(pytest.skip.Exception) as e:
        gate.provider_unavailable("down")
    assert str(e.value) == "UNVERIFIED: down"


def test_the_marker_is_registered_and_run_tests_forwards_the_flag():
    assert "real_agent" in subprocess.run([sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider"], capture_output=True, text=True,
                                          cwd=REPO).stdout
    assert "REAL_AGENT" in (REPO / "scripts" / "run-tests.sh").read_text(encoding="utf-8")


def test_the_rule_against_committing_an_outage_into_the_default_team_is_written_down():
    rule = (REPO / "rules" / "017-multimodel-routing.mdc").read_text(encoding="utf-8")
    assert "A provider outage never reaches the shared default team" in rule and "UNVERIFIED" in rule
    docs = (REPO / "docs" / "orchestration.md").read_text(encoding="utf-8")
    assert "Real-agent tests and UNVERIFIED" in docs and "@pytest.mark.real_agent" in docs


def test_no_shipped_profile_carries_an_outage_workaround():
    for profile in sorted((REPO / "profiles").glob("*.yaml")):
        text = profile.read_text(encoding="utf-8").lower()
        assert "outage" not in text and "temporary" not in text and "workaround" not in text, profile.name
