"""Opt-in probe: does the configured `agents.defaults.utilityModel` work with this host's key? (ADR-0014 D5)

OpenClaw runs titles and progress notes on `agents.defaults.utilityModel`. Unset, it derives an older Haiku that an
Anthropic workspace key rejects with a 400. The probe is one tiny model turn through the local CLI transport
(`openclaw infer model run --local`, which does not go through the gateway), billed like any other call, so it is never
run unless the operator opts in and the cost is printed first. Tests inject the runner and never run it.

The answer is a classification, not a guess: `ok`, `invalid` (the key is not valid for that model: create the key inside
the workspace) or `error` (the probe could not tell). A replacement model is proposed only when a probe VERIFIED it.
No secret is read or printed: the CLI uses its own credentials and the output is classified, not echoed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

PROMPT = "Reply with exactly OK"
NOTICE = ("The utility-model probe runs one tiny model turn per model it checks ('Reply with exactly OK', through the "
          "local CLI, not the gateway). Each is billed like any other call on your account (a few tokens).")
INVALID_MESSAGE = "key not valid for this model, create the key inside the workspace"

_INVALID = re.compile(r"\b400\b|invalid[_ ]request|invalid[_ ]api[_ ]key|not[_ ]valid|model[_ ]not[_ ]found|"
                      r"permission[_ ]denied|not authorized|unauthori[sz]ed|\b401\b|\b403\b|\b404\b", re.IGNORECASE)

Runner = Callable[[list[str]], "tuple[int, str]"]


@dataclass(frozen=True)
class Result:
    ref: str
    status: str            # "ok" | "invalid" | "error"
    detail: str = ""


def command(ref: str) -> list[str]:
    return ["infer", "model", "run", "--local", "--model", ref, "--prompt", PROMPT, "--json"]


def classify(ref: str, rc: int, out: str) -> Result:
    text = out or ""
    if rc == 0 and re.search(r"\bOK\b", text):
        return Result(ref, "ok")
    if _INVALID.search(text):
        return Result(ref, "invalid", INVALID_MESSAGE)
    if rc == 0:
        return Result(ref, "error", "the model answered, but not with OK")
    return Result(ref, "error", "the probe could not tell (the CLI failed)")


def probe(ref: str, run: Runner) -> Result:
    try:
        rc, out = run(command(ref))
    except Exception as e:  # noqa: BLE001 - a probe never raises into setup or doctor
        return Result(ref, "error", f"the probe could not run ({e.__class__.__name__})")
    return classify(ref, rc, out)


def first_verified(candidates: list[str], run: Runner, *, skip: str = "") -> Result | None:
    """The first candidate (other than `skip`) the probe answers OK for; None when none does."""
    for ref in candidates:
        if ref == skip:
            continue
        result = probe(ref, run)
        if result.status == "ok":
            return result
    return None


def candidates() -> list[str]:
    """Refs to try as a replacement, cheapest first: the pinned Haiku, then the pinned Sonnet."""
    from . import model_pins
    pins = model_pins.effective()
    return [f"anthropic/{pins['haiku']}", f"anthropic/{pins['sonnet']}"]
