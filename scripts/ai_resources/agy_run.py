"""`ai-resources agy run --role R`: run an Antigravity role from a plain shell or a Claude Code session (ADR-0019 C2).

The primary way to reach an agy role is OpenClaw `sessions_spawn` on a `kit-role-*` agent. This wrapper is the second path, for a
session that has no `sessions_spawn`: it resolves the role through the routing matrix (`route`), refuses when the role does not
resolve to an agy cell, runs `agy -p` in the checkout with the same privileges as any other writer (no worktree, no sandbox),
and answers with the report contract (`orchestrator.reply.return_block`). The argv and the stream-json parsing are the ones of
`openclaw-plugin/ai-resources/index.js` (`buildAgyBackend`, `parseAgyLine`), ported once; the JS stays as it is.

Exit codes: 0 ran (the JSON carries the contract, a `Blocked: true` reply is still 0), 75 the matrix fallback must run instead
(`cause` + the next `fallback` cell are in the JSON and in the dispatch ledger), 3 the route is blocked, 2 refused or bad input,
1 agy failed without a recorded fallback cause. Scope: a write role takes a `scope_guard` claim first (unless its run already
holds one) and the paths it changed outside the claim are logged on the dispatch row afterwards (advisory, never blocking).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import dispatch_ledger, pool_usage, scope_guard
from .orchestrator import limits, reply as reply_mod
from .orchestrator.state import RunStore, StateError
from .scoring import routing

EXIT_FALLBACK = 75
DEFAULT_TIMEOUT = 1800
AGY_FLAGS = ("--dangerously-skip-permissions", "--disable-slash-commands", "--output-format", "stream-json")


def agy_bin() -> str:
    """AI_RESOURCES_AGY_BIN, else `agy` on PATH or in ~/.local/bin (the gateway's PATH may not see it); '' when absent."""
    return os.environ.get("AI_RESOURCES_AGY_BIN") or shutil.which("agy") or \
        (str(p) if (p := Path.home() / ".local" / "bin" / "agy").is_file() else "")


def argv_for(binary: str, model: str, prompt: str) -> list[str]:
    return [binary, *AGY_FLAGS, *(["--model", model] if model else []), "-p", prompt]


def parse_stream(out: str) -> dict[str, Any]:
    """The text, usage and error of an agy `stream-json` output (`parseAgyLine` of the plugin, folded)."""
    text: list[str] = []
    res: dict[str, Any] = {"text": "", "tokens_in": 0, "tokens_out": 0, "error": "", "conversation": ""}
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "init":
            res["conversation"] = str(ev.get("conversation_id") or "")
        step = ev.get("step_update")
        if isinstance(step, dict) and step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
            text.append(step["text_delta"])
        final = ev.get("result")
        if isinstance(final, dict):
            usage = final.get("usage") or {}
            res["tokens_in"], res["tokens_out"] = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
            res["text"] = str(final.get("response") or "").strip()
            err = final.get("error")
            if final.get("status") not in (None, "SUCCESS") or err:
                res["error"] = str((err.get("message") if isinstance(err, dict) else err) or f"agy status {final.get('status')}")
    res["text"] = res["text"] or "".join(text).strip()
    return res


def _changed(cwd: str) -> set[str] | None:
    """Repo-relative paths git reports as modified or untracked; None outside a git checkout."""
    try:
        p = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-uall", "--no-renames"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return {line[3:].strip().strip('"') for line in p.stdout.splitlines() if len(line) > 3} if p.returncode == 0 else None


def _live(run_id: str) -> bool:
    """Whether a claim holder still works: an engine run that is active, or an `agy-<pid>` one-shot whose process lives."""
    if run_id.startswith("agy-") and run_id[4:].isdigit():
        try:
            os.kill(int(run_id[4:]), 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:                   # EPERM: the process exists
            return True
    try:
        return RunStore().load(run_id).get("status") in limits.ACTIVE
    except (StateError, OSError):
        return False


def _run_agy(argv: list[str], cwd: str, timeout: float) -> tuple[str, str, int | None]:
    """(stdout, stderr, returncode); returncode None = timed out and the process group was killed."""
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                            stdin=subprocess.DEVNULL)
    try:
        out, err = proc.communicate(timeout=timeout)
        return out, err, proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return out or "", err or "", None


def _usage(path: str) -> dict[str, float | None]:
    try:
        if path:
            return {p: (None if v is None else float(v)) for p, v in json.loads(Path(path).read_text("utf-8")).items()}
        return pool_usage.usage_map(pool_usage.snapshot())
    except (OSError, ValueError):
        return {}                         # unreadable usage never blocks the call: the pool is then treated as unknown


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    prompt = Path(args.prompt_file).read_text("utf-8") if args.prompt_file else args.prompt
    if not (prompt or "").strip():
        return 2, {"error": "a prompt is required (--prompt or --prompt-file)"}
    run_id = args.run or f"agy-{os.getpid()}"
    writers = dispatch_ledger.writer_families(args.run) if args.run else []
    try:
        route = routing.resolve(args.role, _usage(args.usage_file), writers)
        role = routing.merged()[args.role]
    except routing.RoutingError as e:
        return 2, {"error": str(e)}
    doc: dict[str, Any] = {"role": args.role, "run": run_id, "route": route.cell, "pool": route.pool, "cause": route.cause}
    if route.blocked:
        return 3, {**doc, "blocked": True, "reason": route.reason}
    if route.backend != "agy":
        return 2, {**doc, "refused": f"{args.role} resolves to {route.cell} [{route.pool}], not an agy cell"
                   + (f" (cause: {route.cause})" if route.cause else "") + "; run it on that route", "fallbacks": list(route.fallbacks)}

    started, fallback = time.time(), route.fallbacks[0] if route.fallbacks else None

    def finish(code: int, outcome: str, cause: str = "", **row: Any) -> tuple[int, dict[str, Any]]:
        dispatch_ledger.record(args.role, outcome, run_id=run_id, step=args.step or "", cell=route.cell, fallback_cause=cause,
                               fallback_from=route.cell if cause else "", duration_s=time.time() - started,
                               writer_families=writers, **row)
        return code, {**doc, "outcome": outcome, **({"cause": cause, "fallback": fallback} if cause else {}),
                      "duration_s": round(time.time() - started, 1)}

    binary = agy_bin()
    if not binary:
        return finish(EXIT_FALLBACK, "fail", "agy-missing")

    scopes = [] if not role.writes else (args.scope or [scope_guard.ROOT])
    took = False
    if scopes and not scope_guard.held(cwd, run_id):
        conflicts = scope_guard.claim(cwd, run_id, scopes, _live)
        if conflicts:
            return 2, {**doc, "refused": f"write scope held by {'; '.join(conflicts)}"}
        took = True
    run_dir = f".agent-output/{run_id}/{args.role}"
    Path(cwd, run_dir).mkdir(parents=True, exist_ok=True)
    full = (f"{prompt.rstrip()}\n\nWorking directory (stay inside it): {cwd}\n"
            f"Write your report and scratch files under {run_dir}/ (this run's own directory).\n"
            f"End your final reply with exactly this block:\n{reply_mod.return_block(args.role)}")
    before = _changed(cwd)
    try:
        out, err, rc = _run_agy(argv_for(binary, route.model, full), cwd, args.timeout)
    finally:
        if took:
            scope_guard.release(cwd, run_id)
    got = parse_stream(out)
    tokens = {"tokens_in": got["tokens_in"], "tokens_out": got["tokens_out"]}
    if rc is None:
        return finish(EXIT_FALLBACK, "fail", "timeout", **tokens)
    failure = got["error"] or (err.strip() if rc != 0 else "")
    if failure and routing.classify_quota(f"{failure}\n{got['text']}", route.pool):
        return finish(EXIT_FALLBACK, "fail", "pool-exhausted", **tokens)
    if routing.classify_quota(got["text"][:300], route.pool) and len(got["text"]) < 300:
        return finish(EXIT_FALLBACK, "fail", "pool-exhausted", **tokens)      # a quota message returned as the answer
    if failure:
        code, d = finish(1, "fail", **tokens)
        return code, {**d, "error": failure[:500]}
    if not got["text"]:
        return finish(EXIT_FALLBACK, "fail", "empty", **tokens)

    report = Path(cwd, run_dir, "report.md")
    report.write_text(got["text"] + "\n", encoding="utf-8")
    after = _changed(cwd)
    stray = scope_guard.violations(sorted((after or set()) - (before or set())), scopes, run_id) if after is not None else []
    rep = reply_mod.parse(got["text"])
    code, d = finish(0, "ok", **tokens, extra={"scope_violations": stray} if stray else None)
    return code, {**d, "report_path": f"{run_dir}/report.md", "status": rep.get("status"), "blocked": rep.blocked,
                  "verdict": rep.verdict_word, "return_to_step": rep.return_to, "block_reason": rep.reason,
                  "model": route.model, **tokens, "scope_violations": stray, "conversation": got["conversation"]}


def cmd_run(args: argparse.Namespace) -> int:
    try:
        code, doc = run(args)
    except (OSError, ValueError) as e:
        code, doc = 2, {"error": str(e)}
    if args.json:
        print(json.dumps(doc, indent=2))
    elif code == 0:
        print(f"{doc['role']}: ok via {doc['route']} [{doc['pool']}] in {doc['duration_s']}s; report {doc['report_path']}"
              f"{'; verdict ' + doc['verdict'] if doc['verdict'] else ''}")
        if doc["scope_violations"]:
            print("  scope violations (logged): " + ", ".join(doc["scope_violations"]))
    else:
        print(f"agy run: {doc.get('refused') or doc.get('error') or doc.get('reason') or 'cause ' + str(doc.get('cause'))}"
              + (f"; fallback: {doc['fallback']}" if code == EXIT_FALLBACK and doc.get("fallback") else ""), file=sys.stderr)
    return code


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("agy", help="Antigravity helpers")
    v = p.add_subparsers(dest="agy_command", required=True)
    r = v.add_parser("run", help="Run a role on agy (-p) through the routing matrix and return the report contract")
    r.add_argument("--role", required=True)
    src = r.add_mutually_exclusive_group()
    src.add_argument("--prompt-file", metavar="FILE")
    src.add_argument("--prompt", metavar="TEXT")
    r.add_argument("--cwd", help="checkout agy works in (default: the current directory)")
    r.add_argument("--scope", nargs="+", metavar="PATH", help="repo-relative paths a write role may change (default: the whole checkout)")
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds before the call is killed (cause: timeout)")
    r.add_argument("--run", metavar="RUN_ID", help="the workflow run: its writer families feed the resolver, its output dir holds the report")
    r.add_argument("--step", help="workflow step id, recorded on the dispatch row")
    r.add_argument("--usage-file", metavar="FILE", help="JSON {pool: used fraction}; default: live pool usage")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)
