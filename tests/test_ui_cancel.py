"""Ctrl-C scoping in the ui prompt wrappers and the wizard/CLI backstops."""
from __future__ import annotations

import pytest

from ai_resources import cli
from ai_resources.setup import flow, ui, wizard


class _Q:
    def __init__(self, interrupt=False, value="x"):
        self.interrupt, self.value = interrupt, value

    def ask(self):
        return None  # questionary.ask() swallows KeyboardInterrupt

    def unsafe_ask(self):
        if self.interrupt:
            raise KeyboardInterrupt
        return self.value


class _FakeQuestionary:
    def __getattr__(self, name):
        return lambda *a, **k: _Q(interrupt=True)


@pytest.fixture
def fake_q(monkeypatch):
    monkeypatch.setattr(ui, "questionary", _FakeQuestionary(), raising=False)
    monkeypatch.setattr(ui, "_HAS_QUESTIONARY", True)
    monkeypatch.setattr(ui, "_QSTYLE", None)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)


CALLS = [
    lambda: ui.select("m", ["a"]),
    lambda: ui.checkbox("m", ["a"]),
    lambda: ui.text("m"),
    lambda: ui.password("m"),
    lambda: ui.confirm("m"),
]


@pytest.mark.parametrize("call", CALLS)
def test_cancel_in_scope_raises(fake_q, call):
    with ui.prompt_scope(wizard=True):
        with pytest.raises(flow.Cancelled):
            call()


@pytest.mark.parametrize("call", CALLS)
def test_outside_scope_returns_none(fake_q, call):
    assert call() is None


def test_input_eof_in_scope_cancels(monkeypatch):
    monkeypatch.setattr(ui, "_HAS_QUESTIONARY", False)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)

    def boom(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    with ui.prompt_scope(wizard=True):
        with pytest.raises(flow.Cancelled):
            ui.confirm("m")


# Non-interactive regression table: what each wrapper returns (or exits with) with no terminal, in and out of
# the wizard scope. These values are today's behaviour and must not move.
_NI = [
    ("select-default", lambda: ui.select("m", ["a", "b"], default="b"), "b"),
    ("checkbox-default", lambda: ui.checkbox("m", ["a", "b"], default=["a"]), ["a"]),
    ("text-default", lambda: ui.text("m", default="4000"), "4000"),
    ("text-empty", lambda: ui.text("m"), ""),
    ("confirm-true", lambda: ui.confirm("m", default=True), True),
    ("confirm-false", lambda: ui.confirm("m", default=False), False),
]
_NI_EXIT = [
    ("select-no-default", lambda: ui.select("m", ["a", "b"]), "no saved answer"),
    ("password", lambda: ui.password("m"), "--non-interactive"),
]


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("name,call,expected", _NI, ids=[n for n, _, _ in _NI])
def test_non_interactive_returns_unchanged(monkeypatch, scoped, name, call, expected):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    with ui.prompt_scope(wizard=scoped):
        assert call() == expected


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("name,call,fragment", _NI_EXIT, ids=[n for n, _, _ in _NI_EXIT])
def test_non_interactive_exits_unchanged(monkeypatch, scoped, name, call, fragment):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    with ui.prompt_scope(wizard=scoped):
        with pytest.raises(SystemExit) as e:
            call()
    assert fragment in str(e.value)


def test_wizard_run_cancel_returns_130(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(wizard.state, "locked_run", lambda fn, **kw: fn())

    def run_inner(args):
        ui.section(4, 10, "Provider backends")
        ui.record_action("installed pipx")
        raise ui.Cancelled(ui.current_step())
    monkeypatch.setattr(wizard, "_run", run_inner)
    rc = wizard.run(argparse.Namespace(dry_run=True))
    err = capsys.readouterr().err
    assert rc == 130
    assert "Provider backends" in err and "installed pipx" in err


def test_cli_main_backstop_for_doctor(monkeypatch, capsys):
    """A bare Ctrl-C inside a command (here a ui call made by `doctor`) ends in 130 and one line, no traceback."""
    from ai_resources import doctor

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(doctor.ui, "require_deps", lambda: None)
    monkeypatch.setattr(doctor.ui, "banner", boom)
    assert cli.main(["doctor"]) == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err and "Cancelled." in err


def test_cli_main_backstop_for_cancelled(monkeypatch, capsys):
    from ai_resources import doctor
    monkeypatch.setattr(doctor.ui, "require_deps", lambda: None)

    def boom(*a, **k):
        raise flow.Cancelled("Credentials")
    monkeypatch.setattr(doctor.ui, "banner", boom)
    assert cli.main(["doctor"]) == 130
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("v,ok", [("4000", True), ("", True), ("abc", False), ("0", False), ("70000", False)])
def test_port_validator(v, ok):
    assert (wizard._validate_port(v) is True) == ok
