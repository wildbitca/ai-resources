"""`ai-resources acceptance`: the proof that what setup configured actually behaves as designed (ADR-0019 D8).

One registry of cases, one row per case (`id | PASS/FAIL/SKIP/N/A | reason`), eight groups. `N/A` is a component the
host does not use (a removed or unconfigured provider): it is not a gap, so it is not a SKIP.

* route         every role resolves through the matrix, ONE minimal real call per distinct (backend, model) cell, and the
                model/pool the call actually used (the response, or the dispatch row) must equal the matrix cell;
* fault         injected faults (pool at 0.85, bad key, timeout, agy missing, quota text): the fallback fires exactly once
                with the recorded cause, offline with fakes;
* checker       a checker never shares a family with the writers (or is blocked, explicitly);
* spawn         every `kit-role-*` agent is spawnable from `main` and the channel agents; peak gateway memory is sampled; a
                subagent main spawned landed on the right kit-role agent and provider (read from the gateway's own sessions);
* team          `main` can take each of the 21 roles; one workflow runs its roles on different providers with a checker on
                another provider than the implementer; CLI and API routes appear together in one run;
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
from . import scratch as _scratch
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple

from . import dispatch_ledger
from . import scrub as _scrub
from .scoring import routing

PASS, FAIL, SKIP, NA = "PASS", "FAIL", "SKIP", "N/A"
GROUPS = ("route", "fault", "checker", "spawn", "team", "gemini-tools", "agy-write", "setup-live")
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


def na(reason: str, **ev: Any) -> Result:
    """Not applicable: the component is removed or was never configured on this host. Neither a gap nor a failure."""
    return Result(NA, reason, ev)


def agent_exec_context(environ: "dict[str, str] | None" = None) -> bool:
    """True inside an agent's shell tool or a subagent's: OpenClaw refuses `openclaw agent` / sessions RPCs there because
    a CLI call cannot carry the inter-session attribution. The guard is respected, never bypassed."""
    e = os.environ if environ is None else environ
    return e.get("OPENCLAW_SHELL") == "exec" or e.get("OPENCLAW_SUBAGENT_EXEC") == "1"


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
    doc: Callable[[], dict] | None = None        # the live openclaw.json (a seam for tests); None = read it through the cockpit
    config_mtime_ms: Callable[[], float] | None = None   # when that config last changed (a seam for tests); None = stat the file

    def openclaw_doc(self) -> dict:
        if self.doc is not None:
            return self.doc()
        from .setup.cockpits import openclaw as cockpit
        return cockpit.read_config(cockpit.config_path())

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
    root = Path(_scratch.new_tmpdir("sandbox-"))
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
    scratch = _scratch.tmp_root() / "acceptance-scratch" / "route"
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


def _kit_role_sessions(env: Env, agent: str, limit: int = 20) -> list[dict]:
    """The sessions of `agent` that another session spawned (`spawnedBy`), newest STARTED first. Read-only (`openclaw sessions`).

    Ordered by `sessionStartedAt`, not `updatedAt`: any later touch of an old session (archive, cleanup, a status write)
    bumps `updatedAt`, which put a spawn from before a config fix ahead of the newer spawns that proved the fix."""
    rc, out = _oc(env, ["openclaw", "sessions", "--agent", agent, "--json", "--limit", str(limit)], 60)
    doc = _json_from(out) if rc == 0 else {}
    rows = [x for x in (doc.get("sessions") or []) if isinstance(x, dict) and x.get("spawnedBy")]
    return sorted(rows, key=lambda x: x.get("sessionStartedAt") or x.get("updatedAt") or 0, reverse=True)


def _config_mtime_ms(env: Env) -> float:
    """When the live OpenClaw config last changed (ms); 0 when unknown (every session then counts)."""
    if env.config_mtime_ms is not None:
        return float(env.config_mtime_ms())
    try:
        from .setup.cockpits import openclaw as cockpit
        return Path(cockpit.config_path()).stat().st_mtime * 1000
    except OSError:
        return 0.0


@case("spawn.explore-run", "spawn", offline=False, cap="one `openclaw agent --agent <kit-role explore> -m pong` turn (the role's own model); "
      "inside an agent session no turn is made: the spawn is read from the gateway's own sessions")
def _spawn_explore(env: Env) -> Result:
    from .setup.cockpits import openclaw_roles
    oc = env.which("openclaw")
    if not oc:
        return skip("openclaw is not on PATH")
    wired = _spawn_allow(env)
    if wired.status != PASS:
        return skip(f"spawn.allow is {wired.status} ({wired.reason[:100]}); fix the wiring first. Command in --operator")
    agent = openclaw_roles.agent_id_for("explore")
    if agent_exec_context():
        # `openclaw agent` is refused inside an agent's exec (no inter-session attribution): the case uses sessions_spawn
        # semantics instead. A subagent that main spawned on this agent is a session the gateway recorded with `spawnedBy`.
        rows = _kit_role_sessions(env, agent)
        done = [r for r in rows if r.get("status") == "done"]
        if done:
            r = done[0]
            return ok(f"{agent} answered a sessions_spawn from {r['spawnedBy'].split(':')[1]} (session {r.get('sessionId', '')[:8]}, "
                      f"{r.get('modelProvider')}/{r.get('model')}); `openclaw agent` is not used inside an agent session",
                      agent=agent, session=r.get("key"), spawned_by=r.get("spawnedBy"), model=r.get("model"))
        return skip(f"inside an agent session `openclaw agent` is refused by OpenClaw and {agent} has no finished spawned session yet; "
                    f"have main call sessions_spawn(agentId=\"{agent}\", task=\"reply pong\") or run the case from an operator terminal")
    before = _memory_sample(env)
    rc, out = _run_tool([oc, "agent", "--agent", agent, "-m", PROMPT, "--json", "--timeout", "90"], timeout=150)
    after = _memory_sample(env)
    if rc != 0 or "pong" not in out.lower():
        return skip(f"{agent} is not reachable: {scrub(out[-160:])}") if _UNAUTH.search(out) else bad(f"{agent} did not answer: {scrub(out[-200:])}")
    return ok(f"{agent} answered through the gateway", agent=agent, memory_before=before, memory_after=after)


@case("spawn.landed-on-role-agent", "spawn", severity="warn", remedy="re-run `ai-resources setup` (it rewrites the kit-role agents from the matrix)")
def _spawn_landed(env: Env) -> Result:
    """A subagent main spawned on a kit-role agent runs on that agent's own model chain. Two proofs:

    * the config: OpenClaw resolves a spawn as `<target>.subagents.model`, then `agents.defaults.subagents.model`, then the
      target's own model; the model that order gives must be in the chain the agent is configured with (always checkable);
    * the newest spawned session (by start time) that began after the config last changed: the gateway's row names the
      agent in its key and the provider/model it ran. A session from before the last config change says nothing about
      today's config, so it is reported as unproven, never as a failure."""
    from .setup.cockpits import openclaw_orchestration as orch, openclaw_roles
    doc = env.openclaw_doc()
    ids = orch.kit_role_ids(doc)
    if not ids:
        return skip("no kit-role agents in the OpenClaw config")
    by_agent = {openclaw_roles.agent_id_for(r): r for r in routing.ROLES}
    resolved = orch.audit_spawn_models(doc, [a for a in ids if a in by_agent])
    since = _config_mtime_ms(env)
    checked, wrong, unproven, stale = [], [], [], []
    for aid in ids:
        role = by_agent.get(aid)
        entry = orch._get(doc, "agents", "entries", aid, "model") or {}
        chain = [entry] if isinstance(entry, str) else [entry.get("primary"), *(entry.get("fallbacks") or [])]
        families = {routing.writer_family(str(c)) for c in chain if c}

        def in_chain(ref: str) -> bool:
            return ref in chain or routing.writer_family(ref) in families
        if aid in resolved and not in_chain(resolved[aid][1]):
            layer, model = resolved[aid]
            wrong.append(f"{aid}: a spawn resolves to {model} ({layer}), the agent's chain is {', '.join(map(str, chain))}; "
                         f"fix: openclaw config set agents.entries.{aid}.subagents.model.primary {chain[0]}")
            continue
        rows = _kit_role_sessions(env, aid, 5) if role else []
        if not rows:
            unproven.append(aid)
            continue
        row = rows[0]
        if (row.get("sessionStartedAt") or 0) < since:
            stale.append(aid)                  # ran before the config last changed: the config proof above is the evidence
            continue
        key_agent = str(row.get("key", "")).split(":")[1:2]
        ref = f"{row.get('modelProvider')}/{row.get('model')}"
        if key_agent != [aid] or not in_chain(ref):
            wrong.append(f"{aid}: session {row.get('key', '')[:40]} ran {ref}, the agent's chain is {', '.join(map(str, chain))}")
        else:
            checked.append(aid)
    if wrong:
        return bad("; ".join(wrong[:3]), wrong=wrong)
    cfg_ok = [a for a in resolved]
    if not checked and not cfg_ok:
        return skip("no kit-role agent has a spawned session yet; have main call sessions_spawn on one (the check reads the gateway's sessions)")
    return ok(f"{len(cfg_ok)} kit-role agent(s) resolve a spawn to a model of their own chain; {len(checked)} spawned session(s) confirm it"
              + (f"; {len(stale)} last spawn(s) predate the current config and prove nothing" if stale else "")
              + (f" ({len(unproven)} not spawned yet)" if unproven else ""),
              proven=checked, unproven=unproven, stale=stale, resolved={a: v[1] for a, v in resolved.items()})


# --- team: main assumes the roles, a mixed-provider run, CLI and API together -----------------------------------------------

_API_BACKENDS = ("anthropic-api", "gemini-api")


@case("team.main-assumes-21-roles", "team", severity="warn", remedy="re-run `ai-resources setup`")
def _team_roles(env: Env) -> Result:
    """OpenClaw `main` can take any of the 21 roles: each resolves through the matrix to a route that is not blocked, a team
    role has its instruction file and a kit-role agent main may spawn, a chat role runs on an API route main owns."""
    from .setup import detection
    from .setup.cockpits import openclaw_orchestration as orch, openclaw_roles
    kit = Path(env.kit())
    doc = env.openclaw_doc()
    live_ids = set(orch.kit_role_ids(doc))
    allow = orch._get(doc, "agents", "entries", "main", "subagents", "allowAgents") or []
    problems: list[str] = []
    for role, row in routing.ROLES.items():
        route = routing.resolve(role, {}, [], detection.inactive_backends())
        if route.blocked:
            problems.append(f"{role}: no route ({route.reason[:60]})")
            continue
        if row.kind == "team":
            if not (kit / "agents" / "roles" / f"{role}.md").is_file():
                problems.append(f"{role}: no role file agents/roles/{role}.md")
            aid = openclaw_roles.agent_id_for(role)
            if live_ids and aid not in live_ids:
                problems.append(f"{role}: agent {aid} is not registered")
            elif live_ids and "*" not in allow and aid not in allow:
                problems.append(f"{role}: main may not spawn {aid}")
        elif route.backend not in _API_BACKENDS:
            problems.append(f"{role}: chat role on {route.backend}, expected an API route")
    if len(routing.ROLES) != 21:
        problems.append(f"the matrix has {len(routing.ROLES)} roles, expected 21")
    return bad("; ".join(problems[:4]), problems=problems) if problems else ok(
        "main can take all 21 roles: 15 team roles (role file, kit-role agent, spawn allowed) and 6 chat roles (API route)", roles=21)


def _backend_of(route: "routing.Route") -> str:
    return "claude-code" if route.backend in ("claude-code", "claude-cli") else route.backend


@case("team.cross-provider-run", "team", severity="warn")
def _team_run(env: Env) -> Result:
    """One workflow, different providers per role, a checker on another provider than the implementer. Offline: the real engine
    runs `feature-cross-provider`; a fake runtime answers every dispatch, resolving the role through the matrix against the
    writers that already wrote in the run (the resolver `ai-resources route` and the engine's checker choice use)."""
    from .orchestrator import engine as eng, workflow_loader as wl
    from .orchestrator.state import RunStore
    from .setup import detection
    base = _scratch.tmp_root() / "acceptance-scratch"
    base.mkdir(parents=True, exist_ok=True)
    with _scratch.tempdir("team-") as d:
        root = Path(d)
        (root / "ws").mkdir()
        wf = wl.load(str(Path(env.kit()) / "workflows" / "feature-cross-provider.workflow.yaml"))
        engine = eng.Engine(RunStore(root / "runs"), kit_root=Path(env.kit()), claims_root=root / "claims")
        run = engine.start(wf, "acceptance: prove the provider mix", str(root / "ws"))
        if run["status"] == "failed":
            return bad(f"the run could not start: {run['failure'][:200]}")
        seen: dict[str, "routing.Route"] = {}
        writers: list[str] = []
        res = engine.next(run["id"])
        for _ in range(80):
            acts = res["actions"]
            if any(a["type"] == "ask" for a in acts):
                res = engine.approve(run["id"])
                continue
            dispatches = [a for a in acts if a["type"] == "dispatch"]
            if not dispatches:
                break
            for a in dispatches:
                role = a["role"]
                route = routing.resolve(role, {}, writers, detection.inactive_backends())
                if route.blocked:
                    return bad(f"{role} is blocked in the run: {route.reason[:120]}")
                seen[role] = route
                if role in eng.WRITER_ROLES:
                    writers.append(route.model)
                verdict = "Verdict: READY\n" if role == "verifier" else "Verdict: PASS\n" if role in ("tester", "code-reviewer", "security-auditor") else ""
                res = engine.report(run["id"], a["step"], text="Status: success\nBlocked: false\n" + verdict,
                                    session=f"acc-{a['step']}", agent=a["agent"], model=a.get("model") or route.model)
        final = engine.store.load(run["id"])["status"]
    if final != "completed":
        return bad(f"the mixed-provider run ended {final}, not completed", status=final)
    return _judge_team(seen, detection.inactive_backends())


def _judge_team(seen: "dict[str, routing.Route]", inactive: "Iterable[str]" = ()) -> Result:
    backends = {role: _backend_of(r) for role, r in seen.items()}
    kinds = sorted(set(backends.values()))
    impl, checker = seen.get("implementer"), seen.get("verifier")
    problems = []
    if len(kinds) < 2:
        return skip(f"only {kinds} appeared: a mixed team needs a second provider signed in here "
                    f"(inactive: {', '.join(sorted(inactive)) or 'none'})", backends=backends)
    if impl and checker and (_backend_of(impl) == _backend_of(checker) and routing.writer_family(impl.model) == routing.writer_family(checker.model)):
        problems.append("the checker shares the implementer's provider and model family")
    if impl and checker and routing.writer_family(impl.model) == routing.writer_family(checker.model):
        problems.append("the checker shares the implementer's model family")
    return bad("; ".join(problems), backends=backends) if problems else ok(
        "roles ran on " + " + ".join(kinds) + f"; the checker ({checker.cell if checker else '?'}) is not the implementer's "
        f"({impl.cell if impl else '?'}) family", backends=backends)


@case("team.cli-and-api-one-run", "team", offline=False, severity="warn",
      cap="one minimal call on each of agy, claude-code, anthropic-api and gemini-api (the same calls route.* makes, reused when they ran)")
def _team_cli_and_api(env: Env) -> Result:
    """CLI routes (agy, claude-code) and API routes (anthropic-api, gemini-api) in the same run id: each provider that is signed in
    here answered a minimal call whose model and pool matched the matrix (and was recorded in the dispatch ledger of the run)."""
    probes = {"planner": "claude-code", "explore": "agy", "main": "anthropic-api", "utility": "gemini-api"}
    answered, absent = {}, {}
    for role, backend in probes.items():
        r = _route_case(role)(env)
        if r.status == PASS:
            answered[backend] = role
        elif r.status == SKIP:
            absent[backend] = r.reason[:80]
        else:
            return bad(f"{role} on {backend} failed: {r.reason[:160]}")
    if not answered:
        return skip("no provider answered here: " + "; ".join(f"{b}: {why}" for b, why in absent.items()))
    # `_route_case` asserts model AND pool of each answer and writes the dispatch row; a provider counts only when it passed.
    in_ledger = sorted(answered)
    cli = [b for b in in_ledger if b in ("agy", "claude-code", "claude-cli")]
    api = [b for b in in_ledger if b in _API_BACKENDS]
    if not (cli and api):
        return skip(f"this run only reached {in_ledger or 'nothing'}; both a CLI and an API provider must be signed in "
                    "(absent: " + "; ".join(f"{b}: {why}" for b, why in absent.items()) + ")")
    return ok(f"one run id used CLI ({', '.join(cli)}) and API ({', '.join(api)}) routes together", backends=in_ledger,
              absent=sorted(absent))


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


def _gemini_routes(env: Env) -> "dict[str, bool]":
    """Which Gemini routes this host really uses: `gemini-api` (a Google auth profile or key in the gateway) and `agy`
    (Antigravity installed). A route that is not configured is not part of the setup, so its proof is N/A."""
    from . import agy_run
    try:
        doc = env.openclaw_doc()
    except Exception:  # noqa: BLE001 - an unreadable config is "unknown": the probe itself will tell
        doc = {}
    if doc:
        profiles = ((doc.get("auth") or {}).get("profiles")) or {}
        api = any(str((p or {}).get("provider")) == "google" for p in profiles.values()) or bool(
            ((doc.get("plugins") or {}).get("entries") or {}).get("google", {}).get("enabled"))
    else:
        api = True
    return {"gemini-api": api or bool(os.environ.get("GEMINI_API_KEY")), "agy": bool(agy_run.agy_bin())}


@case("gemini.tool-call", "gemini-tools", offline=False,
      cap="one `openclaw agent exec` turn on google/gemini-3.8-flash with one file-read tool call (gemini-api), and the agy.write-scope "
          "file write when gemini-api is not configured")
def _gemini_tools(env: Env) -> Result:
    """Gemini tool-calling, proven on the routes the host actually uses: the Gemini API through OpenClaw (a file-read tool call),
    else Antigravity's Gemini (the file write of `agy.write-scope`). A provider that is removed or not configured is N/A."""
    from . import retired
    routes = _gemini_routes(env)
    if not any(routes.values()):
        return na("no Gemini route is configured on this host (no Google auth profile or key, no agy); nothing to prove")
    oc = env.which("openclaw")
    model = "google/gemini-3.8-flash"
    if routes["gemini-api"] and oc:
        scratch = _scratch.tmp_root() / "acceptance-scratch" / "gemini-tools"
        scratch.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(6)
        (scratch / "token.txt").write_text(token + "\n")
        rc, out = _run_tool([oc, "agent", "exec", "--model", model, "--cwd", str(scratch), "--json", "--timeout", "120",
                             "Use your file reading tool to read token.txt in the working directory and reply with only its exact content."], timeout=180)
        doc = _json_from(out)
        if doc and doc.get("ok") is False or (rc != 0 and not doc):
            why = scrub(str((doc.get("error") or {}).get("message") if doc else out[-160:]))[:160]
            keys = {k for c in retired.RETIRED for k in c.env_keys}
            if any(k in why for k in keys):
                # a dangling reference to a component setup retired: a defect of the config, not a Gemini result
                return bad(f"the agent turn failed on a removed provider's secret ({why}); run `ai-resources setup` (it removes the "
                           "leftovers of retired components)")
            if routes["agy"]:
                return _agy_tool_proof(env, f"the Gemini API turn could not run here ({why[:80]})")
            return skip(f"OpenClaw could not run the agent turn here ({why}); Gemini tool-calling stays unproven and the heartbeat on Haiku")
        passed = rc == 0 and token in out
        env.cache.mkdir(parents=True, exist_ok=True)
        gemini_verdict_path(env.cache).write_text(json.dumps(
            {"verdict": "pass" if passed else "fail", "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
        return ok(f"{model} called the file tool and returned the token; the heartbeat may move to Gemini", route="gemini-api") if passed else bad(
            f"{model} did not return the file content through a tool call (exit {rc}): {scrub(out[-160:])}")
    if routes["agy"]:
        return _agy_tool_proof(env, "the Gemini API route is not configured" if not routes["gemini-api"] else "openclaw is not on PATH")
    return skip("openclaw is not on PATH")


def _agy_tool_proof(env: Env, why: str) -> Result:
    r = _agy_write(env)
    if r.status == PASS:
        return ok(f"{why}; Antigravity's Gemini called its file tool (agy.write-scope): " + r.reason, route="agy")
    return r


# --- agy writes in a scratch dir ---------------------------------------------------------------------------------------

@case("agy.write-scope", "agy-write", offline=False, cap="one `agy run --role doc-writer` call (agy flash, ~100k tokens of the AI Pro quota)")
def _agy_write(env: Env) -> Result:
    from . import agy_run
    if not agy_run.agy_bin():
        return skip("agy is not installed (no agy on PATH or in ~/.local/bin)")
    scratch = _scratch.tmp_root() / "acceptance-scratch" / "agy-write"
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
    if why:
        root = str(plugin.get("rootDir") or "?")
        detail = {"path": f"the running plugin dir is {root} but the expected one is {stable}",
                  "version": f"the running version is {running or '?'} and the disk has {disk or '?'}"}.get(
            why, f"the gateway process started before the kit on disk was installed (running {running or '?'}, disk {disk or '?'})")
        return bad(f"the gateway runs older kit plugin code than the disk ({why} rule): {detail}", stale=why)
    if disk and running and running != disk:
        return bad(f"the running version is {running} but the disk has {disk} (version rule)", stale="version")
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
    if not cfg.get("OPENCLAW_OFFBOX_BUCKET"):
        return skip("no off-box backup is configured on this host")
    # Self-healing: the kit-derived file has no secret in it and is regenerated from the host federation when it is gone
    # (a missing file made the uploader fail with "not readable" on 2026-10-27 and 10-28).
    status, detail = host.offbox_cred_repair(cfg)
    if status == "failed":
        return bad(("the off-box credential file named by OPENCLAW_OFFBOX_CRED_FILE is missing (" + detail + ")") if "missing" in detail else detail)
    cred = cfg.get("OPENCLAW_OFFBOX_CRED_FILE") or str(host.OFFBOX_WIF_CRED_PATH)
    try:
        mode = os.stat(cred).st_mode & 0o777
    except OSError:
        return bad("the off-box credential file named by OPENCLAW_OFFBOX_CRED_FILE is missing")
    if mode != 0o600:
        return bad(f"the off-box credential file has mode {mode:o}, not 600")
    return ok("the off-box credential file was missing and was regenerated (0600)") if status == "repaired" \
        else ok("the off-box credential file exists (0600)")


@case("setup.offbox-regenerate", "setup-live")
def _offbox_regen(env: Env) -> Result:
    from . import openclaw_host as host
    with _scratch.tempdir("ai-resources-acc-") as d:
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
    with _scratch.tempdir("ai-resources-acc-") as d:
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
    with _scratch.tempdir("ai-resources-acc-") as d:
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
    with _scratch.tempdir("ai-resources-acc-") as d:
        busy, r1, m1 = _window(Path(d, "busy"), busy=2)
        blind, r2, m2 = _window(Path(d, "blind"), busy=None)
    if busy == blind == host.EXIT_REFUSED_BUSY and not (r1.argv or r2.argv) and not (m1.exists() or m2.exists()):
        return ok("a restart is deferred (exit 6, nothing stopped, no marker left) while agent turns are in flight or the probe cannot tell")
    return bad(f"active turns: exits {busy}/{blind}, systemctl calls {len(r1.argv) + len(r2.argv)}")


@case("setup.cgroup-t29", "setup-live")
def _t29(env: Env) -> Result:
    from . import openclaw_host as host
    with _scratch.tempdir("ai-resources-acc-") as d:
        inside, r1, _ = _window(Path(d, "inside"), cgroup=f"0::/user.slice/{host.GATEWAY_UNIT}\n")
        unknown, r2, _ = _window(Path(d, "unreadable"), make_cgroup=False)
    if inside == unknown == host.EXIT_REFUSED_SELF and not (r1.argv or r2.argv):
        return ok("a process inside the gateway cgroup, or one that cannot tell, refuses to restart it (T29); nothing stopped")
    return bad(f"T29: exits {inside}/{unknown}, systemctl calls {len(r1.argv) + len(r2.argv)}")


@case("setup.reload-rules", "setup-live", severity="warn")
def _reload_rules(env: Env) -> Result:
    from . import openclaw_host as host, openclaw_reload_rules as rules
    with _scratch.tempdir("ai-resources-acc-") as d:
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


def _role_drift(env: Env) -> "list | None":
    """The drifts between the matrix and the live kit-role agents, or None when the host has no kit-role agent."""
    from . import role_drift
    from .setup import lanes_render
    doc = env.openclaw_doc()
    if not any(a.startswith("kit-role-") for a in role_drift.live._entries(doc)):
        return None
    try:
        profile_doc = lanes_render.routing_doc(env.setup_state())
    except Exception:  # noqa: BLE001 - an unreadable profile means the default matrix
        profile_doc = {}
    return role_drift.check(doc, profile_doc)


@case("setup.role-matrix-drift", "setup-live",
      remedy="`ai-resources setup --dry-run` shows the model/fallbacks it would reconcile; then re-run `ai-resources setup`")
def _role_matrix_drift(env: Env) -> Result:
    from . import role_drift
    drifts = _role_drift(env)
    if drifts is None:
        return skip("no kit-role-* agent in the live OpenClaw config")
    bad_ones = role_drift.mismatches(drifts)
    if bad_ones:
        return bad(f"{len(bad_ones)} kit-role field(s) differ from the routing matrix: " + "; ".join(d.line() for d in bad_ones),
                   drift=[d.line() for d in bad_ones])
    return ok("every kit-role agent's model, fallbacks and spawn model equal its routing matrix row")


@case("setup.role-billing-pool", "setup-live", severity="warn",
      remedy="pin the Max cell to the claude-cli runtime (`ai-resources setup` does it for kit-role agents it owns)")
def _role_billing_pool(env: Env) -> Result:
    from . import role_drift
    drifts = _role_drift(env)
    if drifts is None:
        return skip("no kit-role-* agent in the live OpenClaw config")
    pool = role_drift.billing(drifts)
    if pool:
        return bad(f"{len(pool)} Max-subscription role(s) would bill the Anthropic API: " + "; ".join(d.line() for d in pool),
                   drift=[d.line() for d in pool])
    return ok("no Max-primary kit-role agent runs on the Anthropic API pool")


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
    counts = {s: sum(r.status == s for _c, r in results) for s in (PASS, FAIL, SKIP, NA)}
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
        print(f"-- {c[PASS]} PASS, {c[FAIL]} FAIL, {c[SKIP]} SKIP, {c[NA]} N/A")
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
