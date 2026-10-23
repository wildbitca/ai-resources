"""The orchestrator engine (ADR-0015 D3): a pure, resumable state machine over a kit workflow.

The seam with the runtime is narrow and deliberate:

* `start` registers a run (queued behind `maxTeams` teams, or running with its own worktree and branch);
* `next` returns the ACTIONS a runtime must perform (dispatch role R with prompt P in directory W within T seconds; ask
  the operator to approve the plan; wait; stop). It dispatches nothing itself and never sleeps or polls;
* `report` feeds a finished action back (a reply, a timeout or an error) and returns the next actions;
* `approve`, `cancel`, `resume` and `status` are the operator's verbs.

Rules the engine enforces: steps run in workflow order; a parallel group runs together only when every member is a
read-only role, and joins before the next step; `Blocked` with `Return_to_step` returns to the named step at most
`MAX_RETURNS` (3) times, the next one fails the run; only the verifier signs a run off (a `READY` from any other role is
rejected) and it must be independent of the writers (distinct session where the runtime reports one, distinct agent id and
model family always); the operator approves the plan before the first step that writes; each role has a timeout.
State changes are written atomically under a per-run lock, so a killed process resumes at the same step.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from .. import domains, repo_root, roles_policy
from ..setup.cockpits import openclaw_roles
from . import limits, reply as reply_mod, worktree as worktree_mod
from .state import RunStore, StateError, new_run_id
from .workflow_loader import CHECKER_ROLES, READ_ONLY_ROLES, VERIFIER_ROLE, WRITER_ROLES, Step, Workflow, WorkflowError

MAX_RETURNS = 3
HANDOFF_KEYS = ("workflow", "step", "status", "blocked", "return_to_step", "block_reason", "domain", "requires_tests",
                "security_critical", "refs", "verdicts", "next", "docs_updated", "git_integration")
TERMINAL = limits.FINISHED
Clock = Callable[[], float]


class EngineError(RuntimeError):
    """The request is refused (wrong state, unknown step). `code` is stable for the CLI and the plugin."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------------------------------------------------
# helpers

def _wf_to_state(wf: Workflow) -> list[dict[str, Any]]:
    return [{"id": s.id, "role": s.role, "skills": list(s.skills), "group": s.group, "handoff_to": s.handoff_to,
             "return_to": s.return_to, "backend": s.backend, "prompt_template": s.prompt_template,
             "entry_criteria": s.entry_criteria, "exit_criteria": s.exit_criteria} for s in wf.steps]


def _steps_of(run: Mapping[str, Any]) -> list[Step]:
    return [Step(id=d["id"], role=d["role"], skills=tuple(d.get("skills") or ()), group=d.get("group") or "",
                 prompt_template=d.get("prompt_template") or "", entry_criteria=d.get("entry_criteria") or "",
                 exit_criteria=d.get("exit_criteria") or "", handoff_to=d.get("handoff_to") or "",
                 return_to=d.get("return_to") or "", backend=d.get("backend") or "claude-code")
            for d in run["workflow_steps"]]


def _groups(steps: list[Step]) -> list[list[Step]]:
    out: list[list[Step]] = []
    for s in steps:
        if s.group and out and out[-1][-1].group == s.group:
            out[-1].append(s)
        else:
            out.append([s])
    return out


def _event(run: dict[str, Any], kind: str, step: str = "", text: str = "", now: float = 0.0) -> None:
    run.setdefault("events", []).append({"t": round(now, 3), "type": kind, "step": step, "text": text[:300]})


def family_of(ref: Any) -> str | None:
    return roles_policy.model_family(ref)


def agent_for(role: str, domain: str, kit_root: Path) -> str:
    """The `kit-role-*` agent a role runs on; the persona agent when the run's domain has an active persona."""
    if domain and domain in domains.DOMAINS and domain in domains.active_domains() \
            and (kit_root / "agents" / "personas" / f"{role}-{domain}.md").is_file():
        return openclaw_roles.agent_id_for(role, domain)
    return openclaw_roles.agent_id_for(role)


def build_prompt(run: Mapping[str, Any], step: Step, attempt: int, kit_root: Path, handoff_file: str) -> str:
    """Everything the child needs: it has no chat context, no subagent definitions and does not write the handoff."""
    cwd = str(run.get("cwd") or run.get("workspace"))
    domain = str(run.get("domain") or "")
    kit = str(kit_root)
    persona = kit_root / "agents" / "personas" / f"{step.role}-{domain}.md" if domain else None
    reads = [f"- role: {kit}/agents/roles/{step.role}.md"]
    if persona is not None and persona.is_file():
        reads.append(f"- persona: {persona}")
    reads += [f"- skill: {kit}/skills/{sid}/SKILL.md" for sid in step.skills]
    template = (step.prompt_template or "").replace("{{workspace}}", cwd).replace("{{domain}}", domain) \
        .replace("{{user_goal}}", str(run.get("goal") or "")).replace("{{handoff_file}}", handoff_file) \
        .replace("$AGENT_KIT", kit)
    verdict = "Verdict: <PASS | REQUIRES_CHANGES | FAIL>\n" if step.role in ("verifier", "code-reviewer", "security-auditor",
                                                                              "tester", "software-architect") else ""
    return (
        f"You are the {step.role} of an orchestrated team run ({run['id']}, step {step.id}, attempt {attempt}).\n"
        f"Working directory (stay inside it): {cwd}\n"
        f"Handoff file (read it; the orchestrator owns writes to it - do NOT edit it): {handoff_file}\n"
        "Read these first:\n" + "\n".join(reads) + "\n"
        "Rules: one writer per run; only the verifier signs a run off; never call the native AskUserQuestion; never sleep "
        "or poll in a loop (run long work in the background and read its output with short calls).\n"
        "Where the instructions below say to update the handoff file, put those fields in your return block instead.\n"
        "End your final reply with exactly this block:\n"
        "Status: success | partial | blocked\n"
        "Blocked: true | false\n"
        "Return_to_step: <step id, only when Blocked is true>\n"
        "Block_reason: <short reason, only when Blocked is true>\n"
        f"{verdict}\n"
        "--- step instructions ---\n"
        f"{template}\n")


# ----------------------------------------------------------------------------------------------------------------------
# the engine

class Engine:
    def __init__(self, store: RunStore | None = None, *, kit_root: Path | None = None, clock: Clock = time.time,
                 runner: Callable[..., Any] | None = None):
        self.store = store or RunStore()
        self.kit_root = kit_root or repo_root()
        self.clock = clock
        self.runner = runner

    # -- persistence ----------------------------------------------------------------------------------------------
    def _load(self, run_id: str) -> dict[str, Any]:
        try:
            return self.store.load(run_id)
        except StateError as e:
            raise EngineError(str(e), "unknown-run") from e

    def _save(self, run: dict[str, Any]) -> None:
        run["updated_at"] = self.clock()
        self.store.save(run)
        self.store.write_handoff(run["id"], self._render_handoff(run))

    # -- handoff --------------------------------------------------------------------------------------------------
    def _render_handoff(self, run: Mapping[str, Any]) -> str:
        front = {k: run["handoff"].get(k) for k in HANDOFF_KEYS if k in run["handoff"]}
        lines = ["# Handoff", "", f"**Run:** {run['id']}  |  **Workflow:** {run['workflow']}  |  **Status:** {run['status']}",
                 "", f"**Goal:** {run.get('goal', '')}", "", "| Step | Role | State | Attempt | Result |", "|---|---|---|---|---|"]
        for sid in run["order"]:
            st = run["steps"][sid]
            lines.append(f"| {sid} | {st['role']} | {st['state']} | {st['attempt']} | {st.get('result', '')} |")
        if run.get("failure"):
            lines += ["", f"**Failure:** {run['failure']}"]
        independence = run.get("independence") or {}
        if independence.get("notes"):
            lines += ["", "**Verifier independence:** " + "; ".join(independence["notes"])]
        header = yaml.safe_dump(front, sort_keys=False, default_flow_style=False)
        return f"---\n{header}---\n\n" + "\n".join(lines) + "\n"

    def _merge_handoff(self, run: dict[str, Any], step: Step, rep: reply_mod.Reply) -> None:
        h = run["handoff"]
        h["step"] = step.id
        refs, verdicts = h.setdefault("refs", {}), h.setdefault("verdicts", {})
        for key, slot in (("plan_ref", "plan"), ("architecture_design_ref", "architecture"), ("test_report", "test_report")):
            if rep.get(key):
                refs[slot] = rep.get(key)
        if rep.get("code_review_verdict"):
            verdicts["code_review"] = rep.get("code_review_verdict")
        if rep.get("security_audit"):
            verdicts["security"] = rep.get("security_audit")
        if rep.get("architect_approval"):
            verdicts["architect"] = rep.get("architect_approval")
        if rep.get("requires_tests"):
            h["requires_tests"] = rep.get("requires_tests")
        if rep.get("security_critical_feature"):
            h["security_critical"] = rep.get("security_critical_feature")
        if step.role == VERIFIER_ROLE and rep.verdict_word:
            verdicts["verification"] = rep.verdict_word

    # -- start / admission ----------------------------------------------------------------------------------------
    def start(self, wf: Workflow, goal: str, workspace: str, *, domain: str = "", run_id: str = "",
              models: Mapping[str, Any] | None = None, role_timeouts: Mapping[str, Any] | None = None,
              max_teams: int = limits.DEFAULT_MAX_TEAMS, base_ref: str = "HEAD", use_worktree: bool = True
              ) -> dict[str, Any]:
        """Register a run. It runs now when a team slot is free, else it is queued (FIFO)."""
        if not goal.strip():
            raise EngineError("a run needs a goal", "usage")
        now = self.clock()
        rid = run_id or new_run_id(wf.name, now)
        steps = {s.id: {"role": s.role, "state": "pending", "attempt": 0, "session": "", "agent": "", "result": "",
                        "reason": "", "started_at": 0, "finished_at": 0} for s in wf.steps}
        approval_step = wf.first_writer()
        run: dict[str, Any] = {
            "id": rid, "workflow": wf.name, "workflow_file": wf.path, "goal": goal.strip()[:2000],
            "workspace": str(workspace), "cwd": str(workspace), "domain": domain, "status": "queued",
            "created_at": now, "queued_at": now, "started_at": 0, "finished_at": 0, "updated_at": now,
            "max_teams": limits.clamp_max_teams(max_teams), "base_ref": base_ref, "use_worktree": bool(use_worktree),
            "worktree": None, "models": {k: str(v) for k, v in (models or {}).items() if v},
            "role_timeouts": {k: v for k, v in (role_timeouts or {}).items()},
            "workflow_steps": _wf_to_state(wf), "order": [s.id for s in wf.steps], "steps": steps, "returns": {},
            "approval": {"before": approval_step, "state": "pending" if approval_step else "not_needed"},
            "independence": {"mode": "unverified", "notes": list(wf.notes)}, "failure": "", "events": [],
            "handoff": {"workflow": wf.name, "step": "", "status": "pending", "blocked": False, "return_to_step": "",
                        "block_reason": "", "domain": domain, "requires_tests": "unknown", "security_critical": "unknown",
                        "refs": {"plan": "", "architecture": "", "test_report": "", "review": "", "security": ""},
                        "verdicts": {"architect": "pending", "code_review": "pending", "security": "pending",
                                     "verification": "pending"},
                        "next": wf.steps[0].id, "docs_updated": "", "git_integration": "none"},
        }
        problem = self._independence_problem(run)
        with self.store.global_lock():
            if self.store.exists(rid):
                raise EngineError(f"run {rid!r} already exists", "exists")
            runs = self.store.all()
            if problem:
                run["status"], run["failure"] = "failed", problem
                run["finished_at"] = now
                _event(run, "failed", "", problem, now)
            elif limits.admit(runs, run["max_teams"]) == "running":
                self._activate(run, now)
            else:
                _event(run, "queued", "", f"behind {len(limits.queue_order(runs))} queued run(s)", now)
            self.store.save(run)
            self.store.write_handoff(rid, self._render_handoff(run))
        return run

    def _independence_problem(self, run: dict[str, Any]) -> str:
        """Fail closed on a checker whose model family is unknown or equals a writer's; unverifiable when models are unknown."""
        steps = _steps_of(run)
        models = run["models"]
        checker_roles = {s.role for s in steps if s.role in CHECKER_ROLES}
        writer_roles = {s.role for s in steps if s.role in WRITER_ROLES or s.role in roles_policy.SCOPE_ROLES}
        notes = run["independence"]["notes"]
        if not checker_roles:
            return ""
        known = all(models.get(r) for r in checker_roles) and all(models.get(r) for r in writer_roles)
        if not models or not known:
            notes.append("model families unknown for some roles; independence rests on distinct agent ids "
                         "and, where the runtime reports one, a distinct session")
            return ""
        w_fams = {r: family_of(models[r]) for r in writer_roles}
        for c in sorted(checker_roles):
            cf = family_of(models[c])
            if cf is None:
                return f"checker {c} runs on a model of unknown family ({models[c]}); independence cannot be shown"
            for w, wf_ in w_fams.items():
                if wf_ is None or wf_ == cf:
                    return (f"checker {c} ({models[c]}, family {cf}) is not independent of writer {w} "
                            f"({models[w]}, family {wf_})")
        run["independence"]["mode"] = "agent-model"
        notes.append("verifier independence: distinct agent id and model family"
                     " (plus a distinct session when the runtime reports one)")
        return ""

    def _activate(self, run: dict[str, Any], now: float) -> None:
        """A run gets its team slot: worktree and branch first (a failure fails the run, not the process)."""
        run["status"] = "running"
        run["started_at"] = now
        if run.get("use_worktree"):
            try:
                rec = worktree_mod.create(run["id"], run["workspace"], run.get("base_ref") or "HEAD", self.runner)
            except Exception as e:  # noqa: BLE001 - lanes_cmd raises its own error type; any failure fails this run only
                run["status"], run["failure"], run["finished_at"] = "failed", f"could not create the team worktree: {e}", now
                _event(run, "failed", "", run["failure"], now)
                return
            if rec is None:
                run["independence"]["notes"].append("the workspace is not a git checkout: the team works in place")
            else:
                run["worktree"], run["cwd"] = rec, rec["path"]
        _event(run, "started", "", f"cwd {run['cwd']}", now)

    def _promote(self) -> list[str]:
        """Queued runs that now fit, oldest first. Returns their ids."""
        promoted: list[str] = []
        with self.store.global_lock():
            runs = self.store.all()
            max_teams = max((r.get("max_teams", limits.DEFAULT_MAX_TEAMS) for r in runs if r.get("status") == "queued"),
                            default=limits.DEFAULT_MAX_TEAMS)
            for rid in limits.next_to_promote(runs, max_teams):
                with self.store.locked(rid):
                    run = self._load(rid)
                    if run["status"] != "queued":
                        continue
                    self._activate(run, self.clock())
                    self._save(run)
                    promoted.append(rid)
        return promoted

    # -- the actions ----------------------------------------------------------------------------------------------
    def next(self, run_id: str) -> dict[str, Any]:
        """The actions to perform now. Idempotent: a step already handed out is not handed out again."""
        with self.store.locked(run_id):
            run = self._load(run_id)
            actions = self._actions(run)
            self._save(run)
        return {"run": run_id, "status": run["status"], "actions": actions}

    def _actions(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        status = run["status"]
        if status == "queued":
            ahead = [r for r in limits.queue_order(self.store.all()) if r != run["id"]]
            return [{"type": "notice", "run": run["id"], "kind": "queued",
                     "text": f"queued: {limits.clamp_max_teams(run['max_teams'])} team(s) are working; this one starts when "
                             f"a slot frees ({len(ahead)} run(s) ahead of it in the queue)"}]
        if status in TERMINAL:
            return [{"type": "failed" if status == "failed" else status if status == "cancelled" else "done",
                     "run": run["id"], "text": run.get("failure") or f"run {status}"}]
        if status == "awaiting_approval":
            return [self._ask_approval(run)]
        steps = _steps_of(run)
        for group in _groups(steps):
            members = [s for s in group if run["steps"][s.id]["state"] != "done"]
            if not members:
                continue
            first_writer = run["approval"]["before"]
            if run["approval"]["state"] == "pending" and any(s.id == first_writer for s in members):
                run["status"] = "awaiting_approval"
                _event(run, "awaiting_approval", first_writer, "", self.clock())
                return [self._ask_approval(run)]
            out: list[dict[str, Any]] = []
            for s in members:
                st = run["steps"][s.id]
                if st["state"] == "pending":
                    blocked_by = self._independence_gate(run, s)
                    if blocked_by:
                        self._fail(run, blocked_by)
                        return self._actions(run)
                    st["state"], st["attempt"], st["started_at"] = "running", st["attempt"] + 1, self.clock()
                    st["agent"] = agent_for(s.role, run.get("domain") or "", self.kit_root)
                    _event(run, "dispatch", s.id, f"attempt {st['attempt']}", self.clock())
                    out.append(self._dispatch(run, s))
                elif st["state"] == "running":
                    out.append({"type": "wait", "run": run["id"], "step": s.id, "text": "waiting for this step's report"})
            return out
        # every step done
        run["status"], run["finished_at"] = "completed", self.clock()
        run["handoff"]["status"], run["handoff"]["next"] = "success", "none"
        _event(run, "completed", "", "", self.clock())
        return [{"type": "done", "run": run["id"], "text": "every step is done"}]

    def _ask_approval(self, run: Mapping[str, Any]) -> dict[str, Any]:
        refs = run["handoff"].get("refs", {})
        return {"type": "ask", "kind": "approval", "run": run["id"], "step": run["approval"]["before"],
                "text": f"The plan is ready ({refs.get('plan') or 'see the handoff'}). Approve it to let "
                        f"{run['approval']['before']} start writing, or cancel the run.",
                "options": ["approve", "cancel"], "handoff": str(self.store.handoff_path(run["id"]))}

    def _dispatch(self, run: dict[str, Any], step: Step) -> dict[str, Any]:
        st = run["steps"][step.id]
        handoff = str(self.store.handoff_path(run["id"]))
        return {"type": "dispatch", "run": run["id"], "step": step.id, "role": step.role, "agent": st["agent"],
                "attempt": st["attempt"], "cwd": str(run.get("cwd") or run["workspace"]),
                "timeout_s": limits.role_timeout(step.role, run.get("role_timeouts")),
                "parallel": bool(step.group), "handoff_file": handoff,
                "prompt": build_prompt(run, step, st["attempt"], self.kit_root, handoff)}

    def _independence_gate(self, run: dict[str, Any], step: Step) -> str:
        """Refuse to hand the verifier out under a violation that can already be seen (the agent id)."""
        if step.role != VERIFIER_ROLE:
            return ""
        verifier = agent_for(step.role, run.get("domain") or "", self.kit_root)
        writers = {run["steps"][s.id]["agent"] for s in _steps_of(run)
                   if (s.role in WRITER_ROLES or s.role in roles_policy.SCOPE_ROLES) and run["steps"][s.id]["agent"]}
        if verifier in writers:
            return f"verifier agent {verifier} is also a writer's agent"
        return ""

    # -- report ---------------------------------------------------------------------------------------------------
    def report(self, run_id: str, step_id: str, *, text: str = "", session: str = "", agent: str = "",
               timeout: bool = False, error: str = "") -> dict[str, Any]:
        """A step finished (its reply), timed out or errored. Returns the next actions and any promoted runs."""
        with self.store.locked(run_id):
            run = self._load(run_id)
            if run["status"] in TERMINAL:
                return {"run": run_id, "status": run["status"], "actions": self._actions(run), "promoted": [],
                        "ignored": f"the run is {run['status']}"}
            if step_id not in run["steps"]:
                raise EngineError(f"unknown step {step_id!r}", "unknown-step")
            st = run["steps"][step_id]
            if st["state"] == "done" and run["status"] in ("running", "awaiting_approval"):
                return {"run": run_id, "status": run["status"], "actions": self._actions(run), "promoted": [],
                        "ignored": "already reported"}
            if run["status"] != "running":
                raise EngineError(f"run {run_id} is {run['status']}, not running", "not-running")
            if st["state"] != "running":
                raise EngineError(f"step {step_id} is {st['state']}, not running", "not-running")
            step = next(s for s in _steps_of(run) if s.id == step_id)
            if session:
                st["session"] = session[:120]
            if agent:
                st["agent"] = agent[:120]
            outcome = self._judge(run, step, text, timeout=timeout, error=error)
            self._apply(run, step, outcome)
            actions = self._actions(run)
            terminal = run["status"] in TERMINAL
            self._save(run)
        promoted = self._promote() if terminal else []
        return {"run": run_id, "status": run["status"], "actions": actions, "promoted": promoted}

    def _judge(self, run: dict[str, Any], step: Step, text: str, *, timeout: bool, error: str) -> dict[str, Any]:
        """Turn a reply into an outcome: ok, or blocked with the step to return to and why."""
        st = run["steps"][step.id]
        if timeout:
            limit = limits.role_timeout(step.role, run.get("role_timeouts"))
            return {"ok": False, "result": "timeout", "target": step.id, "reason": f"timed out after {limit} s",
                    "reply": reply_mod.Reply()}
        if error:
            return {"ok": False, "result": "error", "target": step.id, "reason": f"the dispatch failed: {error[:200]}",
                    "reply": reply_mod.Reply()}
        rep = reply_mod.parse(text)
        target = rep.return_to if rep.return_to in run["steps"] else (step.return_to or step.id)
        if step.role != VERIFIER_ROLE and rep.signoff:
            return {"ok": False, "result": "rejected", "target": step.id, "reply": rep,
                    "reason": f"only the verifier signs a run off; {step.role} replied "
                              f"{sorted(rep.words() & reply_mod.SIGNOFF_WORDS)[0]}"}
        if step.role == VERIFIER_ROLE:
            problem = self._verifier_problem(run, step, st, rep)
            if problem:
                return {"ok": False, "result": "rejected", "target": step.id, "reply": rep, "reason": problem}
        if rep.blocked or rep.blocking_verdict:
            reason = rep.reason or (f"verdict {rep.verdict_word}" if rep.verdict_word else "blocked")
            return {"ok": False, "result": "blocked", "target": target, "reply": rep, "reason": reason}
        return {"ok": True, "result": "ok", "reply": rep, "reason": ""}

    def _verifier_problem(self, run: dict[str, Any], step: Step, st: Mapping[str, Any], rep: reply_mod.Reply) -> str:
        expected = agent_for(step.role, run.get("domain") or "", self.kit_root)
        if st.get("agent") and st["agent"] != expected:
            return f"the verifier step ran on {st['agent']}, not on {expected}"
        sessions = {run["steps"][s.id]["session"] for s in _steps_of(run)
                    if s.id != step.id and run["steps"][s.id]["session"]
                    and (s.role in WRITER_ROLES or s.role in roles_policy.SCOPE_ROLES)}
        if st.get("session") and st["session"] in sessions:
            return "the verifier ran in a writer's session, so it is not independent"
        if not st.get("session"):
            run["independence"]["mode"] = run["independence"]["mode"] if run["independence"]["mode"] != "session" \
                else "agent-model"
            note = "the runtime reported no session id for the verifier; independence rests on agent id and model family"
            if note not in run["independence"]["notes"]:
                run["independence"]["notes"].append(note)
        elif run["independence"]["mode"] in ("unverified", "agent-model"):
            run["independence"]["mode"] = "session"
        if not rep.verdict_word and not rep.blocked:
            return "the verifier's reply carries no verdict"
        return ""

    def _apply(self, run: dict[str, Any], step: Step, outcome: dict[str, Any]) -> None:
        now = self.clock()
        st = run["steps"][step.id]
        st["finished_at"] = now
        st["result"] = outcome["result"]
        st["reason"] = outcome["reason"]
        self._merge_handoff(run, step, outcome["reply"])
        if outcome["ok"]:
            st["state"] = "done"
            _event(run, "step-done", step.id, "", now)
            group = [s for s in _steps_of(run) if s.group and s.group == step.group] if step.group else [step]
            self._join_group_if_ready(run, group)
            run["handoff"]["next"] = next((sid for sid in run["order"] if run["steps"][sid]["state"] != "done"), "none")
            return
        st["state"] = "blocked"
        _event(run, "step-blocked", step.id, f"{outcome['result']}: {outcome['reason']}", now)
        run["handoff"].update({"blocked": True, "return_to_step": outcome["target"], "block_reason": outcome["reason"],
                               "status": "blocked"})
        outcome_for_join = {"target": outcome["target"], "reason": outcome["reason"]}
        st["return"] = outcome_for_join
        group = [s for s in _steps_of(run) if s.group and s.group == step.group] if step.group else [step]
        self._join_group_if_ready(run, group)

    def _join_group_if_ready(self, run: dict[str, Any], group: list[Step]) -> None:
        """When no member of the group is still running, return on the first blocked member (once) or move on."""
        if any(run["steps"][s.id]["state"] == "running" for s in group):
            return
        blocked = [s for s in group if run["steps"][s.id]["state"] == "blocked"]
        if not blocked:
            return
        first = blocked[0]
        ret = run["steps"][first.id].get("return") or {"target": first.id, "reason": "blocked"}
        self._return_to(run, ret["target"], ret["reason"], blocked_step=first.id)

    def _return_to(self, run: dict[str, Any], target: str, reason: str, *, blocked_step: str) -> None:
        run["returns"][target] = run["returns"].get(target, 0) + 1
        if run["returns"][target] > MAX_RETURNS:
            self._fail(run, f"step {target} was returned to {MAX_RETURNS} times already; stopping at the retry cap "
                            f"(last reason: {reason})")
            return
        order = run["order"]
        gate = run["approval"]["before"]
        if gate and run["approval"]["state"] == "granted" and order.index(target) < order.index(gate):
            run["approval"]["state"] = "pending"          # the plan is being redone: the operator approves the new one
            _event(run, "approval-reset", target, "", self.clock())
        for sid in order[order.index(target):]:
            st = run["steps"][sid]
            if st["state"] in ("done", "blocked", "running"):
                st["state"] = "pending"
                st.pop("return", None)
        run["handoff"].update({"blocked": False, "return_to_step": "", "block_reason": "", "status": "partial"})
        run["handoff"]["next"] = target
        _event(run, "returned", target, f"from {blocked_step}: {reason} (return {run['returns'][target]} of {MAX_RETURNS})",
               self.clock())

    def _fail(self, run: dict[str, Any], reason: str) -> None:
        run["status"], run["failure"], run["finished_at"] = "failed", reason, self.clock()
        run["handoff"]["status"] = "blocked"
        run["handoff"]["block_reason"] = reason
        _event(run, "failed", "", reason, self.clock())

    # -- operator verbs -------------------------------------------------------------------------------------------
    def approve(self, run_id: str) -> dict[str, Any]:
        with self.store.locked(run_id):
            run = self._load(run_id)
            if run["approval"]["state"] != "pending":
                raise EngineError("this run has no plan waiting for approval", "no-approval")
            if run["status"] not in ("awaiting_approval", "running"):
                raise EngineError(f"run {run_id} is {run['status']}", "not-running")
            run["approval"]["state"] = "granted"
            _event(run, "approved", run["approval"]["before"], "", self.clock())
            if run["status"] == "awaiting_approval":
                run["status"] = "running"
            actions = self._actions(run)
            self._save(run)
        return {"run": run_id, "status": run["status"], "actions": actions}

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.store.locked(run_id):
            run = self._load(run_id)
            if run["status"] in TERMINAL:
                return {"run": run_id, "status": run["status"], "actions": [], "promoted": []}
            run["status"], run["finished_at"] = "cancelled", self.clock()
            for st in run["steps"].values():
                if st["state"] == "running":
                    st["state"], st["result"] = "pending", "cancelled"
            _event(run, "cancelled", "", "", self.clock())
            self._save(run)
        return {"run": run_id, "status": "cancelled", "actions": [], "promoted": self._promote()}

    def resume(self, run_id: str) -> dict[str, Any]:
        """After a gateway restart or a killed process: the steps handed out and never reported are handed out again.
        This is not a retry (the attempt number goes back to what it was), so it never counts against a cap."""
        with self.store.locked(run_id):
            run = self._load(run_id)
            if run["status"] != "running":
                return {"run": run_id, "status": run["status"], "actions": self._actions(run), "resumed": []}
            resumed = []
            for sid, st in run["steps"].items():
                if st["state"] == "running":
                    st["state"] = "pending"
                    st["attempt"] = max(0, st["attempt"] - 1)
                    resumed.append(sid)
            if resumed:
                _event(run, "resumed", ",".join(resumed), "", self.clock())
            actions = self._actions(run)
            self._save(run)
        return {"run": run_id, "status": run["status"], "actions": actions, "resumed": resumed}

    def resume_all(self) -> list[dict[str, Any]]:
        out = [self.resume(rid) for rid in self.store.ids() if self._status_of(rid) == "running"]
        for rid in self._promote():
            out.append(self.next(rid))
        return out

    def _status_of(self, rid: str) -> str:
        try:
            return str(self.store.load(rid).get("status"))
        except StateError:
            return ""

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id:
            run = self._load(run_id)
            return self._summary(run, detail=True)
        runs = self.store.all()
        return {"max_teams": max((r.get("max_teams", limits.DEFAULT_MAX_TEAMS) for r in runs), default=limits.DEFAULT_MAX_TEAMS),
                "active": limits.active_count(runs), "queued": limits.queue_order(runs),
                "runs": [self._summary(r) for r in sorted(runs, key=lambda r: r.get("created_at", 0))]}

    def _summary(self, run: Mapping[str, Any], detail: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": run["id"], "workflow": run["workflow"], "status": run["status"], "goal": run.get("goal", "")[:120],
            "step": next((sid for sid in run["order"] if run["steps"][sid]["state"] != "done"), ""),
            "done": sum(1 for s in run["steps"].values() if s["state"] == "done"), "total": len(run["order"]),
            "worktree": (run.get("worktree") or {}).get("path", ""), "branch": (run.get("worktree") or {}).get("branch", ""),
            "failure": run.get("failure", "")}
        if detail:
            out.update({"steps": {sid: {k: run["steps"][sid].get(k) for k in ("role", "state", "attempt", "result", "reason",
                                                                              "agent", "session")}
                                  for sid in run["order"]},
                        "returns": run.get("returns", {}), "approval": run["approval"], "independence": run["independence"],
                        "handoff": str(self.store.handoff_path(run["id"])), "events": run.get("events", [])[-20:]})
        return out
