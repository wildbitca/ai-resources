"""Real-agent tests, and the rule that a real-agent run which did not run is UNVERIFIED, never green.

A *real-agent test* is one that needs a live provider or agent runtime to mean anything: it marks itself
`@pytest.mark.real_agent` and calls a real model, CLI agent or gateway round trip. Everything else, including tests that
drive a fake dispatcher or a stub script, is an ordinary test and is never marked.

* By default a real-agent test is skipped with the reason `real-agent test: not requested (--real-agent)`.
* With `--real-agent` it runs; a test that finds its provider unavailable (outage, quota block, missing credentials) calls
  `provider_unavailable(reason)`, which skips with the reason `UNVERIFIED: ...`.
* Either way a skipped real-agent test is counted and printed as `UNVERIFIED`, never folded into the green count, and with
  `--real-agent` or `AI_RESOURCES_REQUIRE_REAL_AGENT=1` any UNVERIFIED test makes the session fail (exit status 1).

Loaded as a plugin by tests/conftest.py. The decisions are plain functions so they can be tested without a nested run.
"""
from __future__ import annotations

import os

import pytest

NOT_REQUESTED = "real-agent test: not requested (--real-agent)"
UNVERIFIED_PREFIX = "UNVERIFIED: "
STRICT_ENV = "AI_RESOURCES_REQUIRE_REAL_AGENT"


_SEEN: dict[str, str] = {}


def provider_unavailable(reason: str):
    """Skip a real-agent test whose provider cannot be reached; the run reports UNVERIFIED, not green."""
    pytest.skip(UNVERIFIED_PREFIX + reason)


def is_real_agent(item) -> bool:
    return item.get_closest_marker("real_agent") is not None


def strict(requested: bool, environ=None) -> bool:
    """True when an UNVERIFIED real-agent test must fail the session."""
    env = os.environ if environ is None else environ
    return bool(requested or env.get(STRICT_ENV, "") not in ("", "0", "false", "no"))


def exit_status(current: int, unverified: int, requested: bool, environ=None) -> int:
    """The session exit status: a strict run with UNVERIFIED tests fails even when everything that ran passed."""
    return 1 if current == 0 and unverified and strict(requested, environ) else current


def summary_lines(unverified: list[tuple[str, str]]) -> list[str]:
    if not unverified:
        return []
    return [f"UNVERIFIED: {len(unverified)} real-agent test(s) did not run, so they prove nothing:",
            *(f"  {nodeid}: {reason}" for nodeid, reason in unverified)]


def pytest_addoption(parser):
    parser.addoption("--real-agent", action="store_true", default=False,
                     help="run real-agent tests (they need a live provider); unavailable ones are UNVERIFIED and fail the run")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_agent: needs a live provider or agent runtime; skipped it is UNVERIFIED, never green (tests/real_agent_gate.py)")
    _SEEN.clear()


def pytest_collection_modifyitems(config, items):
    if config.getoption("--real-agent"):
        return
    skip = pytest.mark.skip(reason=NOT_REQUESTED)
    for item in items:
        if is_real_agent(item):
            item.add_marker(skip)


def pytest_runtest_logreport(report):
    """Runs where the results are collected (the controller under xdist, so workers need no shared state)."""
    if "real_agent" in report.keywords and report.skipped and not hasattr(report, "wasxfail"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _SEEN.setdefault(report.nodeid, reason.removeprefix("Skipped: "))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    for line in summary_lines(list(_SEEN.items())):
        terminalreporter.write_line(line, red=True, bold=line.startswith("UNVERIFIED"))


def pytest_sessionfinish(session, exitstatus):
    session.exitstatus = exit_status(int(exitstatus), len(_SEEN), bool(session.config.getoption("--real-agent")))
