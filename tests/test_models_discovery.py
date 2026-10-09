"""Per-provider discovery: only enabled and credentialed providers are asked (httpx.MockTransport for HTTP)."""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import httpx
import pytest

from ai_resources import model_accounts as ma
from ai_resources import models
from ai_resources.selection import ProviderSel, Selection, SlotSel

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"
SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def _catalog(name):
    return (FIX / name).read_text()


def selection(*providers, slots=None, vendor=()):
    s = Selection(shape="multi-provider")
    for p in providers:
        s.providers[p] = ProviderSel(enabled=True, vendor_listing=p in vendor)
    for slot, ref in (slots or {}).items():
        s.slots[slot] = SlotSel(ref=ref)
    return s


def accounts(**creds):
    return {p: ma.Account(credentialed=ok, sources=["env-file"] if ok else [], direct_key=ok) for p, ok in creds.items()}


class Runner:
    def __init__(self, files=None, fail=(), slow=None, clock=None):
        self.calls = []
        self.files = files or {"claude-cli": "claude-cli-catalog.json", "google": "catalog-google.json",
                                "openai": "catalog-openai.json"}
        self.fail, self.slow, self.clock = set(fail), slow, clock

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if "refresh" in argv:
            return 0, "updated"
        provider = argv[argv.index("--provider") + 1]
        if self.slow and self.clock is not None:
            self.clock[0] += self.slow
        if provider in self.fail:
            return 1, "boom"
        return 0, _catalog(self.files[provider])


def test_only_enabled_credentialed_providers_are_asked():
    sel = selection("anthropic", "google", slots={"anthropic:sonnet": "anthropic/claude-sonnet-5",
                                                    "google:gemini-flash": "google/gemini-3.7-flash"})
    run = Runner()
    d = models.discover(run, selection=sel, accounts=accounts(anthropic=True, google=True, openai=True))
    assert sum("refresh" in a for a in run.calls) == 1
    lists = [a[a.index("--provider") + 1] for a in run.calls if "list" in a]
    assert sorted(lists) == ["claude-cli", "google"]
    rows = {r.provider: r.text for r in d.providers}
    assert rows["openai"] == "skipped: not enabled"
    assert d.others["google:gemini-flash"] == "gemini-3.8-flash"
    assert d.best["sonnet"] == "claude-sonnet-5-5"


def test_an_uncredentialed_provider_is_skipped_without_a_call():
    sel = selection("google", slots={"google:gemini-flash": "google/gemini-3.7-flash"})
    run = Runner()
    d = models.discover(run, selection=sel, accounts=accounts(google=False))
    assert not [a for a in run.calls if "list" in a]
    assert {r.provider: r.text for r in d.providers}["google"] == "skipped: no credentials"


def test_one_failing_provider_does_not_hide_the_others():
    sel = selection("anthropic", "google", slots={"anthropic:sonnet": "anthropic/claude-sonnet-5",
                                                    "google:gemini-flash": "google/gemini-3.7-flash"})
    d = models.discover(Runner(fail={"google"}), selection=sel, accounts=accounts(anthropic=True, google=True))
    rows = {r.provider: r.text for r in d.providers}
    assert rows["google"].startswith("discovery failed")
    assert d.best["sonnet"] == "claude-sonnet-5-5"        # AC-S6c: anthropic proposals still exist


def test_every_provider_failing_raises():
    sel = selection("anthropic", "google", slots={"anthropic:sonnet": "anthropic/claude-sonnet-5",
                                                    "google:gemini-flash": "google/gemini-3.7-flash"})
    with pytest.raises(models.DiscoveryError):
        models.discover(Runner(fail={"google", "claude-cli"}), selection=sel,
                        accounts=accounts(anthropic=True, google=True))


def test_the_budget_is_per_run_not_per_provider():
    clock = [0.0]
    sel = selection("anthropic", "google", "openai", slots={"anthropic:sonnet": "anthropic/claude-sonnet-5",
                                                              "google:gemini-flash": "google/gemini-3.7-flash",
                                                              "openai:gpt-terra": "openai/gpt-5.6-terra"})
    run = Runner(slow=60, clock=clock)
    d = models.discover(run, selection=sel, accounts=accounts(anthropic=True, google=True, openai=True),
                        monotonic=lambda: clock[0], budget=100)
    rows = {r.provider: r.text for r in d.providers}
    assert rows["anthropic"].endswith("listed") and rows["google"].endswith("listed")
    assert rows["openai"] == "discovery failed: budget"
    assert len([a for a in run.calls if "list" in a]) == 2


def test_google_catalog_yields_previews_and_new_families():
    sel = selection("google", slots={"google:gemini-pro": "google/gemini-3.1-pro-preview",
                                       "google:gemini-flash": "google/gemini-3.7-flash"})
    d = models.discover(Runner(), selection=sel, accounts=accounts(google=True))
    assert d.others["google:gemini-flash"] == "gemini-3.8-flash"
    assert d.previews["google:gemini-pro"] == "gemini-3.1-pro-preview"
    assert d.others["google:gemini-pro"] == "gemini-2.5-pro"         # a preview is never "best"
    assert "google/gemma-4-31b-it" in d.new_families


def _http(handler):
    return models.make_http(httpx.MockTransport(handler))


def test_vendor_listing_is_off_unless_the_user_opted_in():
    seen = []
    http = _http(lambda req: seen.append(req) or httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]}))
    sel = selection("deepseek", slots={"deepseek:deepseek-pro": "deepseek/deepseek-v4-pro"})
    d = models.discover(Runner(), selection=sel, accounts=accounts(deepseek=True), http=http,
                        key_for=lambda p: SENTINEL)
    assert seen == []
    assert {r.provider: r.text for r in d.providers}["deepseek"] == "static list, report-only"


def test_vendor_listing_sends_the_key_only_in_the_header_to_the_constant_host():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}, {"id": "deepseek-v5-pro"}]})

    sel = selection("deepseek", slots={"deepseek:deepseek-pro": "deepseek/deepseek-v4-pro"}, vendor=("deepseek",))
    d = models.discover(Runner(), selection=sel, accounts=accounts(deepseek=True), http=_http(handler),
                        key_for=lambda p: SENTINEL)
    [req] = seen
    assert req.url.host == "api.deepseek.com" and str(req.url) == "https://api.deepseek.com/models"
    assert req.headers["authorization"] == f"Bearer {SENTINEL}"
    assert SENTINEL not in str(req.url) and SENTINEL.encode() not in req.content
    assert d.others["deepseek:deepseek-pro"] == "deepseek-v5-pro"
    blob = json.dumps([r.as_dict() for r in d.providers]) + repr(d)
    assert SENTINEL not in blob


def test_a_vendor_http_error_is_a_failed_row_without_the_key():
    sel = selection("openai", "google", slots={"openai:gpt-terra": "openai/gpt-5.6-terra",
                                                 "google:gemini-flash": "google/gemini-3.7-flash"}, vendor=("openai",))
    d = models.discover(Runner(), selection=sel, accounts=accounts(openai=True, google=True),
                        http=_http(lambda req: httpx.Response(401, text="bad key")), key_for=lambda p: SENTINEL)
    rows = {r.provider: r.text for r in d.providers}
    assert rows["openai"].startswith("discovery failed") and SENTINEL not in rows["openai"]
    assert rows["google"].endswith("listed")


def test_no_selection_is_the_original_claude_only_discovery():
    run = Runner()
    d = models.discover(run)
    assert d.best["opus"] == "claude-opus-5-5" and not d.others
    assert [a[a.index("--provider") + 1] for a in run.calls if "list" in a] == ["claude-cli"]


def test_an_empty_catalog_entry_with_no_vendor_opt_in_is_report_only():
    sel = selection("openai", slots={"openai:gpt-terra": "openai/gpt-5.6-terra"})
    d = models.discover(Runner(), selection=sel, accounts=accounts(openai=True))
    assert {r.provider: r.text for r in d.providers}["openai"] == "static list, report-only"
