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

import json
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


def _answer_text(out: str) -> "str | None":
    """The model's reply from the CLI's JSON (`outputs[].text`), found even when log lines surround it; None when the
    output holds no such document. A log line on stderr that happens to contain "OK" is not an answer."""
    dec = json.JSONDecoder()
    i = out.find("{")
    while i >= 0:
        try:
            obj, end = dec.raw_decode(out, i)
        except ValueError:
            i = out.find("{", i + 1)
            continue
        outputs = obj.get("outputs") if isinstance(obj, dict) else None
        if isinstance(outputs, list):
            return " ".join(str(o.get("text") or "") for o in outputs if isinstance(o, dict))
        i = out.find("{", end)
    return None


_SECRET = re.compile(r"\b(?:AIza|sk-|ya29\.)[\w.\-]{8,}|\bAQ\.[\w.\-]*|\boc-[\w\-]{6,}"
                     r"|\bBearer\s+\S+|\bsk-ant[\w\-]*|\b[A-Za-z0-9_\-]{32,}\b")


def _first_error_line(out: str) -> str:
    for line in (out or "").splitlines():
        line = line.strip()
        if line and not line.startswith("[") and "{" not in line[:1]:
            return _SECRET.sub("<redacted>", line)[:160]
    return ""


def failure_detail(ref: str, rc: int, out: str) -> str:
    """Why the CLI failed, in words that name the cause and the one command that finishes or shows it."""
    low = (out or "").lower()
    if rc == 127:
        return "the OpenClaw CLI is not installed or not on PATH; install it, then re-run `ai-resources setup`"
    if "offline maintenance" in low:
        return ("OpenClaw state is in an offline maintenance window, so the probe could not run; "
                "re-run `ai-resources setup` when it ends")
    if "timed out" in low:
        return f"the probe timed out; run `openclaw infer model run --local --model {ref} --prompt OK --json` to retry"
    line = _first_error_line(out)
    cmd = f"openclaw infer model run --local --model {ref} --prompt 'Reply with exactly OK' --json"
    return (f"the CLI failed (exit {rc}): {line}; run `{cmd}` to see it" if line
            else f"the CLI failed (exit {rc}) without a message; run `{cmd}` to see it")


def classify(ref: str, rc: int, out: str) -> Result:
    text = out or ""
    answer = _answer_text(text) if rc == 0 else None
    if rc == 0 and re.search(r"\bOK\b", answer if answer is not None else text):
        return Result(ref, "ok")
    if rc == 127 or "offline maintenance" in text.lower() or "timed out" in text.lower():
        return Result(ref, "error", failure_detail(ref, rc, text))
    if _INVALID.search(text):
        return Result(ref, "invalid", INVALID_MESSAGE)
    if rc == 0:
        return Result(ref, "error", "the model answered, but not with OK")
    return Result(ref, "error", failure_detail(ref, rc, text))


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
