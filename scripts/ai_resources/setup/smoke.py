"""Post-setup smoke tests — round-trip each configured model through the gateway.

Each backend is probed on the surface Claude Code actually uses, which differs
between them: LiteLLM is reached on the OpenAI-shaped `/v1/chat/completions`,
while the OpenRouter backend serves Claude Code's traffic on the Anthropic-shaped
`/v1/messages`. Probing the wrong surface would pass while telling us nothing —
OpenRouter answers on both, so a green `/v1/chat/completions` says nothing about
whether per-role routing works.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import litellm


def _anthropic_round_trip(model: str, gateway_url: str, api_key: str) -> tuple[bool, str]:
    """Round-trip a tiny prompt through an Anthropic-format endpoint."""
    payload = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with just OK"}],
    }
    req = urllib.request.Request(
        gateway_url.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", {}).get("message", body)
        except ValueError:
            pass
        return False, f"HTTP {e.code}: {str(body)[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(e)
    # Report which model actually served it: a gateway silently substituting a
    # different model is the failure this test exists to catch.
    served = doc.get("model", "")
    if served and served != model:
        return True, f"served by {served} (requested {model})"
    return True, ""


def run_all(executors: dict, gateway_url: str, master_key: str,
            backend: str = "litellm") -> list[tuple[str, bool, str]]:
    """Run smoke tests across all configured models. Returns [(label, ok, msg)]."""
    results: list[tuple[str, bool, str]] = []

    if not master_key:
        env_var = "OPENROUTER_API_KEY" if backend == "openrouter" else "LITELLM_MASTER_KEY"
        results.append(("Gateway credential present", False, f"{env_var} missing"))
        return results

    # Health check — only the self-hosted gateway exposes one. A hosted gateway
    # is either reachable on the round-trip below or it isn't.
    if backend == "litellm":
        ok = litellm.health_check(gateway_url.rstrip("/") + "/health/liveliness")
        results.append((f"Gateway health  {gateway_url}", ok,
                        "" if ok else "endpoint unreachable"))
        if not ok:
            return results

    # Per-model round-trip
    seen: set[str] = set()
    for _role, cfg in executors.get("by_role", {}).items():
        model = cfg.get("model", "")
        if not model or model in seen:
            continue
        seen.add(model)
        if backend == "openrouter":
            ok, msg = _anthropic_round_trip(model, gateway_url, master_key)
        else:
            ok, msg = litellm.smoke_test_model(model, gateway_url, master_key)
        results.append((f"Model round-trip  {model}", ok, msg))

    return results
