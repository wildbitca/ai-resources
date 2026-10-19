"""ADR-0010 S3: read-only channel inventory and the pure disable planner. No openclaw process is ever started."""
from __future__ import annotations

import itertools
import pathlib
import re

from ai_resources import openclaw_channels as oc

FIX = pathlib.Path(__file__).parent / "fixtures"
GOOD = (FIX / "openclaw_channels_list.json").read_text(encoding="utf-8")
UNKNOWN = (FIX / "openclaw_channels_list_unknown.json").read_text(encoding="utf-8")


def runner_of(rc, text):
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw))
        return rc, text
    run.calls = calls
    return run


def test_fixture_lists_telegram_enabled():
    inv = oc.list_channels(runner_of(0, GOOD))
    assert inv is not None
    tg = [a for a in inv if a.channel == "telegram"]
    assert tg and all(a.enabled for a in tg)
    assert {a.channel for a in inv} == {"telegram", "slack"}


def test_list_runs_only_the_read_form():
    run = runner_of(0, GOOD)
    oc.list_channels(run)
    assert run.calls[0][0] == ["openclaw", "channels", "list", "--all", "--json"]


def test_unknown_shape_missing_binary_timeout_bad_json_give_none():
    assert oc.list_channels(runner_of(0, UNKNOWN)) is None
    assert oc.list_channels(runner_of(127, "openclaw not found")) is None
    assert oc.list_channels(runner_of(124, "timed out")) is None
    assert oc.list_channels(runner_of(0, "not json")) is None
    assert oc.list_channels(runner_of(0, "[]")) is None

    def boom(argv, **kw):
        raise OSError("x")
    assert oc.list_channels(boom) is None


def test_unsafe_ids_are_refused():
    bad = '{"chat": {"slack": {"accounts": ["a; rm -rf ~"]}}}'
    assert oc.parse_inventory(bad) is None
    assert oc.parse_inventory('{"chat": {"slack": {"accounts": [{"id": "x", "enabled": "no"}]}}}') is None


INV = [
    oc.ChannelAccount("telegram", "default"),
    oc.ChannelAccount("slack", "default"),
    oc.ChannelAccount("slack", "ops", agent="ops-agent"),
    oc.ChannelAccount("matrix", "off", enabled=False),
]
KEYS = [a.key for a in INV] + ["ghost:x"]


def _subsets(items):
    for n in range(len(items) + 1):
        yield from itertools.combinations(items, n)


def test_every_executable_change_is_the_remove_form_with_its_undo():
    seen_all = 0
    for seen in _subsets(KEYS):
        for wanted in _subsets(KEYS):
            for item in oc.plan_changes(seen, wanted, INV):
                if isinstance(item, oc.PrintOnly):
                    continue
                seen_all += 1
                assert item.kind == "disable"
                assert item.argv[:3] == ("openclaw", "channels", "remove")
                assert item.undo_argv == oc.add_argv(item.channel, item.account)
                joined = " ".join(item.argv)
                assert "--delete" not in joined and "add" not in item.argv
                assert not re.search(r"token|secret|password|key|credential", joined, re.I)
    assert seen_all > 0


def test_no_executable_change_is_an_add():
    for seen in _subsets(KEYS):
        for wanted in _subsets(KEYS):
            for item in oc.plan_changes(seen, wanted, INV):
                if isinstance(item, oc.ChannelChange):
                    assert item.kind != "add"


def test_unticking_slack_plans_one_remove_and_names_the_undo():
    plan = oc.plan_changes(["telegram:default", "slack:default"], ["telegram:default"], INV)
    assert len(plan) == 1
    ch = plan[0]
    assert ch.command == "openclaw channels remove --channel slack --account default"
    assert ch.undo_command == "openclaw channels add --channel slack --account default"


def test_agent_flag_only_with_an_explicit_owner():
    plan = oc.plan_changes(["slack:ops"], [], INV)
    assert plan[0].argv[-2:] == ("--agent", "ops-agent")
    plan = oc.plan_changes(["slack:default"], [], INV)
    assert "--agent" not in plan[0].argv


def test_ticked_disabled_channel_is_print_only():
    plan = oc.plan_changes([], ["matrix:off"], INV)
    assert len(plan) == 1 and isinstance(plan[0], oc.PrintOnly)
    assert plan[0].text == "openclaw channels add --channel matrix --account off  (run it yourself)"


def test_names_outside_the_inventory_plan_nothing():
    assert oc.plan_changes(["ghost:x"], [], INV) == []


def test_committed_fixture_leaks_nothing():
    """AC-S3d: the leak grep of the plan."""
    assert not re.search(r"/home/|[0-9]{6,}|bot[0-9]+:|[A-Za-z0-9+/=_-]{32,}", GOOD)
