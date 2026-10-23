"""`ai-resources orchestrate ...` and `ai-resources workflows ...` (ADR-0015 D3).

Every verb prints one JSON object with `--json` (the plugin's only interface) and a short text summary without it.
The engine never dispatches: the objects carry `actions` for the runtime. Exit codes: 0 ok, 1 refused (a wrong state, an
unknown run or step), 2 usage or environment error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from . import cost as cost_mod
from . import limits, workflow_loader
from .engine import Engine, EngineError
from .state import RunStore, StateError

MAX_REPLY_FILE = 400_000


def _models(path: str) -> dict[str, str]:
    """role -> model ref from an executors.yaml (`by_role`); {} when the file is absent or unreadable."""
    if not path:
        from ..setup import state as setup_state
        path = str(setup_state.executors_path())
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, str] = {}
    for role, spec in ((doc.get("by_role") or {}) if isinstance(doc, dict) else {}).items():
        model = spec.get("model") if isinstance(spec, dict) else spec
        if isinstance(model, str) and model:
            out[str(role)] = model
    return out


def _param(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise EngineError(f"--param needs name=value, got {text!r}", "usage")
    return key.strip(), value


def _read_reply(path: str) -> str:
    """The reply text, at most MAX_REPLY_FILE bytes of its END: the return block is the last thing a role writes."""
    if not path:
        return ""
    if path == "-":
        data = sys.stdin.buffer.read()
    else:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - MAX_REPLY_FILE))
            data = fh.read()
    return data[-MAX_REPLY_FILE:].decode("utf-8", errors="replace")


def _emit(args: argparse.Namespace, doc: dict[str, Any]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=1, sort_keys=True))
        return
    if "error" in doc:
        print(f"error: {doc['error']}", file=sys.stderr)
        return
    for key in ("run", "status", "step", "failure"):
        if doc.get(key):
            print(f"{key}: {doc[key]}")
    for a in doc.get("actions", []) or []:
        extra = f" {a.get('role', '')} -> {a.get('agent', '')} ({a.get('timeout_s', '')}s)" if a["type"] == "dispatch" else ""
        print(f"- {a['type']}{(' ' + a.get('step', '')) if a.get('step') else ''}{extra}: {a.get('text', '')}".rstrip(": "))
    if doc.get("promoted"):
        print("promoted: " + ", ".join(doc["promoted"]))


def _default_engine(args: argparse.Namespace) -> Engine:
    return Engine(RunStore(Path(args.runs_dir) if getattr(args, "runs_dir", "") else None))


ENGINE_FACTORY = _default_engine           # tests replace it to inject a runner and a clock


def _engine(args: argparse.Namespace) -> Engine:
    return ENGINE_FACTORY(args)


def run(args: argparse.Namespace) -> int:
    cmd = args.orch_cmd
    try:
        e = _engine(args)
        if cmd == "start":
            wf = workflow_loader.load(args.workflow)
            timeouts = json.loads(args.role_timeouts) if args.role_timeouts else {}
            if not isinstance(timeouts, dict):
                raise EngineError("--role-timeouts must be a JSON object", "usage")
            workspace = str(Path(args.workspace or os.getcwd()).resolve())
            r = e.start(wf, args.goal, workspace, domain=args.domain, run_id=args.run_id, models=_models(args.executors),
                        role_timeouts=timeouts, max_teams=args.max_teams, base_ref=args.base_ref,
                        use_worktree=not args.no_worktree, params=dict(_param(x) for x in args.param))
            doc = e.next(r["id"])
            doc.update({"workflow": wf.name, "worktree": (r.get("worktree") or {}).get("path", ""),
                        "branch": (r.get("worktree") or {}).get("branch", ""), "failure": r.get("failure", "")})
        elif cmd == "next":
            doc = e.next(args.run)
        elif cmd == "report":
            doc = e.report(args.run, args.step, text=_read_reply(args.reply_file), session=args.session, agent=args.agent,
                           timeout=args.timeout, error=args.error)
        elif cmd == "approve":
            doc = e.approve(args.run)
        elif cmd == "cancel":
            doc = e.cancel(args.run)
        elif cmd == "resume":
            doc = {"runs": e.resume_all()} if args.all or not args.run else e.resume(args.run)
        elif cmd == "status":
            doc = e.status(args.run or None)
        elif cmd == "cost":
            agents_dir = Path(args.agents_dir) if args.agents_dir else Path.home() / ".openclaw" / "agents"
            doc = cost_mod.run_cost(e.store.load(args.run), agents_dir)
        else:                                                           # pragma: no cover - argparse requires a verb
            raise EngineError(f"unknown verb {cmd!r}", "usage")
    except EngineError as err:
        _emit(args, {"error": str(err), "code": err.code})
        return 2 if err.code == "usage" else 1
    except (workflow_loader.WorkflowError, StateError, ValueError, OSError) as err:
        _emit(args, {"error": str(err), "code": "environment"})
        return 2
    _emit(args, doc)
    return 0


def run_workflows(args: argparse.Namespace) -> int:
    try:
        if args.wf_cmd == "list":
            doc: dict[str, Any] = {"workflows": workflow_loader.list_workflows()}
        else:
            doc = workflow_loader.describe(workflow_loader.load(args.name))
    except workflow_loader.WorkflowError as err:
        _emit(args, {"error": str(err), "code": "environment"})
        return 2
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=1, sort_keys=True))
    elif args.wf_cmd == "list":
        for w in doc["workflows"]:
            print(f"{w['name']:34} {w['description'][:80]}")
    else:
        print(f"{doc['name']}: {doc['description']}")
        for s in doc["steps"]:
            print(f"  {s['id']:20} {s['role']:22} {('group ' + s['group']) if s['group'] else ''}")
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("orchestrate", help="Run a kit workflow as a team (state machine; the runtime dispatches)",
                       description="The native team orchestrator engine (ADR-0015). It never dispatches: `next` and "
                                   "`report` return the actions for a runtime, such as the OpenClaw plugin.")
    verbs = p.add_subparsers(dest="orch_cmd", metavar="VERB", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--json", action="store_true", help="Print one JSON object")
        sp.add_argument("--runs-dir", default="", help=argparse.SUPPRESS)
        sp.set_defaults(func=run)
        return sp

    s = common(verbs.add_parser("start", help="Register a run (it queues behind maxTeams teams)"))
    s.add_argument("--workflow", required=True, help="Workflow name, file stem or path (see `ai-resources workflows list`)")
    s.add_argument("--goal", required=True)
    s.add_argument("--workspace", default="", help="Repository to work in (default: the current directory)")
    s.add_argument("--domain", default="")
    s.add_argument("--run-id", default="")
    s.add_argument("--executors", default="", help="executors.yaml with the role -> model map (default: the setup one)")
    s.add_argument("--max-teams", type=int, default=limits.DEFAULT_MAX_TEAMS)
    s.add_argument("--role-timeouts", default="", help='JSON {"planner": 1200, ...} in seconds')
    s.add_argument("--base-ref", default="HEAD")
    s.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="A workflow parameter that fills {{NAME}} in the step prompts (repeatable)")
    s.add_argument("--no-worktree", action="store_true", help="Work in the workspace itself (no per-team worktree)")
    n = common(verbs.add_parser("next", help="The actions to perform now"))
    n.add_argument("run")
    r = common(verbs.add_parser("report", help="Feed back a finished step: a reply, a timeout or an error"))
    r.add_argument("run")
    r.add_argument("step")
    r.add_argument("--session", default="", help="The session id the runtime ran the step in")
    r.add_argument("--agent", default="", help="The agent id the step ran on")
    r.add_argument("--reply-file", default="", help="File with the role's final reply ('-' for stdin)")
    r.add_argument("--timeout", action="store_true", help="The per-role timeout fired")
    r.add_argument("--error", default="", help="The dispatch failed with this message")
    common(verbs.add_parser("approve", help="Approve the plan so the first writer may start")).add_argument("run")
    common(verbs.add_parser("cancel", help="Cancel a run and free its team slot")).add_argument("run")
    rs = common(verbs.add_parser("resume", help="Hand out again the steps that were never reported (after a restart)"))
    rs.add_argument("run", nargs="?", default="")
    rs.add_argument("--all", action="store_true", help="Every running run, then start what the queue now allows")
    st = common(verbs.add_parser("status", help="One run, or every run and the queue"))
    st.add_argument("run", nargs="?", default="")
    c = common(verbs.add_parser("cost", help="API-equivalent cost of a run from the OpenClaw transcripts (a report, no cap)"))
    c.add_argument("run")
    c.add_argument("--agents-dir", default="")

    w = sub.add_parser("workflows", help="List the kit workflows or show one as JSON")
    wv = w.add_subparsers(dest="wf_cmd", metavar="VERB", required=True)
    wl = wv.add_parser("list", help="Every workflow")
    wl.add_argument("--json", action="store_true")
    ws = wv.add_parser("show", help="One workflow: steps, roles, skills, groups and the approval gate")
    ws.add_argument("name")
    ws.add_argument("--json", action="store_true")
    for sp in (wl, ws):
        sp.set_defaults(func=run_workflows)
