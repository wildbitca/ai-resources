"""`gateway.bind: tailnet` is only written on a host that is on a tailnet."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_host as section  # noqa: E402

VALUES = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.42.0.0/24"}


def _patch(doc, **extra):
    return host.build_host_patch(host.load_host_profile(), doc, {**VALUES, **extra})


def test_without_a_tailnet_the_bind_is_not_written_and_the_reason_is_listed():
    built = _patch({}, TAILNET="0")
    assert "bind" not in built["patch"].get("gateway", {})
    assert any(s.startswith("gateway.bind:") and "Tailscale" in s for s in built["skipped"])


def test_without_a_tailnet_an_existing_loopback_bind_is_left_alone():
    built = _patch({"gateway": {"bind": "loopback"}}, TAILNET="0")
    assert "bind" not in built["patch"].get("gateway", {})
    assert not any(c["path"] == ["gateway", "bind"] for c in built["changes"])


def test_without_a_tailnet_the_rest_of_the_gateway_block_is_still_written():
    gw = _patch({}, TAILNET="0")["patch"]["gateway"]
    assert gw["publicOrigin"] == "https://ai.example.org"
    assert gw["trustedProxies"] == ["10.42.0.0/24"]


def test_with_a_tailnet_an_existing_loopback_bind_is_kept():
    # fill-only, ADR-0003: the profile never overwrites a value the operator set.
    built = _patch({"gateway": {"bind": "loopback"}}, TAILNET="1")
    assert "bind" not in built["patch"].get("gateway", {})
    assert "gateway.bind" in built["kept"]


def test_with_a_tailnet_and_no_bind_the_bind_is_filled():
    built = _patch({"gateway": {}}, TAILNET="1")
    assert built["patch"]["gateway"]["bind"] == "tailnet"
    assert "gateway.bind" in built["filled"]


def test_with_a_tailnet_a_forced_bind_replaces_loopback():
    built = host.build_host_patch(host.load_host_profile(), {"gateway": {"bind": "loopback"}},
                                  {**VALUES, "TAILNET": "1"},
                                  overrides={"keep": frozenset(), "force": frozenset({"gateway.bind"})})
    assert built["patch"]["gateway"]["bind"] == "tailnet"
    [ch] = [c for c in built["changes"] if c["path"] == ["gateway", "bind"]]
    assert ch["previous"] == "loopback" and ch["action"] == "forced"


def test_when_detection_did_not_run_the_profile_value_stands():
    assert _patch({}, )["patch"]["gateway"]["bind"] == "tailnet"


def test_the_cockpit_passes_the_detection_to_the_builder(monkeypatch):
    o = state.OpenClawState()
    monkeypatch.setattr(host, "tailnet_available", lambda: False)
    assert section._values(o)["TAILNET"] == "0"
    monkeypatch.setattr(host, "tailnet_available", lambda: True)
    assert section._values(o)["TAILNET"] == "1"


def test_detection_needs_the_cli_and_an_address(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda _n: None)
    assert host.tailnet_available() is False

    monkeypatch.setattr(host.shutil, "which", lambda _n: "/usr/bin/tailscale")
    run = lambda out, rc=0: (lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=""))  # noqa: E731
    monkeypatch.setattr(host.subprocess, "run", run("100.64.0.9\n"))
    assert host.tailnet_available() is True
    monkeypatch.setattr(host.subprocess, "run", run("", rc=1))        # tailscaled is not running
    assert host.tailnet_available() is False
    monkeypatch.setattr(host.subprocess, "run", run("\n"))
    assert host.tailnet_available() is False


def test_detection_survives_a_hung_or_broken_cli(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda _n: "/usr/bin/tailscale")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("tailscale", 5)

    monkeypatch.setattr(host.subprocess, "run", boom)
    assert host.tailnet_available() is False
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert host.tailnet_available() is False
