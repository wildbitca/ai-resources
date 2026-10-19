"""ADR-0010 S11: the cockpit step (phase 2) names the cockpit, asks the OpenClaw engine and the channel accounts
before the team, and plans no execution. Every openclaw call is a fake; only `channels list` ever runs."""
from __future__ import annotations

import pathlib

import yaml

from ai_resources import openclaw_channels as oc
from ai_resources.setup import state, wizard
from ai_resources.setup.cockpits import _openclaw_channels as channel_ui
from wizard_fakes import Script

FIX = pathlib.Path(__file__).parent / "fixtures"
LISTING = (FIX / "openclaw_channels_list.json").read_text(encoding="utf-8")


def _state(*, openclaw=False, claude=True):
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=claude)
    s.cockpits["openclaw"] = state.CockpitState(installed=openclaw)
    selected = (["claude"] if claude else []) + (["openclaw"] if openclaw else [])
    s.profile.customizations["__targets__"] = {"selected": selected}
    return s


class Runner:
    def __init__(self, text=LISTING, rc=0):
        self.argvs, self.text, self.rc = [], text, rc

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        return self.rc, self.text


def _wire(monkeypatch, runner):
    monkeypatch.setattr(channel_ui, "_runner", lambda: runner)


def test_the_cockpit_step_comes_before_the_team_and_the_rest_after_it():
    ids = [s.id for s in wizard._build_steps(True)]
    assert ids.index("tools") < ids.index("cockpit") < ids.index("mode") < ids.index("preset")
    assert ids.index("preset") < ids.index("lanes") < ids.index("cockpits")
    spec = {s.id: s for s in wizard._build_steps(True)}["cockpit"]
    assert spec.rewindable and spec.number == 1


def test_claude_code_alone_is_auto_chosen_with_one_line(monkeypatch):
    """AC-S11b."""
    s = _state()
    script = Script()
    script.install(monkeypatch)
    spec = {x.id: x for x in wizard._build_steps(True)}["cockpit"]

    class C:
        pass
    c = C()
    c.s = s
    # nothing to ask: the runner skips the step and prints its header and this one line
    assert spec.skip(c) == ("Main tool", "Main tool: Claude Code (the only one among your tools).")
    assert wizard._step_cockpit_choice(s) == 0
    assert script.asked == []
    assert [m for m in script.shown if "Main tool" in m] == ["info: Main tool: Claude Code (the only one among your tools)."]


def test_no_capable_cockpit_skips_the_step_silently():
    s = _state(claude=False)
    spec = {x.id: x for x in wizard._build_steps(True)}["cockpit"]

    class C:
        pass
    c = C()
    c.s = s
    assert spec.skip(c) == ("Main tool", "")


def test_engine_and_channel_questions_come_first_and_only_list_runs(monkeypatch):
    """AC-S11a, AC-S11d: the engine and the channels are asked in the cockpit step; unticking slack plans a disable."""
    runner = Runner()
    _wire(monkeypatch, runner)
    s = _state(openclaw=True)
    script = Script(**{"Which engine": "claude-code",
                       "Which channel accounts": lambda values: [v for v in values if v != "slack:default"]})
    script.install(monkeypatch)
    assert wizard._step_cockpit_choice(s) == 0
    assert [a.split(": ", 1)[1].split(" ")[1] for a in script.asked] == ["engine", "channel"]
    assert not any("Starting team" in a for a in script.asked)
    assert runner.argvs == [["openclaw", "channels", "list", "--all", "--json"]]
    assert s.openclaw.engine == "claude-code"
    assert s.openclaw.channels_wanted == ["telegram:default"]
    assert sorted(s.openclaw.channels_seen) == ["slack:default", "telegram:default"]
    rows = wizard._review_rows(s)
    text = "\n".join(f"{a} {b}" for a, b in rows)
    assert "openclaw channels remove --channel slack --account default" in text
    assert "undo: openclaw channels add --channel slack --account default  (run it yourself)" in text
    assert runner.argvs == [["openclaw", "channels", "list", "--all", "--json"]]      # Review ran nothing


def test_a_ticked_disabled_channel_prints_the_add_command_and_plans_no_execution(monkeypatch):
    """AC-S11e."""
    listing = '{"chat": {"telegram": {"accounts": [{"id": "default", "enabled": true}]},' \
              ' "matrix": {"accounts": [{"id": "off", "enabled": false}]}}}'
    runner = Runner(listing)
    _wire(monkeypatch, runner)
    s = _state(openclaw=True)
    script = Script(**{"Which engine": "keep", "Which channel accounts": lambda values: list(values)})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    lines = channel_ui.review_lines(s)
    assert lines == ["Turn on yourself: openclaw channels add --channel matrix --account off  (run it yourself)"]
    assert runner.argvs == [["openclaw", "channels", "list", "--all", "--json"]]


def test_an_unreadable_listing_asks_no_channel_question(monkeypatch):
    _wire(monkeypatch, Runner("not json"))
    s = _state(openclaw=True)
    script = Script(**{"Which engine": "keep"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert not any("channel accounts" in a for a in script.asked)
    assert channel_ui.review_lines(s) == []
    assert s.openclaw.channels_seen == [] and s.openclaw.channels_wanted == []


def test_unattended_runs_ask_and_plan_nothing(monkeypatch):
    runner = Runner()
    _wire(monkeypatch, runner)
    s = _state(openclaw=True)
    script = Script(**{"Which engine": "keep"})
    script.install(monkeypatch, non_interactive=True)
    wizard._step_cockpit_choice(s)
    assert runner.argvs == [] and channel_ui.review_lines(s) == []


def test_a_saved_engine_is_not_asked_again_in_the_cockpit_step(monkeypatch):
    runner = Runner()
    _wire(monkeypatch, runner)
    s = _state(openclaw=True)
    s.openclaw.engine = "keep"
    script = Script()
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert script.asked == [] and runner.argvs == []


def test_the_post_team_step_skips_the_engine_when_the_cockpit_step_asked_it(monkeypatch):
    from ai_resources.setup.cockpits import openclaw as cockpit
    calls = []
    for name in ("prompt", "prompt_model", "prompt_audio", "prompt_more"):
        monkeypatch.setattr(cockpit, name, lambda s, _n=name, **k: calls.append(_n))
    s = _state(openclaw=True)
    s.openclaw.engine = "keep"
    setattr(s, wizard._PHASE2_ATTR, True)
    Script().install(monkeypatch)
    wizard._step7_cockpit_config(s)
    assert calls == ["prompt_model"]       # voice notes are the Audio step's, the host section the More step's
    calls.clear()
    delattr(s, wizard._PHASE2_ATTR)                       # no cockpit-step pass this run: the whole prompt, as before
    wizard._step7_cockpit_config(s)
    assert calls == ["prompt"] and getattr(s, wizard._WHOLE_ATTR) is True


def test_old_states_load_with_empty_channel_fields_and_unknown_keys_are_ignored():
    """AC-S11c."""
    old = yaml.safe_load((FIX / "setup_state" / "openclaw_host_voice.yaml").read_text())
    s = state._from_dict(state.SetupState, old)
    assert s.openclaw.channels_wanted == [] and s.openclaw.channels_seen == []
    assert "channels_wanted" not in state._to_dict(s)["openclaw"]          # additive: absent while empty
    s.openclaw.channels_wanted, s.openclaw.channels_seen = ["telegram:default"], ["telegram:default"]
    doc = state._to_dict(s)
    assert doc["openclaw"]["channels_wanted"] == ["telegram:default"] and doc["schema_version"] == "1.0"
    # a reader that does not know the fields (the pre-change _from_dict) keeps only the names it has
    known = {k: v for k, v in doc["openclaw"].items() if k not in ("channels_wanted", "channels_seen")}
    assert state._from_dict(state.OpenClawState, known).channels_wanted == []
