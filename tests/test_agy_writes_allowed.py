"""ADR-0019 D3: Antigravity (agy) can write. No role, matrix cell or tripwire bars an agy writer; the scope
contract stays as verification and logging, the same for every backend."""
from __future__ import annotations

import pytest

from ai_resources import roles_policy as rp, scope_guard
from ai_resources.scoring import routing
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


def test_an_agy_write_outside_its_scope_is_logged_not_refused():
    assert scope_guard.violations(["tests/test_b.py", "src/outside.py"], ["tests/"], "r1") == ["src/outside.py"]
