"""Account detection merges the kit's env file with OpenClaw's auth status without reading key material."""
from __future__ import annotations

import json
import pathlib

from ai_resources import model_accounts as ma

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"
SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def _status():
    return json.loads((FIX / "models-status.json").read_text())


def _deps(env=None, status=True, **kw):
    return ma.Deps(env=lambda: dict(env or {}), status=(lambda: _status()) if status is True else status, **kw)


def test_google_openrouter_and_anthropic_are_credentialed_with_only_an_openrouter_env_key():
    got = ma.detect(_deps({"OPENROUTER_API_KEY": "x"}))
    for pid in ("google", "openrouter", "anthropic"):
        assert got[pid].credentialed and got[pid].reason, pid
    for pid in ("openai", "deepseek", "moonshot"):
        assert not got[pid].credentialed and got[pid].reason == "no credentials", pid


def test_sources_are_labelled_per_store():
    got = ma.detect(_deps({"OPENROUTER_API_KEY": "x"}))
    assert got["google"].sources == ["openclaw-env"] and not got["google"].direct_key
    assert "env-file" in got["openrouter"].sources and got["openrouter"].direct_key
    assert got["anthropic"].sources == ["synthetic"]


def test_the_sentinel_never_reaches_any_rendering():
    got = ma.detect(_deps({"OPENROUTER_API_KEY": SENTINEL}))
    assert SENTINEL in (FIX / "models-status.json").read_text()      # the fixture does carry it
    blob = ma.to_json(got) + ma.render_text(got) + "\n".join(ma.disagreements(got))
    assert SENTINEL not in blob


def test_the_allowlist_drops_unknown_and_secret_fields():
    out = ma.allowlist_status(_status())
    assert set(out["google"]) == {"kind", "profiles", "source"}
    assert SENTINEL not in json.dumps(out)
    hostile = {"auth": {"providers": [{"provider": "openai", "apiKey": "sk-leak", "effective": {"kind": "env", "detail": "sk-leak"},
                                       "env": {"value": "sk-leak", "source": "env: OPENAI_API_KEY"}, "profiles": {"count": 0}}]}}
    assert "sk-leak" not in json.dumps(ma.allowlist_status(hostile))


def test_vertex_needs_all_three_vars_and_the_adc_file():
    only_project = ma.detect(_deps({"GOOGLE_CLOUD_PROJECT": "p"}))
    assert not only_project["vertex"].credentialed and "GOOGLE_CLOUD_LOCATION" in only_project["vertex"].reason
    env = {"GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_CLOUD_LOCATION": "global", "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json"}
    assert not ma.detect(_deps(env, file_exists=lambda p: False))["vertex"].credentialed
    assert ma.detect(_deps(env, file_exists=lambda p: True))["vertex"].sources == ["adc"]


def test_ollama_needs_its_endpoint_to_answer():
    assert not ma.detect(_deps(ollama_up=lambda: False))["ollama"].credentialed
    assert ma.detect(_deps(ollama_up=lambda: True))["ollama"].sources == ["local"]


def test_a_failing_status_call_falls_back_to_the_env_file_with_a_reason():
    def boom():
        raise RuntimeError("rc 1")
    got = ma.detect(_deps({"GEMINI_API_KEY": "x"}, status=boom))
    assert got["google"].credentialed and got["google"].sources == ["env-file"]
    assert not got["openai"].credentialed and "openclaw status unavailable" in got["openai"].reason


def test_disagreements_are_reported_as_info():
    lines = ma.disagreements(ma.detect(_deps({"OPENAI_API_KEY": "x"})))
    assert any(l.startswith("openai: key in the env file but not known to OpenClaw") for l in lines)
    assert any(l.startswith("google: known to OpenClaw but no key") for l in lines)
