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



# --- kit 2.2d, 3d/6c: the window is taken with an exclusive create, never removed unless this run made it -----------

import dataclasses  # noqa: E402

from test_models_update import Env  # noqa: E402


class RacingMarker(host.Marker):
    """`exists()` says the window is free although somebody's file is there: what a look followed by a touch can
    meet when the operator opens the window in between."""

    def exists(self) -> bool:
        return False


def test_update_does_not_close_a_window_it_did_not_create(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.marker = RacingMarker(tmp_path / "watchdog.off")
    env.marker.path.write_text("", encoding="utf-8")                  # the operator's, opened a moment ago
    env.run(classes=["sonnet"])
    assert env.marker.path.exists()


def test_rollback_does_not_close_a_window_it_did_not_create(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.run(classes=["sonnet"]).rc == 0
    env.marker = RacingMarker(tmp_path / "watchdog.off")
    env.marker.path.write_text("", encoding="utf-8")
    assert models.run_rollback(env.deps()).outcome == "rolled_back"
    assert env.marker.path.exists()


def test_update_and_rollback_close_the_window_they_made(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.run(classes=["sonnet"]).rc == 0
    assert not env.marker.path.exists()
    assert models.run_rollback(env.deps()).outcome == "rolled_back"
    assert not env.marker.path.exists() and env.marker_during_restart == [True, True]
