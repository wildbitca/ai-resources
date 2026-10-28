"""A second consecutive setup against the same host changes nothing and warns only about operator-owned items.

Driven through `openclaw.configure` on the simulated host of test_openclaw_host_wizard: no real ~/.openclaw, no gateway.
"""
from __future__ import annotations

import json

from test_openclaw_host_wizard import _configure, _state, script, sim  # noqa: F401  (fixtures are used by name)
from ai_resources.setup import state

# Warnings a second run may still print: each is something only the operator can do, named exactly.
OPERATOR_ONLY = ("workspace directory does not exist",)


def test_the_second_setup_writes_nothing_and_warns_only_about_operator_items(sim, script):  # noqa: F811
    s = _state()
    _configure(s)
    files = sim.files()
    persisted = json.dumps(state._to_dict(s.openclaw), sort_keys=True)
    mark = len(script.messages("warn"))
    _configure(s)
    assert sim.files() == files, "the second run wrote a file"
    assert json.dumps(state._to_dict(s.openclaw), sort_keys=True) == persisted
    extra = script.messages("warn")[mark:]
    assert all(any(k in w for k in OPERATOR_ONLY) for w in extra), extra

