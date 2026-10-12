"""`models update` / `models rollback` hold watchdog.off themselves and then call the real restart: the restart
must run inside that window, and refuse only inside one the operator opened (kit 2.2c, 3c)."""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import models, openclaw_host as host  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    seen: list = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda argv, **kw: seen.append(argv) or (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda backend: True)
    monkeypatch.setattr(host, "gateway_cgroup_state", lambda path=None: "outside")
    return seen


def _real_restart():
    return models.default_deps().restart


def test_a_restart_inside_the_models_own_window_runs_and_keeps_the_window(calls):
    marker = host.Marker()
    marker.touch()                                    # what _restart_and_verify does before it restarts
    token = models._WINDOW_OWNED.set(True)
    try:
        _real_restart()()
    finally:
        models._WINDOW_OWNED.reset(token)
    assert calls == [["gateway", "restart"]]
    assert marker.exists(), "the caller closes the window it opened, the restart does not"


def test_a_restart_inside_an_operators_window_is_deferred(calls):
    marker = host.Marker()
    marker.touch()                                    # not created by models: _WINDOW_OWNED stays False
    with pytest.raises(models.RestartDeferred):
        _real_restart()()
    assert calls == [] and marker.exists()

