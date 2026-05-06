"""Post-setup smoke tests — round-trip each configured model through the gateway."""
from __future__ import annotations

from . import litellm


def run_all(executors: dict, gateway_url: str, master_key: str) -> list[tuple[str, bool, str]]:
    """Run smoke tests across all configured models. Returns [(label, ok, msg)]."""
    results: list[tuple[str, bool, str]] = []

    if not master_key:
        results.append(("Master key present", False, "LITELLM_MASTER_KEY missing"))
        return results

    # Health check
    ok = litellm.health_check(gateway_url.rstrip("/") + "/health/liveliness")
    results.append((f"Gateway health  {gateway_url}", ok, "" if ok else "endpoint unreachable"))
    if not ok:
        return results

    # Per-model round-trip
    seen: set[str] = set()
    for role, cfg in executors.get("by_role", {}).items():
        model = cfg.get("model", "")
        if not model or model in seen:
            continue
        seen.add(model)
        ok, msg = litellm.smoke_test_model(model, gateway_url, master_key)
        results.append((f"Model round-trip  {model}", ok, msg))

    return results
