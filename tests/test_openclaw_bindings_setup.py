"""The wizard writes the Telegram group routing: the root `bindings` array, never `channels` (v1.9.8).

`build_bindings` is tested as a pure function; the wizard is driven through `openclaw.prompt`,
`configure` and `teardown` against the simulated host of test_openclaw_host_wizard. The last test is
the writer/reader contract: what the cockpit writes is what the narration hook resolves. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import copy
import json

import pytest

from fixtures.openclaw_bindings import GROUP_CHATS, catch_all, live_bindings, route
from test_openclaw_host_wizard import (  # noqa: F401  (fixtures are used by name)
    _run_wizard, _state, ANSWERS, script, sim,
)
from test_openclaw_team_hook import _load_hook
from ai_resources import openclaw_host as host
from ai_resources.setup import ui
from ai_resources.setup.cockpits import _openclaw_host as section, openclaw

ASK = "Telegram group chat id for agent"
APP_CHAT, INFRA_CHAT = "-5200000001", "-5200000002"
OPERATOR_ROUTE = {"type": "route", "agentId": "ops", "comment": "hand written by the operator",
                  "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-5999999999"}}}


def _dump(x) -> str:
    return json.dumps(x, indent=2)


# --- build_bindings: the pure part -----------------------------------------------------------------------

def test_the_catch_all_and_an_operator_binding_survive_and_the_catch_all_stays_last():
    current = [OPERATOR_ROUTE, catch_all()]
    new, notes = host.build_bindings(current, {"app": APP_CHAT})
    assert new[0] == OPERATOR_ROUTE and new[-1] == catch_all()
    assert [e["agentId"] for e in new] == ["ops", "app", "main"]
    assert notes == [f"app: bound to group {APP_CHAT}"]


def test_the_same_answers_twice_give_a_byte_identical_array():
    once, _ = host.build_bindings([OPERATOR_ROUTE, catch_all()], {"app": APP_CHAT, "infra": INFRA_CHAT})
    twice, notes = host.build_bindings(once, {"app": APP_CHAT, "infra": INFRA_CHAT})
    assert _dump(twice) == _dump(once) and notes == []


def test_a_catch_all_without_type_and_peer_is_neither_crashed_on_nor_reordered():
    bare = {"agentId": "main", "match": {"accountId": "*"}}
    new, _ = host.build_bindings([bare], {"app": APP_CHAT})
    assert new[-1] == bare and "type" not in new[-1] and "peer" not in new[-1]["match"]
    assert host.build_bindings(None, {}) == ([], [])
    assert host.build_bindings("garbage", {"app": APP_CHAT})[0][0]["agentId"] == "app"
    junk, _ = host.build_bindings([None, 3, "x"], {"app": APP_CHAT})
    assert junk[0]["agentId"] == "app"


def test_an_existing_binding_keeps_its_comment_and_bytes():
    current = live_bindings()
    new, notes = host.build_bindings(current, dict(GROUP_CHATS))
    assert _dump(new) == _dump(current) and notes == []


def test_a_moved_group_updates_the_peer_in_place_and_keeps_the_operator_comment():
    current = live_bindings()
    current[0]["comment"] = "my words"
    new, notes = host.build_bindings(current, {"snoutzone": "-5300000009"})
    assert new[0]["comment"] == "my words" and new[0]["match"]["peer"]["id"] == "-5300000009"
    assert len(new) == len(current) and new[-1] == catch_all()
    assert "snoutzone" in notes[0]


def test_a_peer_less_entry_in_the_middle_is_moved_last_but_nothing_else_is_reordered():
    current = [route("a", "-5000000001"), catch_all(), route("b", "-5000000002")]
    new, _ = host.build_bindings(current, {})
    assert [e["agentId"] for e in new] == ["a", "b", "main"]


def test_the_input_array_is_never_mutated():
    current = live_bindings()
    frozen = copy.deepcopy(current)
    host.build_bindings(current, {"snoutzone": "-5300000009", "new": "-5300000010"})
    assert current == frozen


@pytest.mark.parametrize("value,ok", [("-5217865131", True), ("-1001234567890", True), ("5217865131", False),
                                       ("-", False), ("@group", False), ("-52a", False), ("", False)])
def test_group_id_shape(value, ok):
    assert (host.validate_group_id(value) is None) is ok


def test_the_allowlist_gap_is_only_reported_under_allowlist_policy():
    doc = {"channels": {"telegram": {"groupPolicy": "allowlist", "groups": {"-5100000001": {}}}}}
    assert host.allowlist_gaps(doc, ["-5100000001", "-5100000002"]) == ["-5100000002"]
    doc["channels"]["telegram"]["groupPolicy"] = "open"
    assert host.allowlist_gaps(doc, ["-5100000002"]) == []
    assert host.allowlist_gaps({}, ["-5100000002"]) == []


def test_the_remedial_command_is_the_exact_merge_form():
    assert host.allowlist_fix_command("-5100000002") == (
        "openclaw config set channels.telegram.groups '{\"-5100000002\":{\"requireMention\":false}}' "
        "--strict-json --merge")


# --- the question ---------------------------------------------------------------------------------------

def _seed(sim, bindings, *, policy="allowlist", groups=None):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["bindings"] = bindings
    doc["channels"]["telegram"]["groupPolicy"] = policy
    if groups is not None:
        doc["channels"]["telegram"]["groups"] = groups
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    sim.original_config = sim.cfg.read_bytes()


def _bindings(sim):
    return json.loads(sim.cfg.read_text(encoding="utf-8")).get("bindings")


def _capture_defaults(monkeypatch, script):
    defaults: dict[str, str] = {}
    real = script.text

    def text(message, default="", validate=None):
        defaults[message] = default
        return real(message, default, validate)

    monkeypatch.setattr(ui, "text", text)
    return defaults


def test_the_question_is_prefilled_with_the_id_already_bound(sim, script, monkeypatch):
    _seed(sim, [route("app", APP_CHAT), catch_all()])
    defaults = _capture_defaults(monkeypatch, script)
    s = _state()
    openclaw.prompt(s)
    (prefilled,) = [d for m, d in defaults.items() if "`app`" in m]
    assert prefilled == APP_CHAT
    assert [d for m, d in defaults.items() if "`infra`" in m] == [""]
    assert not any("`main`" in m or "`claude`" in m for m in defaults), "the catch-all and the worker are never asked"


def test_an_empty_answer_leaves_the_existing_binding_untouched(sim, script):
    _seed(sim, [route("app", APP_CHAT), catch_all()])
    script.answers[ASK] = ""
    s = _state()
    _run_wizard(s)
    assert s.openclaw.host_group_ids == {} and _bindings(sim) == [route("app", APP_CHAT), catch_all()]
    assert not [a for a, p in sim.oc.real_patches() if p and "bindings" in p]


def test_a_malformed_id_is_rejected_naming_the_expected_shape(sim, script):
    s = _state()
    openclaw.prompt(s)
    check = next(v for m, v in script.validators.items() if "`app`" in m)
    assert check("") is True and check("-5200000001") is True
    message = check("5200000001")
    assert message is not True and "-5xxxxxxxxx" in message


def test_a_100_id_is_warned_about_and_accepted(sim, script):
    script.answers[ASK] = "-1001234567890"
    s = _state()
    openclaw.prompt(s)
    assert s.openclaw.host_group_ids["app"] == "-1001234567890"
    assert any("-100" in m and "supergroup" in m and "BASIC group" in m for m in script.messages("warn"))


def test_nothing_is_asked_in_an_unattended_run(sim, script):
    _seed(sim, [route("app", APP_CHAT), route("infra", INFRA_CHAT), route("docs", "-5200000003"), catch_all()])
    script.non_interactive = True
    s = _state()
    openclaw.prompt(s)
    assert not any(ASK in q for q in script.asked)
    openclaw.configure({"state": s})
    assert _bindings(sim) == [route("app", APP_CHAT), route("infra", INFRA_CHAT), route("docs", "-5200000003"), catch_all()]
    assert not sim.oc.real_patches()


def test_a_declined_answer_of_the_master_question_asks_no_group_id(sim, script):
    script.answers["Configure this machine as an OpenClaw host"] = False
    s = _state()
    openclaw.prompt(s)
    assert not any(ASK in q for q in script.asked)


# --- configure ------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _consent(script):
    """The bindings gate is answered yes unless a test says otherwise."""
    script.answers["Apply the group bindings to the running gateway"] = True


def _answer(monkeypatch, script, **chats):
    """Answer the group question per agent id (an agent not given answers empty)."""
    def text(message, default="", validate=None):
        script.asked.append(message)
        for aid, chat in chats.items():
            if f"`{aid}`" in message:
                return chat
        if ASK in message:
            return ""
        for key in sorted(script.answers, key=len, reverse=True):
            if key in message:
                return script.answers[key]
        raise AssertionError(f"unscripted question: {message!r}")
    monkeypatch.setattr(ui, "text", text)


def test_configure_sends_only_the_bindings_array_with_an_explicit_replace_path(sim, script, monkeypatch):
    _seed(sim, [OPERATOR_ROUTE, catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT, infra=INFRA_CHAT)
    s = _state()
    _run_wizard(s)
    binding_calls = [(a, p) for a, p in sim.oc.real_patches() if p and "bindings" in p]
    [(args, patch)] = binding_calls
    assert set(patch) == {"bindings"}, "root bindings only: never channels"
    assert args[args.index("--replace-path") + 1] == "bindings"
    dry = [c for c in sim.oc.dry_patches() if c[1] == patch]
    assert dry and sim.oc.calls.index(dry[0]) < sim.oc.calls.index((args, patch))
    live = _bindings(sim)
    assert [e["agentId"] for e in live] == ["ops", "app", "infra", "main"]
    assert live[0] == OPERATOR_ROUTE and live[-1] == catch_all()
    assert s.openclaw.host_group_ids == {"app": APP_CHAT, "infra": INFRA_CHAT}


def test_a_second_run_with_the_same_answers_changes_nothing(sim, script, monkeypatch):
    _seed(sim, [OPERATOR_ROUTE, catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    after_first = sim.cfg.read_bytes()
    sim.oc.calls.clear()
    _run_wizard(s)
    assert sim.cfg.read_bytes() == after_first
    assert not [1 for a, p in sim.oc.real_patches() if p and "bindings" in p]


def test_enter_enter_on_every_prefilled_question_changes_nothing_and_keeps_the_catch_all_last(sim, script, monkeypatch):
    """The live shape (group routes plus main's catch-all last): accept every default as typed."""
    bindings = [route("app", APP_CHAT), route("infra", INFRA_CHAT), route("docs", "-5200000003"), catch_all()]
    _seed(sim, bindings)

    def text(message, default="", validate=None):
        script.asked.append(message)
        if ASK in message:
            return default
        for key in sorted(script.answers, key=len, reverse=True):
            if key in message:
                return script.answers[key]
        raise AssertionError(message)
    monkeypatch.setattr(ui, "text", text)
    s = _state()
    _run_wizard(s)
    assert _bindings(sim) == bindings and _bindings(sim)[-1] == catch_all()
    assert not [1 for a, p in sim.oc.real_patches() if p and "bindings" in p], "no patch at all"
    assert s.openclaw.host_group_ids == {"app": APP_CHAT, "infra": INFRA_CHAT, "docs": "-5200000003"}


def test_a_declined_gate_writes_no_binding(sim, script, monkeypatch):
    _seed(sim, [catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT)
    script.answers["Apply the group bindings to the running gateway"] = False
    s = _state()
    _run_wizard(s)
    assert _bindings(sim) == [catch_all()] and not s.openclaw.bindings_applied


def test_a_rejected_dry_run_applies_nothing(sim, script, monkeypatch):
    _seed(sim, [catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT)
    sim.oc.dry_run_ok = False
    s = _state()
    _run_wizard(s)
    assert _bindings(sim) == [catch_all()] and not s.openclaw.bindings_applied


def test_a_dry_run_of_setup_sends_no_bindings_patch(sim, script, monkeypatch):
    _seed(sim, [catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s, dry_run=True)
    assert _bindings(sim) == [catch_all()] and not s.openclaw.bindings_applied


def test_the_channels_guard_still_refuses_a_channels_key_anywhere_in_the_patch():
    with pytest.raises(ValueError):
        openclaw.apply_patch({"bindings": [], "channels": {"telegram": {"groups": {}}}}, replace_paths=["bindings"])
    with pytest.raises(ValueError):
        openclaw.apply_patch({"bindings": []}, replace_paths=["bindings", "channels.telegram.groups"])


# --- the allowlist gap ----------------------------------------------------------------------------------

def test_a_routed_group_missing_from_the_allowlist_is_reported_with_the_exact_command(sim, script, monkeypatch):
    _seed(sim, [catch_all()], groups={})
    channels_before = json.loads(sim.cfg.read_text(encoding="utf-8"))["channels"]
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    warned = "\n".join(script.messages("warn"))
    assert APP_CHAT in warned and "T35" in warned and "DROP" in warned
    assert host.allowlist_fix_command(APP_CHAT) in warned
    for args, patch in sim.oc.real_patches() + sim.oc.dry_patches():
        assert patch is None or "channels" not in patch or set(patch["channels"]) == {"telegram"} and \
            set(patch["channels"]["telegram"]) == {"streaming"}
    assert json.loads(sim.cfg.read_text(encoding="utf-8"))["channels"]["telegram"]["groups"] == channels_before["telegram"]["groups"]


def test_a_listed_group_is_not_reported(sim, script, monkeypatch):
    _seed(sim, [catch_all()], groups={APP_CHAT: {"requireMention": False}})
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    assert not any(APP_CHAT in m and "NOT in channels" in m for m in script.messages("warn"))


def test_an_open_group_policy_needs_no_warning(sim, script, monkeypatch):
    _seed(sim, [catch_all()], policy="open", groups={})
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    assert not any("NOT in channels" in m for m in script.messages("warn"))


# --- teardown -------------------------------------------------------------------------------------------

def test_teardown_puts_the_array_back_as_the_snapshot(sim, script, monkeypatch):
    before = [OPERATOR_ROUTE, catch_all()]
    _seed(sim, before)
    _answer(monkeypatch, script, app=APP_CHAT, infra=INFRA_CHAT)
    s = _state()
    _run_wizard(s)
    assert _bindings(sim) != before
    openclaw.teardown(s)
    assert _dump(_bindings(sim)) == _dump(before)
    assert not s.openclaw.bindings_applied and s.openclaw.host_group_ids == {}


def test_teardown_removes_a_bindings_array_the_kit_created(sim, script, monkeypatch):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    assert "bindings" not in doc
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    assert _bindings(sim) == [route_of_kit("app", APP_CHAT)]
    openclaw.teardown(s)
    assert "bindings" not in json.loads(sim.cfg.read_text(encoding="utf-8"))


def route_of_kit(aid, chat):
    return {"type": "route", "agentId": aid, "comment": f"{aid} group (ai-resources)",
            "match": {"channel": "telegram", "peer": {"kind": "group", "id": chat}}}


def test_teardown_leaves_an_array_the_operator_changed_since(sim, script, monkeypatch):
    _seed(sim, [catch_all()])
    _answer(monkeypatch, script, app=APP_CHAT)
    s = _state()
    _run_wizard(s)
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["bindings"].insert(0, OPERATOR_ROUTE)
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    edited = _bindings(sim)
    openclaw.teardown(s)
    assert _bindings(sim) == edited
    assert any("changed since the kit wrote it" in m for m in script.messages("warn"))


# --- writer and reader agree ----------------------------------------------------------------------------

def test_what_the_cockpit_writes_is_what_the_narration_hook_resolves(sim, script, monkeypatch):
    """Writer (`build_bindings`) and reader (`resolve_target`, stdlib-only) are two implementations of one
    shape, kept honest by tests/fixtures/openclaw_bindings.py. Round trip through a real config file."""
    _seed(sim, live_bindings(), groups={})     # no forum topics: routing is bindings only
    _answer(monkeypatch, script, app=APP_CHAT, infra=INFRA_CHAT)
    s = _state()
    _run_wizard(s)
    hook = _load_hook()
    monkeypatch.setattr(hook, "OPENCLAW_JSON", str(sim.cfg))
    assert hook.resolve_target(str(sim.workspaces["app"])) == (APP_CHAT, None)
    assert hook.resolve_target(str(sim.workspaces["infra"])) == (INFRA_CHAT, None)
    # main's catch-all never becomes a target, before or after the kit wrote around it
    assert hook.resolve_target(str(sim.workspaces["main"])) is None
    assert _bindings(sim)[-1] == catch_all()
