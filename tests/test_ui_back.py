"""Ctrl-B Back in the ui prompt wrappers: one eager binding merged into the built question, only when asked."""
from __future__ import annotations

import sys
import types

import pytest

from ai_resources.setup import flow, ui


class _KB:
    def __init__(self):
        self.added = []

    def add(self, *keys, **kw):
        def deco(fn):
            self.added.append((keys, kw, fn))
            return fn
        return deco


def _merge(items):
    return ("merged", items)


@pytest.fixture
def fake_pt(monkeypatch):
    mod = types.ModuleType("prompt_toolkit.key_binding")
    mod.KeyBindings = _KB
    mod.merge_key_bindings = _merge
    pkg = types.ModuleType("prompt_toolkit")
    pkg.key_binding = mod
    monkeypatch.setitem(sys.modules, "prompt_toolkit", pkg)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.key_binding", mod)


class _App:
    def __init__(self, key_bindings="existing"):
        self.key_bindings = key_bindings


class _Q:
    def __init__(self, result="x", app=None):
        self.application = app if app is not None else _App()
        self.result = result

    def unsafe_ask(self):
        return self.result

    def ask(self):
        return None


def test_binding_is_merged_only_when_back_is_enabled(fake_pt):
    q = _Q()
    with ui.prompt_scope(wizard=True, back=False):
        assert ui._ask(q) == "x"
    assert q.application.key_bindings == "existing"
    q = _Q()
    with ui.prompt_scope(wizard=True, back=True):
        assert ui._ask(q) == "x"
    kind, items = q.application.key_bindings
    assert kind == "merged" and items[0] == "existing"
    (keys, kw, _fn), = items[1].added
    assert keys == ("c-b",) and kw == {"eager": True}


def test_the_binding_exits_the_application_with_the_back_sentinel(fake_pt):
    q = _Q()
    with ui.prompt_scope(wizard=True, back=True):
        ui._ask(q)
    exits = []
    event = types.SimpleNamespace(app=types.SimpleNamespace(exit=lambda result: exits.append(result)))
    q.application.key_bindings[1][1].added[0][2](event)
    assert exits == [flow.BACK]


def test_a_question_without_bindings_gets_the_binding_alone(fake_pt):
    q = _Q(app=_App(key_bindings=None))
    with ui.prompt_scope(wizard=True, back=True):
        ui._ask(q)
    assert isinstance(q.application.key_bindings, _KB)


def test_back_result_raises_goback_and_ctrl_c_still_cancels(fake_pt):
    with ui.prompt_scope(wizard=True, back=True):
        with pytest.raises(flow.GoBack):
            ui._ask(_Q(result=flow.BACK))

    class Boom(_Q):
        def unsafe_ask(self):
            raise KeyboardInterrupt
    with ui.prompt_scope(wizard=True, back=True):
        with pytest.raises(flow.Cancelled):
            ui._ask(Boom())


def test_outside_the_wizard_scope_nothing_is_merged(fake_pt):
    q = _Q()
    assert ui._ask(q) is None            # .ask(): Ctrl-C is None, as before
    assert q.application.key_bindings == "existing"
    with ui.prompt_scope(wizard=False, back=True):
        assert ui.back_enabled() is False


def test_scope_restores_the_back_flag():
    with ui.prompt_scope(wizard=True, back=True):
        assert ui.back_enabled()
        with ui.prompt_scope(wizard=True, back=False):
            assert not ui.back_enabled()
        assert ui.back_enabled()
    assert not ui.back_enabled()


def test_a_question_double_without_application_is_left_alone(fake_pt):
    class Bare:
        def unsafe_ask(self):
            return "v"
    with ui.prompt_scope(wizard=True, back=True):
        assert ui._ask(Bare()) == "v"


# --- the real libraries, where they are installed -------------------------------------------------------------

def _drive(build, keys):
    pytest.importorskip("questionary")
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as inp:
        inp.send_text(keys)
        q = build(inp, DummyOutput())
        with ui.prompt_scope(wizard=True, back=True):
            return ui._ask(q)


_BUILDERS = {
    "select": lambda q, i, o: q.select("m", choices=["a", "b"], input=i, output=o),
    "checkbox": lambda q, i, o: q.checkbox("m", choices=["a", "b"], input=i, output=o),
    "confirm": lambda q, i, o: q.confirm("m", input=i, output=o),
    "text": lambda q, i, o: q.text("m", input=i, output=o),
    "password": lambda q, i, o: q.password("m", input=i, output=o),
}


@pytest.mark.parametrize("kind", sorted(_BUILDERS))
def test_real_ctrl_b_goes_back_and_ctrl_c_cancels(kind):
    qmod = pytest.importorskip("questionary")
    with pytest.raises(flow.GoBack):
        _drive(lambda i, o: _BUILDERS[kind](qmod, i, o), "\x02")
    with pytest.raises(flow.Cancelled):
        _drive(lambda i, o: _BUILDERS[kind](qmod, i, o), "\x03")


def test_real_enter_still_answers():
    qmod = pytest.importorskip("questionary")
    assert _drive(lambda i, o: _BUILDERS["select"](qmod, i, o), "\r") == "a"
    assert _drive(lambda i, o: _BUILDERS["text"](qmod, i, o), "hi\r") == "hi"
