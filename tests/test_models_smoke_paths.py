"""One probe per path (litellm, openrouter, direct). HTTP is a MockTransport; no network, no real key."""
from __future__ import annotations

import json

import httpx

from ai_resources import model_accounts as ma
from ai_resources import models
from ai_resources.selection import ProviderSel, Selection, SlotSel

SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def http_for(handler):
    return models.make_http(httpx.MockTransport(handler))


def chat(model, text="OK"):
    return {"model": model, "choices": [{"message": {"content": text}}]}


def test_litellm_passes_only_when_the_served_model_is_the_requested_one():
    http = http_for(lambda req: httpx.Response(200, json=chat(json.loads(req.content)["model"])))
    ok = models.smoke_slot("google", "gemini-3.8-flash", "litellm", http=http, gateway_url="http://gw.invalid", gateway_key="k")
    assert ok.ok
    swapped = http_for(lambda req: httpx.Response(200, json=chat("gemini/another-model")))
    bad = models.smoke_slot("google", "gemini-3.8-flash", "litellm", http=swapped, gateway_url="http://gw.invalid", gateway_key="k")
    assert not bad.ok and "served model" in bad.reason


def test_litellm_asks_the_gateway_for_the_canonical_vendor_model_name():
    seen = []
    http = http_for(lambda req: seen.append(json.loads(req.content)) or httpx.Response(200, json=chat("google/gemini-3.8-flash")))
    models.smoke_slot("google", "gemini-3.8-flash", "litellm", http=http, gateway_url="http://gw.invalid", gateway_key="k")
    models.smoke_slot("anthropic", "claude-sonnet-5-5", "litellm", http=http, gateway_url="http://gw.invalid", gateway_key="k")
    assert [b["model"] for b in seen] == ["google/gemini-3.8-flash", "claude-sonnet-5-5"]


def test_openrouter_uses_the_namespaced_id_and_a_bearer_key():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=chat(json.loads(req.content)["model"]))
    http = http_for(handler)
    assert models.smoke_slot("moonshot", "kimi-k2.7-code", "openrouter", http=http, key=SENTINEL).ok
    assert models.smoke_slot("anthropic", "claude-haiku-4-5", "openrouter", http=http, key=SENTINEL).ok
    assert [json.loads(r.content)["model"] for r in seen] == ["moonshotai/kimi-k2.7-code", "anthropic/claude-haiku-4.5"]
    assert all(r.url.host == "openrouter.ai" and r.headers["authorization"] == f"Bearer {SENTINEL}" for r in seen)


def test_direct_google_uses_the_header_key_and_the_constant_host():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})
    res = models.smoke_slot("google", "gemini-3.8-flash", "direct", http=http_for(handler), key=SENTINEL)
    [req] = seen
    assert res.ok and req.url.host == "generativelanguage.googleapis.com"
    assert req.headers["x-goog-api-key"] == SENTINEL and SENTINEL not in str(req.url)


def test_direct_openai_compatible_vendors_use_their_own_host():
    hosts = []
    http = http_for(lambda req: hosts.append(str(req.url)) or httpx.Response(200, json=chat("deepseek-v4-pro")))
    assert models.smoke_slot("deepseek", "deepseek-v4-pro", "direct", http=http, key=SENTINEL).ok
    assert hosts == ["https://api.deepseek.com/chat/completions"]


def test_a_401_fails_the_probe_and_the_key_appears_nowhere():
    http = http_for(lambda req: httpx.Response(401, text=f"invalid key {SENTINEL}"))
    for provider, path in (("google", "direct"), ("openai", "direct"), ("deepseek", "direct")):
        res = models.smoke_slot(provider, "x-1", path, http=http, key=SENTINEL)
        assert not res.ok and res.reason == "HTTP 401"
        assert SENTINEL not in res.reason + res.excerpt


def test_a_path_the_adapter_cannot_probe_never_calls_http():
    calls = []
    http = http_for(lambda req: calls.append(req) or httpx.Response(200, json={}))
    assert models.smoke_slot("vertex", "claude-opus-5", "direct", http=http, key="k").reason == "no smoke path"
    assert models.smoke_slot("google", "gemini-3.8-flash", "claude-cli", http=http).reason == "no smoke path"
    assert calls == []


def test_an_openclaw_only_credential_cannot_use_the_direct_path():
    sel = Selection(providers={"google": ProviderSel()}, slots={"google:gemini-flash": SlotSel(ref="google/gemini-3.7-flash")},
                    smoke_path="direct")
    only_openclaw = {"google": ma.Account(credentialed=True, sources=["openclaw-env"], direct_key=False)}
    with_key = {"google": ma.Account(credentialed=True, sources=["env-file"], direct_key=True)}
    assert models.smoke_path_problem("google", sel, only_openclaw) == "no smoke path"
    assert models.smoke_path_problem("google", sel, with_key) == ""
