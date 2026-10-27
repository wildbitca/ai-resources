"""ADR-0019 D3: Antigravity (agy) can write. No role, matrix cell, lane command or tripwire bars an agy writer; the scope
contract stays as verification and logging, the same on every lane."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_resources import roles_policy as rp
from ai_resources.scoring import routing
from test_lanes_cmd_agy import (LABELS, _open, label, prep, verify, write_table)  # noqa: F401
from test_lanes_cmd import env  # noqa: F401

AGY = "agy-cli-lane"
WRITERS = ("implementer", "tester", "doc-writer", "generalPurpose", "package-upgrade", "crashlytics-fixer",
           "sentry-fixer", "terraform-maintainer", "crossplane-upjet-maintainer")


def test_the_default_matrix_gives_agy_the_primary_of_write_roles():
    agy_writers = [r for r in WRITERS if routing.ROLES[r].primary.startswith("agy:")]
    assert {"tester", "doc-writer", "generalPurpose", "package-upgrade"} <= set(agy_writers)
    assert all(routing.ROLES[r].writes for r in agy_writers)


@pytest.mark.parametrize("role", WRITERS)
def test_any_writer_role_can_be_routed_to_agy_by_a_profile(role):
    doc = {"routing": {role: {"primary": "agy:gemini-4-argon-high", "fallback": "claude-code:sonnet"}}}
    route = routing.resolve(role, {}, profile_doc=doc)
    assert (route.backend, route.model, route.pool, route.cause) == ("agy", "gemini-4-argon-high", routing.POOL_AGY_GEMINI, None)


def test_no_gemini_tripwire_or_report_only_agy_list_remains():
    assert not hasattr(rp, "gemini_tripwires") and not hasattr(rp, "GEMINI_TRIPWIRE_CANDIDATES")
    assert not any(".gemini" in p for p in rp.TRIPWIRE_PATHS + rp.TRIPWIRE_STAT_PATHS)


def test_an_agy_lane_prepares_and_verifies_the_integrating_writer(env):
    write_table(AGY)
    rc, p = prep(env, AGY, "impl", "implementer", "integrating")
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"src/new.py": "x = 1\n"})
    env.report("implementer", "impl", base=wt)
    rc, out = verify(env, "impl", p)
    assert rc == 0, out


def test_an_agy_lane_prepares_and_verifies_a_scope_writer(env):
    write_table(AGY)
    rc, p = prep(env, AGY, "tests", "tester", "scope", scope=("tests/",))
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    rc, out = verify(env, "tests", p)
    assert rc == 0, out


@pytest.mark.parametrize("lane", sorted(LABELS))
def test_a_scope_violation_is_logged_the_same_on_every_lane(env, lane):
    write_table(lane)
    rc, p = prep(env, lane, "tests", "tester", "scope", scope=("tests/",))
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n", "src/outside.py": "y = 2\n"})
    env.report("tester", "tests", base=wt)
    rc, out = verify(env, "tests", p)
    assert rc == 1 and any("src/outside.py" in r and "outside the write scope" in r for r in out["reasons"]), out


def test_path_in_scope_is_a_pure_advisory_check():
    assert rp.path_in_scope("tests/a/b.py", ("tests/",)) and not rp.path_in_scope("src/a.py", ("tests/",))
