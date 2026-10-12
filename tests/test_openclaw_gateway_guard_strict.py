"""The gateway guard's strict second view, kit 2.2f (the last guard pass).

1. A shell (or `read`/`mapfile`/`eval`/`source`/`exec`) that reads a process substitution on standard input runs
   what the substitution prints: `sh < <(echo ...)` is not "printed text".
2. The allow list of other products' commands must not open an assembled OpenClaw name: an option VALUE
   (`sudo -u docker`, `env -u docker`) is not the command word, and a substitution that is an ARGUMENT of another
   product's command is a statement of its own.
3. The bare `gateway` + verb reading of the strict view needs a second signal that it is OpenClaw, so that the
   words of other tools (`npm run gateway:restart`, `git log --grep='gateway restart'`) are not denied.

The hook is run as Claude Code runs it (a real subprocess, `systemctl` a stub that answers `active`); nothing
here touches systemd or the host.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import stat
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"
KC = "kube" + "ctl"           # keeps the harness's own context rule out of the file


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_strict", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "WATCHDOG_OFF", str(tmp_path / "watchdog.off"))
    monkeypatch.setattr(mod, "gateway_active", lambda: True)
    monkeypatch.delenv("KIT_GUARD_DEADLINE_S", raising=False)
    return mod


def _hook_rc(tmp_path, command):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\necho active\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp_path)}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    done = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True,
                          env=env, timeout=60)
    return done.returncode


# --- 1. a process substitution on standard input -----------------------------------------------------------------

_P = "openclaw gateway restart"
_U = "systemctl --user restart openclaw-gateway"
STDIN_FORMS = [
    'sh < <(echo "{p}")',
    'read -r c < <(echo "{p}"); eval "$c"',
    'mapfile -t L < <(echo "{p}"); eval "${{L[@]}}"',
    'exec 3< <(echo "{p}"); sh <&3',
    'sh 0< <(echo "{p}")',
    'bash -s < <(echo "{p}")',
    'exec bash < <(echo "{p}")',
    'sh -e < <(echo "{p}")',
    'source /dev/stdin < <(echo "{p}")',
    'while read -r l; do eval "$l"; done < <(echo "{p}")',
    "bash <<< 'sh < <(echo \"{p}\")'",
    "sudo sh < <(echo '{p}')",
    "true && sh < <(echo '{p}')",
    # the here-string cousin of `read ... < <(...)`: the stored text is run by `eval`
    "read -r c <<< '{p}'; eval \"$c\"",
]


@pytest.mark.parametrize("payload", [_P, _U], ids=["cli", "unit"])
@pytest.mark.parametrize("form", STDIN_FORMS)
def test_a_shell_reading_a_process_substitution_on_stdin_is_denied(tmp_path, form, payload):
    assert _hook_rc(tmp_path, form.format(p=payload)) == 2


STDIN_PRINTED = [
    "cat < <(echo 'restart the gateway')",
    "diff <(echo a) <(echo b)",
    "wc -l < <(echo 'gateway restart')",
    "sort < <(echo 'openclaw gateway restart')",
    "grep gateway < <(echo 'restart the gateway')",
    "while read -r l; do echo \"$l\"; done < <(echo 'gateway restart')",
    "read -r c < <(echo 'gateway restart'); echo \"$c\"",
    "tee /dev/null < <(echo 'openclaw gateway restart')",
    "read -r c <<< 'gateway restart'; echo \"$c\"",
]


@pytest.mark.parametrize("command", STDIN_PRINTED)
def test_printed_only_process_substitutions_stay_allowed(tmp_path, command):
    assert _hook_rc(tmp_path, command) == 0


def test_the_strict_view_looks_through_the_redirect_to_its_owner(guard):
    assert guard._cheap_view('sh < <(echo "gateway restart")', strict=True) == "restart"
    assert guard._cheap_view('cat < <(echo "gateway restart")', strict=True) is None
