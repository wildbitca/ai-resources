"""`ai-resources acceptance`: the proof that what setup configured actually behaves as designed (ADR-0019 D8).

One registry of cases, one row per case (`id | PASS/FAIL/SKIP | reason`), seven groups:

* route         every role resolves through the matrix, ONE minimal real call per distinct (backend, model) cell, and the
                model/pool the call actually used (the response, or the dispatch row) must equal the matrix cell;
* fault         injected faults (pool at 0.85, bad key, timeout, agy missing, quota text): the fallback fires exactly once
                with the recorded cause, offline with fakes;
* checker       a checker never shares a family with the writers (or is blocked, explicitly);
* spawn         every `kit-role-*` agent is spawnable from `main` and the channel agents; peak gateway memory is sampled;
* gemini-tools  a cheap Gemini tool call through OpenClaw decides the heartbeat gating (verdict file, config untouched);
* agy-write     agy writes a file inside a scratch dir and the scope check is logged;
* setup-live    the failure/restart/watchdog/live cases of setup. They are thin adapters over the EXISTING checks
                (`openclaw_host`, `openclaw_reconcile`, `openclaw_reload_rules`, the claude cockpit hooks); `verify --live`
                runs this group and maps it to findings, so both commands are one implementation.

A case that needs a tool that is missing or not signed in is SKIP with the exact reason. Cases that kill turns (SIGTERM, a
real restart, the watchdog) are `operator` cases: listed with the exact command (`--operator`), never run. Real calls are
capped (one minimal prompt, one call per cell) and `--dry-run` prints the plan without any call. Nothing prints a secret.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from . import dispatch_ledger
from . import scrub as _scrub
from .scoring import routing

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
GROUPS = ("route", "fault", "checker", "spawn", "gemini-tools", "agy-write", "setup-live")
SCHEMA = 1
PROMPT = "Reply with exactly: pong"
AGY_PROMPT = "Reply with exactly: pong. Do not use any tool and do not create any file."
CALL_TIMEOUT_S = 120
_UNAUTH = re.compile(r"(?i)not logged in|/login|unauthori[sz]ed|\b401\b|invalid api key|no api key|no credentials|"
                     r"not signed in|authenticat|missing .*api.?key|no auth")


class Result(NamedTuple):
    status: str
    reason: str
    evidence: dict = {}


def ok(reason: str, **ev: Any) -> Result:
    return Result(PASS, reason, ev)


def bad(reason: str, **ev: Any) -> Result:
    return Result(FAIL, reason, ev)


def skip(reason: str, **ev: Any) -> Result:
    return Result(SKIP, reason, ev)


@dataclass
class Env:
    """What a case may use. `runner` (None = the cockpit helpers) and the paths are the seams the tests replace."""
    state: Any = None
    ak_path: str = ""
    runner: Callable[..., "tuple[int, str]"] | None = None
    offline: bool = False
    cache: Path = field(default_factory=lambda: Path.home() / ".cache" / "ai-resources")
    usage_file: str = ""
    which: Callable[[str], "str | None"] = shutil.which
    run_id: str = field(default_factory=lambda: f"acceptance-{os.getpid()}")
    memo: dict = field(default_factory=dict)

    def setup_state(self) -> Any:
        if self.state is None:
            from .setup import state
            self.state = state.load()
        return self.state

    def kit(self) -> str:
        if not self.ak_path:
            from .openclaw_host import kit_root
            self.ak_path = str(kit_root())
        return self.ak_path


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    run: Callable[[Env], Result]
    cap: str = "no call"                    # what a real call costs, printed by --dry-run
    offline: bool = True                    # makes no real model call: runs on a fixture host
    operator: str = ""                      # the exact command of an operator-run case (never executed)
    severity: str = "error"                 # what a FAIL is for `verify --live`: error | warn
    remedy: str = ""


CASES: list[Case] = []


def case(id: str, group: str, **kw: Any) -> Callable:
    def reg(fn: Callable[[Env], Result]) -> Callable:
        CASES.append(Case(id, group, fn, **kw))
        return fn
    return reg


scrub = _scrub.scrub


def _json_from(text: str) -> dict:
    """The last JSON object that starts a line of `text` (the CLIs print log lines before it)."""
    found: dict = {}
    for m in re.finditer(r"^\{", text or "", re.M):
        try:
            val, _ = json.JSONDecoder().raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(val, dict):
            found = val
    return found


@contextmanager
def sandbox(cache: Path) -> Iterator[Path]:
    """A scratch HOME/XDG_STATE_HOME/PATH so a fault case writes its ledger and finds its fakes in a throwaway tree. It lives
    under the cache dir, not /tmp: the run ledger refuses a volatile root."""
    base = cache / "acceptance-scratch"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="sandbox-", dir=base))
    (root / "bin").mkdir()
    if shutil.which("sleep"):
        (root / "bin" / "sleep").symlink_to(shutil.which("sleep"))
    new = {"HOME": str(root), "XDG_STATE_HOME": str(root / "state"), "PATH": str(root / "bin"), "AI_RESOURCES_AGY_BIN": ""}
    old = {k: os.environ.get(k) for k in new}
    os.environ.update(new)
    try:
        yield root
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(root, ignore_errors=True)


def _fake_bin(root: Path, name: str, body: str) -> Path:
    path = root / "bin" / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- route -------------------------------------------------------------------------------------------------------------

def _usage(env: Env) -> dict:
    from . import pool_usage
    try:
        if env.usage_file:
            return {p: (None if v is None else float(v)) for p, v in json.loads(Path(env.usage_file).read_text("utf-8")).items()}
        return pool_usage.usage_map(pool_usage.snapshot())
    except (OSError, ValueError):
        return {}


def _run_tool(argv: list[str], env: dict | None = None, timeout: float = CALL_TIMEOUT_S, cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out after {timeout:g}s"
    except OSError as e:
        return 1, str(e)
    return p.returncode, (p.stdout + "\n" + p.stderr).strip()


def _call_claude(cell: str, env: Env) -> dict:
    claude = env.which("claude")
    if not claude:
        return {"skip": "claude is not on PATH"}
    from .setup import live_auth
    auth = live_auth.read_claude()
    if auth.kind != live_auth.SUBSCRIPTION:
        return {"skip": f"Claude Code is not signed in with the Max subscription ({auth.label}); a call would not drain claude-subscription"}
    alias = routing.claude_alias(cell)
    argv = [claude, "-p", "--output-format", "json", "--max-turns", "1", *(["--model", alias] if alias != "inherit" else []), PROMPT]
    started = time.time()
    rc, out = _run_tool(argv, {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
    doc = _json_from(out) or _json_from("\n" + out)
    if rc != 0 or doc.get("is_error") or not doc:
        return {"error": scrub(out[-300:]) or f"exit {rc}", "unauth": bool(_UNAUTH.search(out))}
    used = sorted((doc.get("modelUsage") or {}))
    usage = doc.get("usage") or {}
    return {"model": used[0] if used else "", "models": used, "pool": routing.POOL_MAX, "tokens_in": int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0),
            "tokens_out": int(usage.get("output_tokens") or 0), "duration_s": time.time() - started, "kind": "claude"}


def _call_infer(cell: str, env: Env) -> dict:
    oc = env.which("openclaw")
    if not oc:
        return {"skip": "openclaw is not on PATH"}
    ref = routing.cell_to_openclaw_ref(cell)
    started = time.time()
    rc, out = _run_tool([oc, "infer", "model", "run", "--model", ref, "--prompt", PROMPT, "--json"])
    doc = _json_from(out)
    if rc != 0 or not doc.get("ok"):
        return {"error": scrub(out[-300:]) or f"exit {rc}", "unauth": bool(_UNAUTH.search(out))}
    # the transport log line names the wire API: a direct Messages / Generative Language call drains the API credits, never a CLI login
    api = (re.search(r"\bapi=(\S+)", out) or [None, ""])[1]
    by_api = {"anthropic-messages": routing.POOL_ANTHROPIC_API, "google-generative-ai": routing.POOL_GEMINI_API}
    by_provider = {"anthropic": routing.POOL_ANTHROPIC_API, "google": routing.POOL_GEMINI_API}
    pool = by_api.get(api, "") if api else by_provider.get(str(doc.get("provider")), "")     # no log line: the provider decides
    return {"model": f"{doc.get('provider')}/{doc.get('model')}", "pool": pool, "api": api, "attempts": len(doc.get("attempts") or []),
            "transport": doc.get("transport"), "duration_s": time.time() - started, "kind": "infer"}


def _call_agy(role: str, env: Env) -> dict:
    from . import agy_run
    scratch = env.cache / "acceptance-scratch" / "route"
    scratch.mkdir(parents=True, exist_ok=True)
    ns = argparse.Namespace(role=role, prompt=AGY_PROMPT, prompt_file=None, cwd=str(scratch), scope=None, timeout=300.0,
                            run=None, step="acceptance:route", usage_file=env.usage_file, json=True)
    code, doc = agy_run.run(ns)
    rows = [r for r in dispatch_ledger.load(doc.get("run", "")) if r.get("role") == role] if doc.get("run") else []
    row = rows[-1] if rows else {}
    if code != 0:
        return {"error": scrub(doc.get("error") or doc.get("refused") or f"cause {doc.get('cause')} (exit {code})"),
                "cause": doc.get("cause"), "unauth": bool(_UNAUTH.search(str(doc.get("error") or ""))), "missing": doc.get("cause") == "agy-missing"}
    return {"model": f"{row.get('backend')}:{row.get('model')}", "pool": row.get("pool"), "tokens_in": row.get("tokens_in", 0),
            "tokens_out": row.get("tokens_out", 0), "duration_s": doc.get("duration_s", 0), "kind": "agy", "role": role}


def _dispatch_once(route: routing.Route, role: str, env: Env) -> dict:
    """The one real call of a (backend, model) cell, memoised: later roles on the same cell reuse its result."""
    key = (route.backend, route.model)
    if key not in env.memo:
        if route.backend in ("claude-code", "claude-cli"):
            env.memo[key] = _call_claude(route.cell, env)
        elif route.backend == "agy":
            from . import agy_run
            env.memo[key] = _call_agy(role, env) if agy_run.agy_bin() else {"skip": "agy is not installed (no agy on PATH or in ~/.local/bin)"}
        elif route.backend in ("anthropic-api", "gemini-api"):
            env.memo[key] = _call_infer(route.cell, env)
        else:
            env.memo[key] = {"skip": f"no dispatcher for backend {route.backend}"}
    return env.memo[key]


def _route_case(role: str) -> Callable[[Env], Result]:
    def run(env: Env) -> Result:
        from .setup import detection
        route = routing.resolve(role, _usage(env), [], detection.inactive_backends())
        if route.blocked:
            return bad(f"{role} is blocked: {route.reason}")
        res = _dispatch_once(route, role, env)
        if res.get("skip"):
            return skip(res["skip"], cell=route.cell)
        if res.get("error"):
            return (skip(f"{route.cell} is not signed in: {res['error'][:120]}", cell=route.cell) if res.get("unauth") or res.get("missing")
                    else bad(f"the call on {route.cell} failed: {res['error'][:160]}", cell=route.cell))
        got = res["model"]
        if res["kind"] == "claude":
            model_ok = any(m.startswith(f"claude-{routing.claude_alias(route.cell)}") for m in res.get("models", []))
        else:
            model_ok = got == (route.cell if res["kind"] == "agy" else routing.cell_to_openclaw_ref(route.cell))
        if not (model_ok and res.get("pool") == route.pool):
            return bad(f"{role}: matrix says {route.cell} [{route.pool}], the call used {got} [{res.get('pool')}]", cell=route.cell, used=got)
        if res.get("role") != role:             # one dispatch row per role; agy_run already wrote the representative's own
            dispatch_ledger.record(role, "ok", run_id=env.run_id, step="acceptance:route", cell=route.cell, pool=route.pool,
                                   tokens_in=res.get("tokens_in", 0), tokens_out=res.get("tokens_out", 0),
                                   duration_s=res.get("duration_s", 0), fallback_from=(routing.merged()[role].chain[0] if route.cause else ""),
                                   fallback_cause=route.cause or "")
        return ok(f"{route.cell} [{route.pool}] answered as the matrix says" + (f" (fallback, cause {route.cause})" if route.cause else ""),
                  cell=route.cell, used=got, tokens_in=res.get("tokens_in", 0), tokens_out=res.get("tokens_out", 0))
    return run


for _role, _r in routing.ROLES.items():
    CASES.append(Case(f"route.{_role}", "route", _route_case(_role), offline=False,
                      cap="one minimal call per distinct (backend, model) cell (claude -p --max-turns 1 / agy -p / openclaw infer), "
                          "reused by every role on the cell; prompt of ~6 tokens"))


# --- fault (offline, fakes) --------------------------------------------------------------------------------------------

_AGY_QUOTA = ('echo \'{"result":{"status":"ERROR","error":{"message":"You have hit your limit (429 resource exhausted)"}}}\'')


def _agy_args(role: str, root: Path, timeout: float, run: str) -> argparse.Namespace:
    (root / "work").mkdir(exist_ok=True)
    return argparse.Namespace(role=role, prompt="x", prompt_file=None, cwd=str(root / "work"), scope=None, timeout=timeout,
                              run=run, step="fault", usage_file="", json=True)


def _one_fallback(role: str, cause: str, code: int, doc: dict, run: str = "") -> str:
    """'' when exactly one ledger row carries `cause` and the dispatcher offered the matrix's NEXT cell, else the problem."""
    chain = routing.merged()[role].chain
    rows = [r for r in dispatch_ledger.load(run) if r.get("role") == role]
    if len(rows) != 1:
        return f"{len(rows)} dispatch rows for {role}, expected exactly 1"
    if rows[0]["fallback_cause"] != cause:
        return f"recorded cause {rows[0]['fallback_cause']!r}, expected {cause!r}"
    if rows[0]["fallback_from"] != chain[0]:
        return f"fallback_from {rows[0]['fallback_from']!r}, expected {chain[0]!r}"
    if code != 75 or doc.get("fallback") != chain[1]:
        return f"exit {code} with fallback {doc.get('fallback')!r}, expected 75 and {chain[1]!r}"
    return ""


def _fault_agy(env: Env, cause: str, binary: str | None, timeout: float = 20.0, role: str = "explore") -> Result:
    from . import agy_run
    with sandbox(env.cache) as root:
        if binary is not None:
            _fake_bin(root, "agy", f'echo x >> "{root}/calls"\n{binary}')
        run = f"acc-fault-{cause}-{secrets.token_hex(3)}"
        code, doc = agy_run.run(_agy_args(role, root, timeout, run))
        calls = len((root / "calls").read_text().split()) if (root / "calls").exists() else 0
        if binary is not None and calls != 1:
            return bad(f"the faulty cell ran {calls} times, expected 1 (no retry, no silent fallback)")
        problem = _one_fallback(role, cause, code, doc, run)
        return bad(f"{cause}: {problem}") if problem else ok(
            f"{cause}: fallback fired once, cause recorded, next cell offered ({doc.get('fallback')})", cause=cause)


@case("fault.pool-hot", "fault")
def _pool_hot(env: Env) -> Result:
    with sandbox(env.cache) as root:
        tok = secrets.token_hex(3)
        for used, cause in ((0.85, "pool-hot"), (1.0, "pool-exhausted")):
            usage_file = root / "usage.json"
            role = "explore"
            usage_file.write_text(json.dumps({routing.pool_of(routing.merged()[role].chain[0]): used}))
            from . import agy_run
            route = routing.resolve(role, agy_run._usage(str(usage_file)))
            dispatch_ledger.record(role, "pending", run_id=f"acc-{cause}-{tok}", cell=route.cell or "", fallback_cause=route.cause or "",
                                   fallback_from=routing.merged()[role].chain[0] if route.cause else "")
            chain = routing.merged()[role].chain
            rows = dispatch_ledger.load(f"acc-{cause}-{tok}")
            if route.cell != chain[1] or route.cause != cause or len(rows) != 1 or rows[0]["fallback_cause"] != cause:
                return bad(f"pool at {used:.0%} routed to {route.cell} with cause {route.cause}; expected {chain[1]} / {cause}")
    return ok("a pool at 85% spills to the next cell (pool-hot), at 100% as pool-exhausted; one row each, cause recorded")


@case("fault.bad-key", "fault")
def _bad_key(env: Env) -> Result:
    from . import key_check
    with sandbox(env.cache) as root:
        tok = secrets.token_hex(3)
        envfile = root / "gateway.systemd.env"
        envfile.write_text("GEMINI_API_KEY=fake-key-for-the-fault-case\n")
        seen: list[str] = []

        def http(url: str, headers: Any) -> int:
            seen.append(url)
            return 401
        res = key_check.check(("gemini",), fix=False, http=http, env_path=envfile, db=root / "none.sqlite")[0]
        if res.env != key_check.REJECTED or not res.problem:
            return bad(f"a rejected key was classified {res.env!r}, problem={res.problem}")
        route = routing.resolve("utility", {}, [], {"gemini-api": "key-invalid"})
        chain = routing.merged()["utility"].chain
        dispatch_ledger.record("utility", "pending", run_id=f"acc-key-{tok}", cell=route.cell or "", fallback_cause=route.cause or "", fallback_from=chain[0])
        rows = dispatch_ledger.load(f"acc-key-{tok}")
        if route.cell != chain[1] or route.cause != "key-invalid" or len(rows) != 1 or rows[0]["fallback_cause"] != "key-invalid":
            return bad(f"a rejected key routed utility to {route.cell} with cause {route.cause}; expected {chain[1]} / key-invalid")
    return ok("a rejected key is detected by key_check (no value printed) and utility falls back once with cause key-invalid")


@case("fault.timeout", "fault")
def _timeout(env: Env) -> Result:
    return _fault_agy(env, "timeout", "sleep 30", timeout=1.0)


@case("fault.agy-missing", "fault")
def _agy_missing(env: Env) -> Result:
    return _fault_agy(env, "agy-missing", None)


@case("fault.quota-text", "fault")
def _quota_text(env: Env) -> Result:
    if not routing.classify_quota("You have hit your limit", routing.POOL_AGY_GEMINI):
        return bad("classify_quota does not recognise the quota text")
    return _fault_agy(env, "pool-exhausted", _AGY_QUOTA)


# --- checker independence ----------------------------------------------------------------------------------------------

def _fam(cell: str | None) -> str | None:
    return routing.model_family((cell or "").partition(":")[2])


@case("checker.gemini-haiku-writers", "checker")
def _checker_cheap(env: Env) -> Result:
    for checker in ("verifier", "security-auditor"):
        for writers in (["gemini-3.8-flash"], ["haiku"], ["gemini-3.8-flash", "haiku"]):
            r = routing.resolve(checker, {}, writers)
            if r.blocked or _fam(r.cell) != "sonnet":
                return bad(f"{checker} with writers {writers} resolved to {r.cell}; expected a sonnet checker")
    return ok("Gemini- and Haiku-written work is checked by Sonnet (verifier and security-auditor)")


@case("checker.sonnet-writer", "checker")
def _checker_sonnet(env: Env) -> Result:
    out = []
    for checker in ("verifier", "security-auditor"):
        r = routing.resolve(checker, {}, ["sonnet"])
        if not r.blocked and _fam(r.cell) in (None, "sonnet"):
            return bad(f"{checker} with a Sonnet writer resolved to {r.cell}")
        if r.blocked and not r.reason:
            return bad(f"{checker} blocked without a reason")
        out.append(f"{checker}: {'BLOCKED (' + str(r.cause) + ')' if r.blocked else r.cell}")
    return ok("Sonnet-written work goes to Opus or is blocked explicitly; " + "; ".join(out))


@case("checker.never-weaker", "checker")
def _checker_never_weaker(env: Env) -> Result:
    families = ["gemini-3.8-flash", "haiku", "sonnet", "opus"]
    for checker in ("verifier", "security-auditor"):
        for i in range(len(families) + 1):
            for j in range(i, len(families) + 1):
                writers = families[i:j]
                r = routing.resolve(checker, {}, writers)
                if r.blocked:
                    if not (r.reason and r.cause == "no-independent-checker"):
                        return bad(f"{checker} blocked silently with writers {writers}")
                elif writers and _fam(r.cell) in writers:
                    return bad(f"{checker} shares family {_fam(r.cell)} with its writers {writers} ({r.cell})")
    return ok("across every writer-family combination a checker is independent or blocked with a reason; never silently weaker")


# --- spawn -------------------------------------------------------------------------------------------------------------

def _openclaw_doc() -> dict:
    from .setup.cockpits import openclaw as cockpit
    return cockpit.read_config(cockpit.config_path())


@case("spawn.allow", "spawn", severity="warn", remedy="re-run `ai-resources setup` (never edit openclaw.json by hand)")
def _spawn_allow(env: Env) -> Result:
    from .setup.cockpits import openclaw_orchestration as orch
    doc = _openclaw_doc()
    ids = orch.kit_role_ids(doc)
    if not ids:
        return skip("no kit-role agents in the OpenClaw config")
    s = env.setup_state()
    orchs = orch.orchestrator_ids(doc, (s.profile.lanes or {}).get("orchestrators") or [],
                                  (s.profile.lanes or {}).get("orchestrators_exclude") or [])
    short = {}
    for o in orchs:
        allow = orch._get(doc, "agents", "entries", o, "subagents", "allowAgents") or []
        if "*" not in allow and (miss := [i for i in ids if i not in allow]):
            short[o] = miss
    if short:
        return bad("; ".join(f"{o} cannot spawn {', '.join(m)}" for o, m in sorted(short.items())), short=short)
    return ok(f"{len(orchs)} orchestrator agent(s) may spawn all {len(ids)} kit-role agents", orchestrators=orchs, kit_roles=len(ids))


def _memory_sample(env: Env) -> dict:
    """Gateway memory (systemd), MCP child count and mean RSS of those children: the C1 sizing data."""
    from . import openclaw_host as host, openclaw_reconcile as rec
    run = env.runner or host.default_runner
    out: dict = {}
    argv = ["systemctl", "--user", "show", host.GATEWAY_UNIT, "-p", "MemoryCurrent", "-p", "MemoryPeak", "-p", "MemoryHigh", "-p", "TasksCurrent"]
    rc, text = run(argv, timeout=10)
    for line in (text or "").splitlines() if rc == 0 else []:
        k, _, v = line.partition("=")
        if v.strip().isdigit():
            out[k.strip()] = int(v)
    counts = rec.gateway_mcp_children(run)
    if counts is not None:
        out["mcp_children"] = sum(counts.values())
    return out


@case("spawn.memory-peak", "spawn")
def _spawn_memory(env: Env) -> Result:
    sample = _memory_sample(env)
    if not sample:
        return skip("the gateway unit's memory cannot be read here (no systemd user session or the unit is stopped)")
    mib = lambda v: f"{v / 2**20:.0f} MiB"  # noqa: E731
    parts = [f"{k}={mib(v) if k.startswith('Memory') and v < 2**62 else v}" for k, v in sorted(sample.items())]
    return ok("sampled for the MemoryHigh/MCP sizing note (measure, do not cap): " + ", ".join(parts), **sample)


@case("spawn.explore-run", "spawn", offline=False, cap="one `openclaw agent --agent <kit-role explore> -m pong` turn (the role's own model)")
def _spawn_explore(env: Env) -> Result:
    from .setup.cockpits import openclaw_roles
    oc = env.which("openclaw")
    if not oc:
        return skip("openclaw is not on PATH")
    wired = _spawn_allow(env)
    if wired.status != PASS:
        return skip(f"spawn.allow is {wired.status} ({wired.reason[:100]}); fix the wiring first. Command in --operator")
    agent = openclaw_roles.agent_id_for("explore")
    before = _memory_sample(env)
    rc, out = _run_tool([oc, "agent", "--agent", agent, "-m", PROMPT, "--json", "--timeout", "90"], timeout=150)
    after = _memory_sample(env)
    if rc != 0 or "pong" not in out.lower():
        return skip(f"{agent} is not reachable: {scrub(out[-160:])}") if _UNAUTH.search(out) else bad(f"{agent} did not answer: {scrub(out[-200:])}")
    return ok(f"{agent} answered through the gateway", agent=agent, memory_before=before, memory_after=after)


# --- gemini tool-calling -----------------------------------------------------------------------------------------------

def gemini_verdict_path(cache: Path | None = None) -> Path:
    return (cache or Path.home() / ".cache" / "ai-resources") / "gemini-tools.json"


def gemini_verdict_line(cache: Path | None = None) -> str:
    """What doctor prints about the heartbeat gating; '' when the case never ran."""
    try:
        v = json.loads(gemini_verdict_path(cache).read_text("utf-8"))
        passed = v["verdict"] == "pass"
        return (f"Gemini tool-calling through OpenClaw {'PASSED' if passed else 'FAILED'} on {v['at'][:10]} ({v['model']}): "
                + ("the heartbeat may move to Gemini (set a `routing:` override; the kit never edits openclaw config)" if passed
                   else "the heartbeat stays on Haiku"))
    except (OSError, ValueError, KeyError, TypeError):
        return ""


@case("gemini.tool-call", "gemini-tools", offline=False, cap="one `openclaw agent exec` turn on google/gemini-3.8-flash with one file-read tool call")
def _gemini_tools(env: Env) -> Result:
    oc = env.which("openclaw")
    if not oc:
        return skip("openclaw is not on PATH")
    model = "google/gemini-3.8-flash"
    scratch = env.cache / "acceptance-scratch" / "gemini-tools"
    scratch.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    (scratch / "token.txt").write_text(token + "\n")
    rc, out = _run_tool([oc, "agent", "exec", "--model", model, "--cwd", str(scratch), "--json", "--timeout", "120",
                         "Use your file reading tool to read token.txt in the working directory and reply with only its exact content."], timeout=180)
    doc = _json_from(out)
    if doc and doc.get("ok") is False or (rc != 0 and not doc):
        # the turn never ran (secret store, auth, gateway): that says nothing about Gemini, so no verdict is written
        why = scrub(str((doc.get("error") or {}).get("message") if doc else out[-160:]))[:160]
        return skip(f"OpenClaw could not run the agent turn here ({why}); Gemini tool-calling stays unproven and the heartbeat on Haiku")
    passed = rc == 0 and token in out
    env.cache.mkdir(parents=True, exist_ok=True)
    gemini_verdict_path(env.cache).write_text(json.dumps(
        {"verdict": "pass" if passed else "fail", "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
    return ok(f"{model} called the file tool and returned the token; the heartbeat may move to Gemini") if passed else bad(
        f"{model} did not return the file content through a tool call (exit {rc}): {scrub(out[-160:])}")


# --- agy writes in a scratch dir ---------------------------------------------------------------------------------------

@case("agy.write-scope", "agy-write", offline=False, cap="one `agy run --role doc-writer` call (agy flash, ~100k tokens of the AI Pro quota)")
def _agy_write(env: Env) -> Result:
    from . import agy_run
    if not agy_run.agy_bin():
        return skip("agy is not installed (no agy on PATH or in ~/.local/bin)")
    scratch = env.cache / "acceptance-scratch" / "agy-write"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    subprocess.run(["git", "-C", str(scratch), "init", "-q"], capture_output=True)
    role = "doc-writer"
    ns = argparse.Namespace(role=role, prompt="Create the file hello.txt in the working directory containing exactly the word pong.",
                            prompt_file=None, cwd=str(scratch), scope=["hello.txt"], timeout=300.0, run=f"{env.run_id}-write",
                            step="acceptance:agy-write", usage_file=env.usage_file, json=True)
    code, doc = agy_run.run(ns)
    if code != 0:
        why = scrub(doc.get("error") or doc.get("refused") or f"cause {doc.get('cause')} (exit {code})")
        return skip(f"agy is not signed in: {why[:140]}") if _UNAUTH.search(why) else bad(f"agy run exited {code}: {why[:200]}")
    wrote = (scratch / "hello.txt").read_text().strip() if (scratch / "hello.txt").is_file() else None
    if wrote is None or "pong" not in wrote.lower():
        return bad(f"agy did not write hello.txt in the scratch dir (found {wrote!r})")
    stray = doc.get("scope_violations") or []
    return bad(f"agy wrote hello.txt but also outside its scope: {stray}") if stray else ok(
        "agy wrote hello.txt inside the scratch dir; scope check logged no violation", route=doc.get("route"), scope_violations=[])


# --- setup-live: adapters over the existing checks ---------------------------------------------------------------------

def _cockpit() -> Any:
    from .setup.cockpits import openclaw
    return openclaw


def _oc(env: Env, argv: list[str], timeout: float) -> tuple[int, str]:
    return env.runner(argv, timeout=timeout) if env.runner else _cockpit()._openclaw(argv[1:], timeout=timeout)


@case("setup.gateway-health", "setup-live", remedy="`ai-resources openclaw doctor` (drain, fix, start, health)")
def _health(env: Env) -> Result:
    rc, _ = _oc(env, ["openclaw", "health"], 45)
    return ok("the gateway answers `openclaw health`") if rc == 0 else bad("the gateway does not answer `openclaw health`")


@case("setup.plugin-fresh", "setup-live")
def _plugin(env: Env) -> Result:
    cp = _cockpit()
    plugin = cp.plugin_runtime()
    stable = str(Path(env.kit()) / "openclaw-plugin" / "ai-resources")
    disk = cp.plugin_version(env.kit())
    if plugin.get("status") != "loaded":
        return bad(f"the kit plugin is {plugin.get('status') or 'unreadable'} in the gateway")
    running = str(plugin.get("packageVersion") or plugin.get("version") or "")
    why = cp.stale_reason(stable, env.runner)
    if why or (disk and running and running != disk):
        return bad(f"the gateway runs kit plugin {running or '?'} but the disk has {disk or '?'}" + (f" ({why} rule)" if why else ""),
                   stale=why)
    return ok(f"the gateway runs the kit plugin {running or disk} that is on disk")


def _missing_hooks(env: Env) -> list[str]:
    from . import openclaw_reconcile as rec
    o = env.setup_state().openclaw
    return rec.hooks_missing(env.kit(), team=bool(o.host_narration) and o.host_hooks_applied, guard=bool(o.host_guard) and o.host_hooks_applied)


@case("setup.hooks", "setup-live", remedy="re-run `ai-resources setup`")
def _hooks(env: Env) -> Result:
    missing = [m for m in _missing_hooks(env) if not m.startswith("StopFailure:")]
    return bad("hooks missing from ~/.claude/settings.json: " + ", ".join(missing)) if missing else ok(
        "every kit hook is registered in ~/.claude/settings.json")


@case("setup.stopfailure-hook", "setup-live", remedy="re-run `ai-resources setup`")
def _stopfailure(env: Env) -> Result:
    missing = [m for m in _missing_hooks(env) if m.startswith("StopFailure:")]
    return bad("hooks missing from ~/.claude/settings.json: " + ", ".join(missing)) if missing else ok(
        "the StopFailure quota hook (kit_quota_signal.py) is registered")


@case("setup.mcp-child-cap", "setup-live", severity="warn")
def _mcp(env: Env) -> Result:
    from . import openclaw_host as host, openclaw_reconcile as rec
    counts = rec.gateway_mcp_children(env.runner or host.default_runner)
    if counts is None:
        return skip("the gateway's cgroup cannot be read here")
    n = sum(counts.values())
    return bad(f"{n} MCP server children under the gateway (threshold {rec.MCP_CHILD_WARN})", children=n) if rec.mcp_overflow(counts) else ok(
        f"{n} MCP server children under the gateway", children=n)


@case("setup.offbox-cred", "setup-live", remedy="re-run `ai-resources setup` (it regenerates it from the host's federation)")
def _offbox(env: Env) -> Result:
    from . import openclaw_host as host
    cfg = host.read_host_env()
    cred = cfg.get("OPENCLAW_OFFBOX_CRED_FILE")
    if not (cfg.get("OPENCLAW_OFFBOX_BUCKET") and cred):
        return skip("no off-box backup is configured on this host")
    try:
        mode = os.stat(cred).st_mode & 0o777
    except OSError:
        return bad("the off-box credential file named by OPENCLAW_OFFBOX_CRED_FILE is missing")
    return ok("the off-box credential file exists (0600)") if mode == 0o600 else bad(f"the off-box credential file has mode {mode:o}, not 600")


@case("setup.offbox-regenerate", "setup-live")
def _offbox_regen(env: Env) -> Result:
    from . import openclaw_host as host
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        src, target = Path(d, "src.json"), Path(d, "cred.json")
        src.write_text(json.dumps({"type": "external_account", "audience": "x", "credential_source": {"executable": {"command": "/bin/true"}}}))
        first = host.offbox_wif_cred(host.OFFBOX_SA_DEFAULT, env={}, source=src, target=target)[0]
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0
        target.unlink()
        again = host.offbox_wif_cred(host.OFFBOX_SA_DEFAULT, env={}, source=src, target=target)[0]
        same = host.offbox_wif_cred(host.OFFBOX_SA_DEFAULT, env={}, source=src, target=target)[0]
    if (first, again, same) == ("written", "written", "same") and mode == 0o600:
        return ok("a deleted off-box credential is regenerated (0600) from the federation config and a second pass changes nothing")
    return bad(f"off-box credential regeneration gave {first}/{again}/{same} (mode {mode:o})")


class _Calls:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], **_kw: Any) -> tuple[int, str]:
        self.argv.append(list(argv))
        return 0, ""


def _window(d: Path, *, busy: int | None = 0, cgroup: str = "0::/user.slice/other.scope\n", marker: bool = False,
            make_cgroup: bool = True) -> tuple[int, _Calls, Any]:
    from . import openclaw_host as host
    d.mkdir(exist_ok=True)
    runner, cg = _Calls(), d / "cgroup"
    if make_cgroup:
        cg.write_text(cgroup)
    else:
        cg.mkdir()                                  # a directory cannot be read as text: the state is "unknown"
    mk = host.Marker(d / "watchdog.off")
    if marker:
        mk.path.write_text("")                      # an operator's (unsigned) window
    code = host.drained_window(lambda: True, runner=runner, require_idle=True, busy_probe=lambda: busy, marker=mk,
                               openclaw_bin="openclaw", out=lambda _l: None, self_cgroup_path=cg, lock_path=d / "lock",
                               sleep=lambda _s: None)
    return code, runner, mk


@case("setup.marker-guard", "setup-live")
def _marker_guard(env: Env) -> Result:
    from . import openclaw_host as host
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        a, b = host.Marker(Path(d, "a.off")), host.Marker(Path(d, "a.off"))
        first, second = a.acquire(), b.acquire()
        code, runner, mk = _window(Path(d, "window"), marker=True)
        kept = mk.path.exists()
    if first and not second and code == host.EXIT_REFUSED and not runner.argv and kept:
        return ok("an open maintenance window is never taken twice or forced: exclusive create, refused with nothing stopped")
    return bad(f"marker guard: acquire {first}/{second}, window exit {code}, systemctl calls {len(runner.argv)}, marker kept {kept}")


@case("setup.restart-lock", "setup-live")
def _restart_lock(env: Env) -> Result:
    from . import openclaw_host as host
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        path = Path(d, "lock")
        one, two = host.acquire_restart_lock(path), None
        try:
            two = host.acquire_restart_lock(path)
        finally:
            host.release_restart_lock(one)
        three = host.acquire_restart_lock(path)
        host.release_restart_lock(three)
    if one is not None and two is None and three is not None:
        return ok("one restart lock: a second path is refused while it is held and can take it once released")
    return bad(f"restart lock: first {one is not None}, second {two is not None}, after release {three is not None}")


@case("setup.active-turns-defer", "setup-live")
def _defer(env: Env) -> Result:
    from . import openclaw_host as host
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        busy, r1, m1 = _window(Path(d, "busy"), busy=2)
        blind, r2, m2 = _window(Path(d, "blind"), busy=None)
    if busy == blind == host.EXIT_REFUSED_BUSY and not (r1.argv or r2.argv) and not (m1.exists() or m2.exists()):
        return ok("a restart is deferred (exit 6, nothing stopped, no marker left) while agent turns are in flight or the probe cannot tell")
    return bad(f"active turns: exits {busy}/{blind}, systemctl calls {len(r1.argv) + len(r2.argv)}")


@case("setup.cgroup-t29", "setup-live")
def _t29(env: Env) -> Result:
    from . import openclaw_host as host
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        inside, r1, _ = _window(Path(d, "inside"), cgroup=f"0::/user.slice/{host.GATEWAY_UNIT}\n")
        unknown, r2, _ = _window(Path(d, "unreadable"), make_cgroup=False)
    if inside == unknown == host.EXIT_REFUSED_SELF and not (r1.argv or r2.argv):
        return ok("a process inside the gateway cgroup, or one that cannot tell, refuses to restart it (T29); nothing stopped")
    return bad(f"T29: exits {inside}/{unknown}, systemctl calls {len(r1.argv) + len(r2.argv)}")


@case("setup.reload-rules", "setup-live", severity="warn")
def _reload_rules(env: Env) -> Result:
    from . import openclaw_host as host, openclaw_reload_rules as rules
    with tempfile.TemporaryDirectory(prefix="ai-resources-acc-") as d:
        shim = Path(d, ".openclaw", "tmp", "agent-cli", "openclaw")
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/bin/sh\nexec node x\n")
        shim.chmod(0o755)
        shim_ok = host.is_openclaw_shim(shim) and str(shim) not in host.real_openclaw_candidates(str(shim.parent))
    if not shim_ok:
        return bad("a per-session shim under ~/.openclaw/tmp is not recognised, so the rules could be read from it")
    if not env.which("openclaw"):
        return skip("openclaw is not installed; the shim guard holds but the installed reload table cannot be read")
    rs = rules.current_ruleset(env.runner or host.default_runner)
    if rs.source != "installed" or rs.installed_count < rules.MIN_RULES:
        return bad(f"the installed OpenClaw's reload table is unreadable: {rs.reason or rs.installed_count}")
    return ok(f"shims are skipped and the installed reload table ({rs.installed_count} rules, openclaw {rs.version}) parses")


@case("setup.idempotent-rerun", "setup-live")
def _idempotent(env: Env) -> Result:
    from .setup.cockpits import openclaw_orchestration as orch
    live = _openclaw_doc()
    doc = live if orch.kit_role_ids(live) else {"agents": {"defaults": {}, "entries": {
        "main": {}, "claude": {}, "kit-role-explore": {}, "kit-role-planner": {}}}}
    orchs, ids = orch.orchestrator_ids(doc), orch.kit_role_ids(doc)
    first = orch._merge(orch._merge(doc, orch.wiring_patch(doc, ids, orchs)), orch.limits_patch(doc))
    second = (orch.wiring_patch(first, ids, orchs), orch.limits_patch(first))
    if any(second):
        return bad("a second `setup --auto` pass on the already-applied document would still change it", second=second)
    return ok(f"applying the orchestration wiring twice on a {'copy of the live' if doc is live else 'fixture'} document: the second pass is a no-op")


@case("setup.credits-ledger", "setup-live", severity="warn", remedy="`ai-resources credits collect`")
def _credits(env: Env) -> Result:
    from . import credits
    path = credits.ledger_path()
    return ok(f"the credits ledger is present ({path})") if path.is_file() else bad(f"the credits ledger {path} does not exist")


# Operator-run: they kill turns or the gateway. Listed with the exact command, never executed.
_OPERATOR = {
    "setup.sigterm-window": ("SIGTERM during a drained window removes watchdog.off and starts the gateway again",
        "ai-resources openclaw doctor --even-if-busy & pid=$!; sleep 5; kill -TERM $pid; wait $pid; "
        "test ! -e ~/.openclaw/watchdog.off && systemctl --user is-active openclaw-gateway.service"),
    "setup.real-restart": ("one drained restart clears a stale plugin / MCP overflow and health answers after it",
        "ai-resources openclaw reconcile && ai-resources verify --live"),
    "setup.watchdog": ("the watchdog stands down while watchdog.off exists and resumes when it is removed",
        "touch ~/.openclaw/watchdog.off && systemctl --user start openclaw-watchdog.service && "
        "journalctl --user -u openclaw-watchdog.service -n 5 --no-pager; rm -f ~/.openclaw/watchdog.off"),
    "spawn.from-main": ("main spawns kit-role-explore through sessions_spawn (needs a model turn on main)",
        "openclaw agent --agent main -m 'Use sessions_spawn to run the agent kit-role-explore with the task: reply pong' --json --timeout 180"),
}
for _id, (_what, _cmd) in _OPERATOR.items():
    CASES.append(Case(_id, _id.split(".")[0] if _id.startswith("spawn") else "setup-live",
                      lambda env, w=_what, c=_cmd: skip(f"operator-run ({w}): `{c}`"), operator=_cmd, offline=False, cap="never run by acceptance"))


# --- running -----------------------------------------------------------------------------------------------------------

def select(group: str = "", ids: tuple[str, ...] = (), offline: bool = False, operator: bool | None = None) -> list[Case]:
    """The cases of a group/ids; `operator` True = only the operator-run ones, False = none of them, None = all (they show as SKIP)."""
    return [c for c in CASES if (not group or c.group == group) and (not ids or c.id in ids)
            and (operator is None or bool(c.operator) == operator) and (c.offline or not offline)]


def _scrub_obj(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _scrub_obj(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub_obj(x) for x in v]
    return v if isinstance(v, (int, float, bool)) or v is None else scrub(v)


def run_case(c: Case, env: Env) -> Result:
    try:
        res = c.run(env)
    except Exception as e:  # noqa: BLE001 - a case must never take the run down
        res = bad(f"the case crashed: {type(e).__name__}: {e}")
    return Result(res.status, scrub(" ".join(str(res.reason).split())), _scrub_obj(res.evidence))


def run_cases(cases: list[Case], env: Env) -> list[tuple[Case, Result]]:
    return [(c, run_case(c, env)) for c in cases]


def table(results: list[tuple[Case, Result]]) -> list[str]:
    return [f"{c.id} | {r.status} | {r.reason[:220]}" for c, r in results]


def document(results: list[tuple[Case, Result]]) -> dict:
    counts = {s: sum(r.status == s for _c, r in results) for s in (PASS, FAIL, SKIP)}
    return {"schema": SCHEMA, "summary": counts, "results": [
        {"id": c.id, "group": c.group, "status": r.status, "reason": r.reason, "evidence": r.evidence} for c, r in results]}


def cmd_acceptance(args: argparse.Namespace) -> int:
    chosen = select(args.group or "", tuple(args.case or ()), args.offline)
    if args.list:
        for c in select(args.group or "", tuple(args.case or ())):
            print(f"{c.id} | {c.group} | {'operator' if c.operator else 'offline' if c.offline else 'real call'}")
        return 0
    if args.operator:
        for c in select(args.group or "", tuple(args.case or ()), operator=True):
            print(f"{c.id} | {c.operator}")
        return 0
    if args.dry_run:
        for c in chosen:
            print(f"{c.id} | PLAN | {'operator-run, listed only' if c.operator else c.cap}")
        return 0
    env = Env(offline=args.offline, usage_file=args.usage_file or "")
    results = run_cases(chosen, env)
    if args.json:
        print(json.dumps(document(results), indent=2))
    else:
        print("\n".join(table(results)))
        c = document(results)["summary"]
        print(f"-- {c[PASS]} PASS, {c[FAIL]} FAIL, {c[SKIP]} SKIP")
    return 1 if any(r.status == FAIL for _c, r in results) else 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("acceptance", help="Prove the routing matrix, fallbacks, spawn wiring and setup behaviour (one row per case)")
    p.add_argument("--group", choices=GROUPS, help="run one group only")
    p.add_argument("--case", action="append", metavar="ID", help="run this case only (repeatable)")
    p.add_argument("--json", action="store_true", help="the results as one JSON document (schema, summary, results)")
    p.add_argument("--list", action="store_true", help="list the cases and whether each makes a real call")
    p.add_argument("--operator", action="store_true", help="list the cases that kill turns, with the exact command (never run)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and the cost cap of each case; no call is made")
    p.add_argument("--offline", action="store_true", help="only the cases that need no real model call (fixtures and fakes)")
    p.add_argument("--usage-file", metavar="FILE", help="JSON {pool: used fraction} instead of the live pool usage")
    p.set_defaults(func=cmd_acceptance)
