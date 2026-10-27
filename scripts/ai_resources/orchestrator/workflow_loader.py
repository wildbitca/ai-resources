"""Load a kit workflow (`workflows/*.workflow.yaml`, see WORKFLOW_CONTRACT.md) into the shape the engine runs.

Only what the engine needs is read: ordered steps, the role of each, its skills, parallel groups, the step a concern
returns to and the prompt template. `backend` is `route` unless a step overrides it (`claude-code`, `openclaw-agent`,
`agy`); the engine dispatches every step to the role's `kit-role-*` agent, and teams share the checkout, kept apart by
their write-scope claims (`scope_guard`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import repo_root, roles_policy

READ_ONLY_ROLES = roles_policy.REPORT_ROLES
# The roles that start writing code or config: the plan must be approved by the operator before the first of them.
WRITER_ROLES = frozenset({"implementer", "package-upgrade", "crashlytics-fixer", "sentry-fixer", "terraform-maintainer",
                          "crossplane-upjet-maintainer"})
CHECKER_ROLES = roles_policy.CHECKER_ROLES
VERIFIER_ROLE = "verifier"


class WorkflowError(ValueError):
    """The workflow cannot be loaded or is inconsistent."""


BACKENDS = frozenset({"route", "claude-code", "openclaw-agent", "agy"})


@dataclass(frozen=True)
class Step:
    id: str
    role: str
    skills: tuple[str, ...] = ()
    group: str = ""
    prompt_template: str = ""
    entry_criteria: str = ""
    exit_criteria: str = ""
    handoff_to: str = ""
    return_to: str = ""                 # `on_concern_return_to`
    backend: str = "route"


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    domain: str
    steps: tuple[Step, ...]
    path: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise WorkflowError(f"unknown step {step_id!r}")

    def index(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        raise WorkflowError(f"unknown step {step_id!r}")

    def groups(self) -> list[list[Step]]:
        """Consecutive steps that share a `parallel_group` form one group; every other step is its own."""
        out: list[list[Step]] = []
        for s in self.steps:
            if s.group and out and out[-1][-1].group == s.group:
                out[-1].append(s)
            else:
                out.append([s])
        return out

    def first_writer(self) -> str:
        """Id of the step before which the operator approves the plan, '' when every step is read-only.

        The first step of a code-writing role (WRITER_ROLES) when there is one, so a research step by a general-purpose
        role ahead of the plan does not pause the run before a plan exists. A workflow with no such role is gated at its
        first step by any role outside READ_ONLY_ROLES (generalPurpose, doc-writer, tester, ...): it must never run
        end to end with no checkpoint."""
        for s in self.steps:
            if s.role in WRITER_ROLES:
                return s.id
        for s in self.steps:
            if s.role not in READ_ONLY_ROLES:
                return s.id
        return ""


def workflows_dir(kit_root: Path | None = None) -> Path:
    return (kit_root or repo_root()) / "workflows"


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkflowError(f"{path.name}: {e}") from e
    if not isinstance(doc, dict):
        raise WorkflowError(f"{path.name}: not a mapping")
    return doc


def _build(doc: dict[str, Any], path: Path) -> Workflow:
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError(f"{path.name}: no steps")
    steps: list[Step] = []
    notes: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("subagent_type"):
            raise WorkflowError(f"{path.name}: step {i} needs an id and a subagent_type")
        sid, role = str(raw["id"]), str(raw["subagent_type"])
        if sid in seen:
            raise WorkflowError(f"{path.name}: duplicate step id {sid!r}")
        seen.add(sid)
        backend = str(raw.get("backend") or "route")
        if backend not in BACKENDS:
            raise WorkflowError(f"{path.name}: step {sid!r} has unknown backend {backend!r} (allowed: {', '.join(sorted(BACKENDS))})")
        hints = raw.get("execution_hints") if isinstance(raw.get("execution_hints"), dict) else {}
        steps.append(Step(
            id=sid, role=role, skills=tuple(str(s) for s in (raw.get("skills") or [])),
            group=str(hints.get("parallel_group") or ""), prompt_template=str(raw.get("prompt_template") or ""),
            entry_criteria=str(raw.get("entry_criteria") or ""), exit_criteria=str(raw.get("exit_criteria") or ""),
            handoff_to=str(raw.get("handoff_to") or ""), return_to=str(raw.get("on_concern_return_to") or ""),
            backend=backend))
    ids = {s.id for s in steps}
    for s in steps:
        for ref in (s.handoff_to, s.return_to):
            if ref and ref not in ids and ref != "none":
                raise WorkflowError(f"{path.name}: step {s.id!r} points at unknown step {ref!r}")
    # A parallel group runs concurrently only when every member is read-only; else it degrades to sequential.
    degraded: list[Step] = []
    out: list[Step] = []
    by_group: dict[str, list[Step]] = {}
    for s in steps:
        if s.group:
            by_group.setdefault(s.group, []).append(s)
    bad = {g for g, members in by_group.items()
           if any(m.role not in READ_ONLY_ROLES for m in members)
           or [steps.index(m) for m in members] != list(range(steps.index(members[0]), steps.index(members[0]) + len(members)))}
    for s in steps:
        if s.group in bad:
            degraded.append(s)
            out.append(Step(**{**s.__dict__, "group": ""}))
        else:
            out.append(s)
    for g in sorted(bad):
        notes.append(f"parallel group {g!r} runs sequentially: it holds a role that writes, or its steps are not adjacent")
    return Workflow(name=str(doc.get("name") or path.stem), description=str(doc.get("description") or ""),
                    domain=str(doc.get("domain") or ""), steps=tuple(out), path=str(path), notes=tuple(notes))


def load(name_or_path: str, kit_root: Path | None = None) -> Workflow:
    """A workflow by file path, by file stem (`feature-cross-provider`) or by its `name:` (`feature-implementation`)."""
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
        return _build(_read(candidate), candidate)
    base = workflows_dir(kit_root)
    for path in (base / f"{name_or_path}.workflow.yaml", base / f"_{name_or_path}-template.workflow.yaml"):
        if path.is_file():
            return _build(_read(path), path)
    for path in sorted(base.glob("*.workflow.yaml")):
        try:
            if _read(path).get("name") == name_or_path:
                return _build(_read(path), path)
        except WorkflowError:
            continue
    raise WorkflowError(f"unknown workflow {name_or_path!r}")


def list_workflows(kit_root: Path | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for path in sorted(workflows_dir(kit_root).glob("*.workflow.yaml")):
        try:
            doc = _read(path)
        except WorkflowError:
            continue
        out.append({"name": str(doc.get("name") or path.stem), "file": path.name,
                    "description": " ".join(str(doc.get("description") or "").split())})
    return out


def describe(wf: Workflow) -> dict[str, Any]:
    """The JSON shape of `ai-resources workflows show <name> --json`."""
    return {
        "name": wf.name, "description": wf.description, "domain": wf.domain, "file": wf.path,
        "approval_before": wf.first_writer(), "notes": list(wf.notes),
        "steps": [{"id": s.id, "role": s.role, "skills": list(s.skills), "group": s.group, "handoff_to": s.handoff_to,
                   "return_to": s.return_to, "backend": s.backend, "read_only": s.role in READ_ONLY_ROLES,
                   "prompt_template": s.prompt_template} for s in wf.steps],
    }
