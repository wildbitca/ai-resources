"""Pins for the mutants that survived the kit 2.2e audit round (kit 2.2f, 5a). Each test names the behaviour a
one-line mutation of the code removes; the mutation proofs are in the handoff."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import signal
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"
KC = "kube" + "ctl"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_pins", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# --- S2 / S3: `_another_product` ------------------------------------------------------------------------------------

def _product_at(guard, text: str) -> bool:
    low = text.lower()
    return guard._another_product(low, low.index("gateway"))


def test_another_product_is_the_command_word_only_after_wrappers_and_their_values(guard):
    assert _product_at(guard, "docker restart gateway") is True
    assert _product_at(guard, "sudo -n docker restart gateway") is True                 # a flag, then the command
    assert _product_at(guard, "sudo -u root docker restart gateway") is True            # a value, then the command
    assert _product_at(guard, "sudo -u docker ${oc:-x} gateway restart") is False       # the value is a user
    assert _product_at(guard, "env -C /opt/docker ${oc:-x} gateway restart") is False
    assert _product_at(guard, "env -u docker ${oc:-x} gateway restart") is False
    assert _product_at(guard, "${oc:-x} gateway restart docker") is False               # after the command word
    assert _product_at(guard, "echo docker; ${oc:-x} gateway restart") is False         # another statement


def test_a_plain_wrapper_names_another_product_further_on_unless_a_built_word_follows(guard):
    assert _product_at(guard, "ssh host docker restart gateway") is True
    assert _product_at(guard, "ssh host docker exec ${c} gateway restart") is False     # a built word after the name
    assert _product_at(guard, "su docker -c ${oc:-x} gateway restart") is False
    assert _product_at(guard, "runuser -u docker -- ${oc:-x} gateway restart") is False  # an option: deny
    assert _product_at(guard, "ssh $host docker restart gateway") is False              # a built word in front


def test_a_substitution_that_is_an_argument_of_a_product_has_its_own_head(guard):
    low = "docker ps $(${oc:-x} gateway restart)"
    assert guard._another_product(low, low.index("gateway")) is False
    low = "docker ps $(docker restart gateway)"
    assert guard._another_product(low, low.index("gateway")) is True
    low = "docker ps `${oc:-x} gateway restart`"
    assert guard._another_product(low, low.index("gateway")) is False


# --- F5: the unit-glob budget of the cheap view ---------------------------------------------------------------------

def test_unit_globs_past_the_budget_count_as_the_unit(guard):
    other = "openclaw-foo* "
    assert guard._cheap_view(other * 10 + "restart") is None
    assert guard._cheap_view(other * (guard._UNIT_GLOB_CHECKS + 100) + "restart") == "restart"


# --- E5: a unit is not answered by a subcommand ---------------------------------------------------------------------

def test_the_unit_is_not_answered_by_a_subcommand_word(guard):
    assert guard._cheap_view("systemctl --user show openclaw-gateway status restart", strict=True) == "restart"
    assert guard._cheap_view("openclaw gateway status --note restart", strict=True) is None


# --- K3 / K7 / K11 / K12: the marker ----------------------------------------------------------------------------------

def test_remove_leaves_a_file_overwritten_in_place_after_it_was_created(tmp_path):
    m = host.Marker(tmp_path / "watchdog.off")
    assert m.acquire() is True
    before = m.path.stat()
    m.path.write_text("", encoding="ascii")                         # same inode, the operator's window
    os.utime(m.path, ns=(before.st_atime_ns, before.st_mtime_ns + 3_000_000_000))
    assert m.path.stat().st_ino == before.st_ino
    m.remove()
    assert m.path.exists(), "the modification time tells this file from the one that was created"


def test_reclaim_leaves_a_file_replaced_by_another_inode(tmp_path, monkeypatch):
    path = tmp_path / "watchdog.off"
    text = f"kit-run pid={_dead_pid()} start=1 at=1\n"
    path.write_text(text, encoding="ascii")
    keep = tmp_path / "old-inode"

    def liveness(pid, start):
        os.link(path, keep)                                          # the inode number cannot be handed out again
        path.unlink()
        path.write_text(text, encoding="ascii")                      # a new file, the same content
        return False

    monkeypatch.setattr(host, "_kit_run_alive", liveness)
    assert host.Marker(path).reclaim_stale() is False
    assert path.exists()


def test_a_fifo_or_a_link_at_the_marker_path_is_read_without_blocking(tmp_path):
    fifo = tmp_path / "fifo.off"
    os.mkfifo(fifo)
    signal.alarm(10)                                                 # a blocked open would hang the suite
    try:
        assert host.Marker(fifo).stale_kit_marker() is False
        assert host.Marker(fifo).reclaim_stale() is False
    finally:
        signal.alarm(0)
    target = tmp_path / "t"
    target.write_text(f"kit-run pid={_dead_pid()} start=1 at=1\n", encoding="ascii")
    link = tmp_path / "link.off"
    link.symlink_to(target)
    assert host.Marker(link).stale_kit_marker() is False


@pytest.mark.parametrize("tail,stale", [(" at=1\n", True), ("\n", True), ("", True), ("0 at=1\n", True),
                                         ("x at=1\n", False), ("_\n", False)])
def test_the_marker_line_ends_its_start_field_with_a_separator(tmp_path, tail, stale):
    path = tmp_path / "watchdog.off"
    path.write_text(f"kit-run pid={_dead_pid()} start=77{tail}", encoding="ascii")
    # `start=77x` is not a signed marker: it is an operator's, and an operator's is never stale
    assert host.Marker(path).stale_kit_marker() is stale
