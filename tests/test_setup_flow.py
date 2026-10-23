"""The step runner and the progress rail are pure (no rich, no questionary)."""
from __future__ import annotations

import pytest

from ai_resources.setup import flow
from ai_resources.setup.flow import GoBack, JumpTo, StepSpec, Wizard


class Box:
    def __init__(self):
        self.value = 0
        self.calls: list[str] = []
        self.notices: list[str] = []
        self.skips: list[tuple[str, str, str]] = []
        self.actions = 0


def build(box, specs, **kw):
    return Wizard(
        specs,
        on_skip=lambda spec, title, reason: box.skips.append((spec.id, title, reason)),
        snapshot=lambda: box.value,
        restore=lambda snap: setattr(box, "value", snap),
        notify=box.notices.append,
        changes=lambda: box.actions,
        **kw)


def step(box, sid, number=1, *, rc=0, effect=None, **kw):
    def run(ctx):
        box.calls.append(sid)
        if effect:
            return effect(ctx) or rc
        return rc
    return StepSpec(sid, number, sid.title(), run, **kw)


def test_linear_order_and_zero_rc():
    box = Box()
    w = build(box, [step(box, "a", 1), step(box, "b", 2), step(box, "c", 3)])
    assert w.run(None) == 0 and box.calls == ["a", "b", "c"]


def test_stops_on_the_first_non_zero_rc_and_returns_it():
    box = Box()
    w = build(box, [step(box, "a"), step(box, "b", rc=2), step(box, "c")])
    assert w.run(None) == 2 and box.calls == ["a", "b"]


def test_skip_prints_header_and_reason_and_never_runs_the_step():
    box = Box()
    skipped = step(box, "b", skip=lambda c: ("Gateway", "Skipped (single-model mode)."))
    w = build(box, [step(box, "a"), skipped, step(box, "c")])
    assert w.run(None) == 0
    assert box.calls == ["a", "c"] and box.skips == [("b", "Gateway", "Skipped (single-model mode).")]


def test_go_back_returns_to_the_previous_step_and_restores_the_snapshot():
    box = Box()
    state_at = {}

    def a(ctx):
        box.value += 1                      # the answer of step a stays: it is the default when a is re-asked

    attempts = {"n": 0}

    def b(ctx):
        state_at[attempts["n"]] = box.value
        box.value += 100                    # a half-applied answer of b, dropped on Back
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise GoBack()

    w = build(box, [step(box, "a", effect=a), step(box, "b", effect=b, back=True)])
    assert w.run(None) == 0
    assert box.calls == ["a", "b", "a", "b"]
    # a ran twice (+2) and b's partial +100 was undone once before b ran again
    assert box.value == 1 + 1 + 100 and state_at[0] == 1


def test_go_back_across_a_non_rewindable_step_is_refused_and_the_step_is_asked_again():
    box = Box()
    n = {"b": 0}

    def b(ctx):
        n["b"] += 1
        if n["b"] == 1:
            raise GoBack()

    w = build(box, [step(box, "a", rewindable=False), step(box, "b", effect=b, back=True)])
    assert w.run(None) == 0
    assert box.calls == ["a", "b", "b"]
    assert len(box.notices) == 1 and "Back stops here" in box.notices[0] and "A" in box.notices[0]


def test_a_step_that_made_a_change_is_not_a_back_target():
    box = Box()
    n = {"b": 0}

    def a(ctx):
        box.actions += 1                    # a installed something

    def b(ctx):
        n["b"] += 1
        if n["b"] == 1:
            raise GoBack()

    w = build(box, [step(box, "a", effect=a), step(box, "b", effect=b, back=True)])
    assert w.run(None) == 0
    assert box.calls == ["a", "b", "b"] and len(box.notices) == 1


def test_go_back_at_the_first_step_is_refused():
    box = Box()
    n = {"a": 0}

    def a(ctx):
        n["a"] += 1
        if n["a"] == 1:
            raise GoBack()

    w = build(box, [step(box, "a", effect=a, back=True)])
    assert w.run(None) == 0 and box.calls == ["a", "a"]
    assert box.notices == ["! There is no earlier step to go back to."]


def test_go_back_skips_over_a_skipped_step_only_when_the_one_before_is_rewindable():
    box = Box()
    n = {"c": 0}

    def c(ctx):
        n["c"] += 1
        if n["c"] == 1:
            raise GoBack()

    w = build(box, [step(box, "a"), step(box, "b", skip=lambda x: ("B", "skip")), step(box, "c", effect=c, back=True)])
    assert w.run(None) == 0
    assert box.calls == ["a", "c", "a", "c"]


def test_jump_to_continues_linearly_from_the_target():
    box = Box()
    jumped = {"done": False}

    def last(ctx):
        if not jumped["done"]:
            jumped["done"] = True
            raise JumpTo("b")

    w = build(box, [step(box, "a"), step(box, "b"), step(box, "c", effect=last)])
    assert w.run(None) == 0
    assert box.calls == ["a", "b", "c", "b", "c"]


def test_editable_lists_only_asked_rewindable_unlocked_labelled_steps():
    box = Box()
    seen = {}

    def last(ctx):
        seen["editable"] = [s.id for s in w.editable()]

    def installs(ctx):
        box.actions += 1

    w = build(box, [
        step(box, "tools", rewindable=False, edit_label="Tools"),
        step(box, "mode", edit_label="Mode"),
        step(box, "gw", effect=installs, edit_label="Gateway"),
        step(box, "skipped", skip=lambda c: ("S", "x"), edit_label="Skipped"),
        step(box, "nolabel"),
        step(box, "models", edit_label="Models"),
        step(box, "review", effect=last, rewindable=False),
    ])
    assert w.run(None) == 0
    # "mode" is not offered: a jump from it would re-run the locked "gw" step.
    assert seen["editable"] == ["models"]


def test_scope_receives_the_step_and_wraps_its_run():
    box = Box()
    events = []

    class Scope:
        def __init__(self, spec, back):
            self.spec, self.back = spec, back

        def __enter__(self):
            events.append(("enter", self.spec.id, self.back))

        def __exit__(self, *a):
            events.append(("exit", self.spec.id))
    w = build(box, [step(box, "a", back=True), step(box, "b")], scope=lambda spec, back: Scope(spec, back))
    w.run(None)
    assert events == [("enter", "a", True), ("exit", "a"), ("enter", "b", False), ("exit", "b")]


# --- rail --------------------------------------------------------------------------------------------------

def test_rail_model_statuses():
    m = flow.rail_model(4, skipped={3})
    assert [s for _n, _l, s in m] == ["done", "done", "skipped", "current", "pending"]
    assert [l for _n, l, _s in m] == ["Tools", "Cockpit", "Team", "Audio", "More"]
    assert flow.rail_model(1)[0][2] == "current"
    assert len(flow.rail_model(5)) == 5 and flow.rail_model(5)[-1][2] == "current"


def test_wide_rail_is_one_line_with_unicode_glyphs():
    out = flow.format_rail(flow.rail_model(4), 130, True)
    assert "\n" not in out and out.startswith(" ✔ Tools")
    assert "● Audio" in out and "○ More" in out


def test_rail_wraps_between_60_and_79_columns():
    out = flow.format_rail(flow.rail_model(4), 40 + 30, True)
    lines = out.split("\n")
    assert all(len(l) <= 70 for l in lines)
    assert "Audio" in out and "More" in out


def test_narrow_terminal_gets_one_line_without_a_rail():
    out = flow.format_rail(flow.rail_model(4), 50, True, title="Provider backends")
    assert out == "Step 4/5  Provider backends" and "✔" not in out


def test_ascii_glyphs_and_skipped_marker():
    out = flow.format_rail(flow.rail_model(4, skipped={3}), 120, False)
    assert "[x] Tools" in out and "[-] Team (skipped)" in out and "[>] Audio" in out and "[ ] More" in out
    assert out.isascii()


@pytest.mark.parametrize("width", [100, 70, 59, 50])
@pytest.mark.parametrize("unicode_ok", [True, False])
def test_every_width_renders_without_error(width, unicode_ok):
    assert flow.format_rail(flow.rail_model(3), width, unicode_ok)


def test_wizard_snapshot_restores_state_and_the_run_globals(monkeypatch):
    from ai_resources.setup import state, wizard
    s = state.SetupState()
    s.mode = "multi-model"
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "p1")
    monkeypatch.setattr(wizard, "_APPLIED_TARGETS", ["claude"])
    snap = wizard._snapshot_run(s)
    s.mode = "single-model"
    s.providers["x"] = state.ProviderState()
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "p2")
    wizard._APPLIED_TARGETS.append("cursor")
    ident = id(s)
    wizard._restore_run(s, snap)
    assert id(s) == ident and s.mode == "multi-model" and "x" not in s.providers
    assert wizard._REQUESTED_PROFILE == "p1" and wizard._APPLIED_TARGETS == ["claude"]


def test_back_in_a_step_that_already_saved_something_keeps_the_state_and_asks_again():
    box = Box()
    seen = []

    def b_effect(ctx):
        seen.append(box.value)
        if len(seen) == 1:
            box.value = 7
            box.actions += 1          # the step saved something, then the operator pressed Ctrl-B
            raise GoBack()
        return 0
    w = build(box, [step(box, "a", edit_label="A"), step(box, "b", effect=b_effect, back=True)])
    assert w.run(None) == 0
    assert box.calls.count("a") == 1               # no rewind to "a"
    assert seen == [0, 7]                          # the in-memory state was not restored
    assert any("Back stops here" in n for n in box.notices)


def test_editable_excludes_a_step_when_a_later_step_is_locked():
    box = Box()

    def change(ctx):
        box.actions += 1
    w = build(box, [step(box, "mode", edit_label="Mode"), step(box, "gateway", effect=change,
                                                                 edit_label="Gateway"),
                    step(box, "review", rewindable=False)])
    assert w.run(None) == 0
    assert [s.id for s in w.editable()] == []      # "mode" would re-run the locked gateway step
