"""ADR-0010 S12: the disable-only channel executor. Every openclaw call is a fake runner; no process starts.

The executor checks every guard itself, so each guard is exercised on `execute` directly, not through its caller."""
from __future__ import annotations

import hashlib
import inspect
import itertools
import pathlib
import re

import pytest

from ai_resources import openclaw_channels as oc
from ai_resources import openclaw_host as host
from ai_resources import openclaw_reload_rules as rules
from ai_resources.setup import state, ui, wizard
from ai_resources.setup.cockpits import _openclaw_channels as channel_ui

FIX = pathlib.Path(__file__).parent / "fixtures"
LISTING = (FIX / "openclaw_channels_list.json").read_text(encoding="utf-8")
HOT = rules.RuleSet("2026.10.3", "installed", rules.BASELINE_RULES)
UNREADABLE = rules.unavailable(None, "no dist")
PINNED_ASSERT_CHANNELS_SAFE_SHA256 = "f5ad904ac7663b6f3d7c3c669f46324fc6b4acf1a01dcfcce7ea1ad928bcc356"


def listing(*accounts):
    """A `channels list --all --json` payload: `accounts` are (channel, account) pairs."""
    chat: dict = {}
    for channel, account in accounts:
        chat.setdefault(channel, {"accounts": []})["accounts"].append(account)
    import json
    return json.dumps({"chat": chat})


class Runner:
    """Answers `channels list` from a queue and records every argv; a `remove` answers `remove_result`."""

    def __init__(self, lists, remove_result=(0, "removed")):
        self.lists = list(lists)
        self.remove_result = remove_result
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[:3] == ["openclaw", "channels", "list"]:
            return (0, self.lists.pop(0)) if self.lists else (1, "no more")
        if argv[:3] == ["openclaw", "channels", "remove"]:
            return self.remove_result
        raise AssertionError(f"unexpected command {argv}")

    @property
    def argvs(self):
        return [c[0] for c in self.calls]

    @property
    def removes(self):
        return [a for a in self.argvs if a[2:3] == ["remove"]]


def inventory(text=LISTING):
    inv = oc.parse_inventory(text)
    assert inv
    return inv


def change(channel="slack", account="default"):
    return oc.ChannelChange("disable", channel, account, oc.remove_argv(channel, account), oc.add_argv(channel, account))


class Confirm:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[tuple[str, bool]] = []

    def __call__(self, message, default=True):
        self.asked.append((message, default))
        return self.answers.pop(0) if self.answers else False


def run(ch=None, *, runner=None, confirm=None, tty=True, dry_run=False, yes=False, cockpit="openclaw",
        inv=None, reload_rules=HOT):
    ch = ch or change()
    runner = runner if runner is not None else Runner([LISTING, listing(("telegram", "default"))])
    confirm = confirm if confirm is not None else Confirm(True, True)
    out = oc.execute(ch, runner=runner, confirm=confirm, tty=tty, dry_run=dry_run, yes=yes, cockpit=cockpit,
                     inventory=inv if inv is not None else inventory(), reload_rules=reload_rules)
    return out, runner, confirm


UNDO = "undo: re-add with credentials, yourself: openclaw channels add --channel slack --account default"


# --- AC-S12a ------------------------------------------------------------------------------------------------------------

def test_a_confirmed_slack_untick_runs_exactly_one_list_then_one_remove_then_one_list():
    out, runner, confirm = run()
    assert runner.argvs == [
        ["openclaw", "channels", "list", "--all", "--json"],
        ["openclaw", "channels", "remove", "--channel", "slack", "--account", "default"],
        ["openclaw", "channels", "list", "--all", "--json"],
    ]
    assert out.status == oc.DONE and out.ran
    assert confirm.asked and all(default is False for _m, default in confirm.asked)
    assert any("turned off" in line for line in out.lines) and any(UNDO in line for line in out.lines)
    assert not any("unexpected" in line for line in out.lines)


def test_every_command_gets_the_60_second_timeout_the_cap_and_the_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
    _out, runner, _c = run()
    assert len(runner.calls) == 3
    for _argv, kw in runner.calls:
        assert kw["timeout"] == 60 and kw["max_bytes"] == oc.MAX_BYTES
        assert not {"OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"} & set(kw["env"])


# --- AC-S12b: the guards, each checked inside execute -------------------------------------------------------------------

@pytest.mark.parametrize("kw,word", [({"dry_run": True}, "dry run"), ({"yes": True}, "--yes"),
                                     ({"tty": False}, "terminal")])
def test_dry_run_yes_and_a_non_tty_run_nothing_and_print_the_command_and_the_undo(kw, word):
    out, runner, confirm = run(**kw)
    assert runner.calls == [] and confirm.asked == []
    assert out.status in (oc.SKIPPED, oc.REFUSED) and not out.ran
    text = "\n".join(out.lines)
    assert word in text and "openclaw channels remove --channel slack --account default" in text and UNDO in text


def test_a_declined_confirm_runs_no_remove_and_still_names_the_command():
    out, runner, confirm = run(confirm=Confirm(False))
    assert runner.removes == [] and out.status == oc.DECLINED
    assert "openclaw channels remove --channel slack --account default" in "\n".join(out.lines)
    assert len(confirm.asked) == 1


def test_the_cockpit_must_be_openclaw():
    out, runner, _c = run(cockpit="claude")
    assert runner.calls == [] and out.status == oc.REFUSED and "OpenClaw cockpit" in out.lines[0]


# --- AC-S12c ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ruleset", [UNREADABLE, None, rules.RuleSet("x", "installed", (("channels", "restart"),)),
                                     rules.RuleSet("x", "installed", (("gateway", "restart"),))])
def test_unreadable_or_not_hot_reload_rules_refuse_with_one_line_and_run_nothing(ruleset):
    out, runner, confirm = run(reload_rules=ruleset)
    assert runner.calls == [] and confirm.asked == []
    assert out.status == oc.REFUSED
    assert "reload rules" in out.lines[0] and len(out.lines) == 2          # the reason, then the undo
    assert UNDO in out.lines[1]


def test_a_per_channel_hot_rule_is_honoured():
    only_slack_hot = rules.RuleSet("x", "installed", (("channels.telegram", "restart"), ("channels", "hot")))
    out, runner, _c = run(reload_rules=only_slack_hot)
    assert out.status == oc.DONE
    out, runner, _c = run(change("telegram", "default"), reload_rules=only_slack_hot)
    assert out.status == oc.REFUSED and runner.calls == []


# --- fresh listing before and after -------------------------------------------------------------------------------------

def test_an_unreadable_fresh_listing_refuses_and_changes_nothing():
    out, runner, confirm = run(runner=Runner([]))
    assert out.status == oc.REFUSED and runner.removes == [] and confirm.asked == []
    assert "nothing was changed" in "\n".join(out.lines)


def test_an_account_no_longer_enabled_in_the_fresh_listing_is_not_touched():
    for fresh in (listing(("telegram", "default")), '{"chat": {"slack": {"accounts": [{"id": "default", '
                                                    '"enabled": false}]}}}'):
        out, runner, confirm = run(runner=Runner([fresh]))
        assert out.status == oc.SKIPPED and runner.removes == [] and confirm.asked == []


def test_a_difference_after_the_change_is_reported():
    after = listing(("telegram", "default"), ("slack", "default"))               # slack is still on
    out, _r, _c = run(runner=Runner([LISTING, after]))
    assert any("still listed as on" in line for line in out.lines)
    other = listing()                                                              # telegram vanished too
    out, _r, _c = run(runner=Runner([LISTING, other]))
    assert any("unexpected: telegram:default is off" in line for line in out.lines)
    out, _r, _c = run(runner=Runner([LISTING]))                                    # the second listing failed
    assert any("could not be read afterwards" in line for line in out.lines)


# --- AC-S12d: nothing but list and remove ever runs ---------------------------------------------------------------------

INV = [oc.ChannelAccount("telegram", "default"), oc.ChannelAccount("slack", "default"),
       oc.ChannelAccount("slack", "ops"), oc.ChannelAccount("matrix", "off", enabled=False)]
KEYS = [a.key for a in INV] + ["ghost:x"]


def test_no_executed_argv_contains_add_delete_or_a_credential_flag_for_any_input():
    forbidden = re.compile(r"(?i)^(add|--delete|--agent|--?(token|bot-?token|secret|password|api-?key|key|credential))")
    executed = 0
    for seen in itertools.chain.from_iterable(itertools.combinations(KEYS, n) for n in range(len(KEYS) + 1)):
        plan = oc.plan_changes(seen, [], INV)
        for item in plan:
            if isinstance(item, oc.PrintOnly):
                continue
            body = "".join(f'"{a.account}"' for a in INV if a.channel == item.channel)
            text = listing(*[(a.channel, a.account) for a in INV if a.enabled])
            runner = Runner([text, text])
            oc.execute(item, runner=runner, confirm=Confirm(True, True), tty=True, dry_run=False, yes=False,
                       cockpit="openclaw", inventory=INV, reload_rules=HOT)
            for argv in runner.argvs:
                executed += 1
                assert argv[:3] in (["openclaw", "channels", "list"], ["openclaw", "channels", "remove"]), argv
                assert not any(forbidden.match(a) for a in argv[3:] if not a.startswith("--channel")
                               and not a.startswith("--account")), argv
    assert executed > 0


def test_a_ticked_disabled_channel_is_print_only_and_cannot_be_executed():
    plan = oc.plan_changes([], ["matrix:off"], INV)
    assert isinstance(plan[0], oc.PrintOnly)
    out = oc.execute(plan[0], runner=Runner([]), confirm=Confirm(True), tty=True, dry_run=False, yes=False,
                     cockpit="openclaw", inventory=INV, reload_rules=HOT)
    assert out.status == oc.REFUSED


@pytest.mark.parametrize("forged", [
    oc.ChannelChange("add", "slack", "default", oc.add_argv("slack", "default"), oc.add_argv("slack", "default")),
    oc.ChannelChange("disable", "slack", "default", oc.remove_argv("slack", "default") + ("--delete",),
                     oc.add_argv("slack", "default")),
    oc.ChannelChange("disable", "slack", "default", oc.remove_argv("slack", "default") + ("--agent", "x"),
                     oc.add_argv("slack", "default")),
    oc.ChannelChange("disable", "slack", "default", oc.remove_argv("slack", "default") + ("--token", "t"),
                     oc.add_argv("slack", "default")),
    oc.ChannelChange("disable", "slack", "default\n", oc.remove_argv("slack", "default\n"), oc.add_argv("slack", "x")),
    oc.ChannelChange("disable", "ghost", "x", oc.remove_argv("ghost", "x"), oc.add_argv("ghost", "x")),
])
def test_a_forged_or_unplanned_change_is_refused_before_anything_runs(forged):
    out, runner, confirm = run(forged)
    assert out.status == oc.REFUSED and runner.calls == [] and confirm.asked == []


# --- AC-S12e ---------------------------------------------------------------------------------------------------------------

def test_a_timeout_is_reported_with_the_undo_and_apply_continues():
    out, runner, _c = run(runner=Runner([LISTING], remove_result=(124, "Command timed out after 60 seconds")))
    assert out.status == oc.FAILED and out.ran
    text = "\n".join(out.lines)
    assert "timed out after 60 s" in text and UNDO in text
    assert len(runner.calls) == 2                       # list, remove: no listing is trusted after a failure


def test_a_failing_remove_is_reported_not_raised():
    boom = Runner([LISTING], remove_result=(1, "boom"))
    out, _r, _c = run(runner=boom)
    assert out.status == oc.FAILED and "failed (exit 1)" in "\n".join(out.lines)

    def raising(argv, **kw):
        if argv[2] == "remove":
            raise OSError("gone")
        return 0, LISTING
    out, _r, _c = run(runner=raising)
    assert out.status == oc.FAILED and "OSError" in "\n".join(out.lines)


# --- AC-S12f ---------------------------------------------------------------------------------------------------------------

def test_a_telegram_untick_needs_a_second_confirm_and_both_default_to_no():
    tg = change("telegram", "default")
    out, runner, confirm = run(tg, confirm=Confirm(True, False))
    assert len(confirm.asked) == 2 and all(default is False for _m, default in confirm.asked)
    assert "Telegram" in confirm.asked[1][0]
    assert runner.removes == [] and out.status == oc.DECLINED
    out, runner, confirm = run(tg, runner=Runner([LISTING, listing(("slack", "default"))]), confirm=Confirm(True, True))
    assert out.status == oc.DONE and len(runner.removes) == 1
    out, runner, confirm = run(change(), confirm=Confirm(True))
    assert len(confirm.asked) == 1                        # slack needs only its own confirm


# --- no secret reaches a log or a print ---------------------------------------------------------------------------------

SECRETS = ["123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", "xoxb-1234567890-abcdefghijkl", "Bearer abcdef0123456789abcdef",
           "token=ABCDEFabcdef0123456789ABCDEFabcdef0123", "sk-" + "a1B2c3D4e5F6" * 4]


def test_no_secret_in_the_command_output_reaches_the_returned_lines_or_the_screen(capsys, monkeypatch):
    noisy = "removing slack\n" + "\n".join(f"debug {s}" for s in SECRETS) + "\n\x1b[31mred\x1b[0m"
    out, _r, _c = run(runner=Runner([LISTING, listing(("telegram", "default"))], remove_result=(1, noisy)))
    blob = "\n".join(out.lines)
    for secret in SECRETS:
        assert secret not in blob
    assert "[redacted]" in blob and len(out.lines) <= 10
    assert max(len(line) for line in out.lines) <= 320 and "\x1b" not in blob
    print(blob)
    seen = capsys.readouterr()
    assert not any(secret in seen.out + seen.err for secret in SECRETS)


def test_redact_is_bounded():
    lines = oc.redact("\n".join(f"line {i} " + "x" * 400 for i in range(50)))
    assert len(lines) == oc.OUTPUT_LINES and all(len(line) <= oc.OUTPUT_WIDTH for line in lines)
    assert oc.redact("") == [] and oc.redact(None) == []


# --- AC-S12g: the invariant that no patch touches channels -----------------------------------------------------------------

def test_assert_channels_safe_is_byte_identical_and_still_refuses_a_channel_enable_patch():
    src = inspect.getsource(host.assert_channels_safe)
    assert hashlib.sha256(src.encode("utf-8")).hexdigest() == PINNED_ASSERT_CHANNELS_SAFE_SHA256
    with pytest.raises(ValueError):
        host.assert_channels_safe({"channels": {"slack": {"enabled": False}}})
    with pytest.raises(ValueError):
        host.assert_channels_safe({"channels": {"telegram": {"enabled": False}}})
    with pytest.raises(ValueError):
        host.assert_channels_safe({}, ["channels.slack"])
    host.assert_channels_safe({"channels": {"telegram": {"streaming": {"mode": "partial"}}}})


def test_the_module_never_builds_an_add_or_patches_channels():
    src = pathlib.Path(oc.__file__).read_text(encoding="utf-8")
    assert "config patch" not in src and "config set" not in src and '"--delete"' in src       # only as a refusal
    assert src.count('"channels", "remove"') == 1


# --- C6: no saved disable intent, stale inventory cleared -------------------------------------------------------------------

def _prompt_state():
    s = state.SetupState()
    return s


def test_the_channel_default_comes_from_the_current_inventory_not_from_saved_intent(monkeypatch):
    from wizard_fakes import Script
    s = _prompt_state()
    s.openclaw.channels_seen = ["telegram:default", "slack:default"]
    s.openclaw.channels_wanted = ["telegram:default"]                # an earlier run unticked slack
    Script(**{"Which channel accounts": lambda values: list(values)}).install(monkeypatch)
    seen = {}
    real = ui.checkbox

    def spy(message, choices, default=None, **kw):
        seen["default"] = list(default or [])
        return real(message, choices, default=default, **kw)
    monkeypatch.setattr(ui, "checkbox", spy)
    channel_ui.prompt(s, runner=Runner([LISTING]))
    assert sorted(seen["default"]) == ["slack:default", "telegram:default"]
    assert sorted(s.openclaw.channels_wanted) == ["slack:default", "telegram:default"]


def test_a_relist_that_comes_back_empty_clears_the_stale_inventory_and_the_plan(monkeypatch):
    from wizard_fakes import Script
    s = _prompt_state()
    Script(**{"Which channel accounts": lambda values: [v for v in values if v != "slack:default"]}).install(monkeypatch)
    channel_ui.prompt(s, runner=Runner([LISTING]))
    assert getattr(s, channel_ui.INVENTORY_ATTR) and channel_ui.review_lines(s)
    channel_ui.prompt(s, runner=Runner([listing()]))                 # nothing listed this time
    assert not hasattr(s, channel_ui.INVENTORY_ATTR) and channel_ui.review_lines(s) == []
    assert s.openclaw.channels_seen == [] and s.openclaw.channels_wanted == []
    channel_ui.prompt(s, runner=Runner([]))                          # unreadable
    assert not hasattr(s, channel_ui.INVENTORY_ATTR)


# --- the Apply step ---------------------------------------------------------------------------------------------------------

def _apply_state(monkeypatch, *, applied=("openclaw",)):
    s = state.SetupState()
    inv = inventory()
    setattr(s, channel_ui.INVENTORY_ATTR, inv)
    s.openclaw.channels_seen = [a.key for a in inv if a.enabled]
    s.openclaw.channels_wanted = ["telegram:default"]                 # slack unticked
    monkeypatch.setattr(wizard, "_APPLIED_TARGETS", list(applied))
    monkeypatch.setattr(host, "default_runner", Runner([LISTING, listing(("telegram", "default"))]))
    monkeypatch.setattr(rules, "current_ruleset", lambda runner, **k: HOT)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": lambda *a, **k: None})())
    for name in ("info", "ok", "warn"):
        monkeypatch.setattr(ui, name, lambda m, *a, **k: None)
    monkeypatch.setattr(ui, "record_action", lambda m: None)
    return s


def test_apply_runs_the_remove_after_the_confirm_and_never_under_yes_or_a_dry_run(monkeypatch):
    s = _apply_state(monkeypatch)
    monkeypatch.setattr(ui, "confirm", lambda m, default=False: True)
    monkeypatch.setattr(wizard, "_ASSUME_YES", False)
    wizard._apply_channel_changes(s)
    assert len(host.default_runner.removes) == 1

    s = _apply_state(monkeypatch)
    monkeypatch.setattr(wizard, "_ASSUME_YES", True)
    wizard._apply_channel_changes(s)
    assert host.default_runner.calls == []

    s = _apply_state(monkeypatch)
    monkeypatch.setattr(wizard, "_ASSUME_YES", False)
    wizard._apply_channel_changes(s, dry_run=True)
    assert host.default_runner.calls == []


def test_apply_without_the_openclaw_cockpit_runs_nothing(monkeypatch):
    s = _apply_state(monkeypatch, applied=("claude",))
    monkeypatch.setattr(ui, "confirm", lambda m, default=False: True)
    monkeypatch.setattr(wizard, "_ASSUME_YES", False)
    wizard._apply_channel_changes(s)
    assert host.default_runner.calls == []


def test_the_intent_is_consumed_at_apply_and_never_saved():
    src = pathlib.Path(wizard.__file__).read_text(encoding="utf-8")
    assert src.count("s.openclaw.channels_seen, s.openclaw.channels_wanted = [], []") >= 2
    assert src.index("_apply_channel_changes(s)\n    s.openclaw.channels_seen") < src.index(
        'state.save(s)\n    ui.record_action("saved setup state")')


@pytest.mark.parametrize("text,secret", [
    ('{"password": "hunter2"}', "hunter2"),
    ("{'password':'hunter2'}", "hunter2"),
    ('{"botToken": "abc def"}', "abc def"),
    ("accessToken=s3cr3tvalue", "s3cr3tvalue"),
    ("SLACK_BOT_TOKEN=xyz123", "xyz123"),
    ("client_secret: topsecretvalue", "topsecretvalue"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ('"authorization": "Bearer abcdef123"', "abcdef123"),
    ("Authorization: Bearer abcdef123", "abcdef123"),
    ("sent header Bearer abcdef123 to host", "abcdef123"),
    ("api_key = 'my key value'", "my key value"),
])
def test_redact_hides_the_value_after_a_quoted_or_suffixed_secret_key(text, secret):
    blob = " ".join(oc.redact(text))
    assert secret not in blob and "[redacted]" in blob


def test_redact_keeps_ordinary_lines():
    assert oc.redact("channel telegram:default removed") == ["channel telegram:default removed"]


# --- advisory: a capped disable output re-reads the list before FAILED vs succeeded -------------------------------------

CAPPED = "x" * oc.MAX_BYTES


def test_a_capped_remove_output_is_judged_by_a_fresh_listing_and_reported_done_when_the_account_is_off():
    runner = Runner([LISTING, listing(("telegram", "default"))], remove_result=(1, CAPPED))
    out, runner, _c = run(runner=runner)
    assert out.status == oc.DONE and out.ran
    assert runner.argvs[-1] == ["openclaw", "channels", "list", "--all", "--json"]
    assert [a for a in runner.argvs if a[2:3] == ["list"]].__len__() == 2
    text = "\n".join(out.lines)
    assert "output exceeded the cap" in text and "turned off" in text and UNDO in text


def test_a_capped_remove_output_is_failed_when_the_account_is_still_on():
    runner = Runner([LISTING, LISTING], remove_result=(1, CAPPED))
    out, runner, _c = run(runner=runner)
    assert out.status == oc.FAILED and out.ran
    assert "still listed as on" in "\n".join(out.lines)


def test_a_capped_remove_output_is_failed_when_the_listing_cannot_be_read():
    runner = Runner([LISTING], remove_result=(1, CAPPED))                       # the second listing answers rc 1
    out, runner, _c = run(runner=runner)
    assert out.status == oc.FAILED and "could not be read" in "\n".join(out.lines)


def test_an_ordinary_failed_remove_does_not_re_read_the_list():
    runner = Runner([LISTING], remove_result=(1, "boom"))
    out, runner, _c = run(runner=runner)
    assert out.status == oc.FAILED and len([a for a in runner.argvs if a[2:3] == ["list"]]) == 1


def test_redact_of_a_huge_single_word_is_fast_and_bounded():
    import time
    start = time.monotonic()
    out = oc.redact("x" * oc.MAX_BYTES)
    assert time.monotonic() - start < 5 and len(out) == 1 and len(out[0]) <= oc.OUTPUT_WIDTH


@pytest.mark.parametrize("line,hidden", [
    ("Cookie: session=abc123; theme=dark", ["abc123", "dark"]),
    ("Set-Cookie: sid=zzz999; Path=/; HttpOnly", ["zzz999"]),
    ('"cookie": "sid=qqq111"', ["qqq111"]),
    ("fetch https://admin:hunter2@example.com/path failed", ["hunter2", "admin"]),
    ("fetch postgres://svc:pw-0@db:5432/x failed", ["pw-0"]),
    ("clone https://ghp_tokenvalue@github.com/o/r", ["ghp_tokenvalue"]),
    ("using key sk-ant-api03-abcdefghij", ["abcdefghij"]),
    ("OPENAI sk-proj-AAAA1111bbbb", ["AAAA1111bbbb"]),
    ("connect auth=opensesame now", ["opensesame"]),
    ("auth_token: xyz-secret-1", ["xyz-secret-1"]),
])
def test_redact_masks_cookies_url_credentials_bare_keys_and_auth(line, hidden):
    out = "\n".join(oc.redact(line))
    assert "[redacted]" in out
    for word in hidden:
        assert word not in out, (line, out)


@pytest.mark.parametrize("line", [
    "author: Ada Lovelace", "https://example.com/path?x=1", "cookie jar is empty today", "ask-me-anything skill",
    "slack / default: enabled", "mailto:ada@example.com",
])
def test_redact_keeps_lines_without_those_secrets(line):
    assert oc.redact(line) == [line]
