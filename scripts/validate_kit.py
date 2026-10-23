#!/usr/bin/env python3
"""Validate the kit's content: skills, workflows, agents, hooks and generated artifacts.

Runs without network or API access, so CI can gate every pull request on it. Behavioural
testing of the prompts themselves is `claude plugin eval` (see evals/), not this script.

Usage:
    python3 scripts/validate_kit.py [--fix-index]

Exit code 0 when everything passes, 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parents[1]
# The package is not installed when CI runs this script, so put `scripts/` on the path for the
# policy module (pure data, no other imports).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_resources import haiku_budget, roles_policy, skill_scope  # noqa: E402
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_LIMIT = 1024  # Claude Code truncates description + when_to_use at 1536 chars
VERIFIER_DOC_SKILL = "knowledge-audit"

# Per-step backend (ADR 0005, ADR 0007). `claude-code` is the default and today's behaviour: a kit role
# subagent. `openclaw-agent` hands the role to another OpenClaw agent through `sessions_spawn`. `lane`
# is resolved at dispatch from the setup lanes table and runs under the kit's lane commands.
BACKENDS = {"claude-code", "openclaw-agent", "lane"}
DEFAULT_BACKEND = "claude-code"
# A leading alphanumeric keeps a flag-like value out; the cap bounds the length.
AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# One writer per run: the implementer and every other write-capable role stay on claude-code.
# Read-only roles may run on another agent; tester and doc-writer run commands or write files,
# so they pass with a warning (the post-step check detects a violation, it cannot prevent one)
# and are never parallel members.
OPENCLAW_READ_ONLY_ROLES = set(roles_policy.EXPLICIT_READ_ONLY_ROLES)
OPENCLAW_WARNED_ROLES = set(roles_policy.EXPLICIT_WARNED_ROLES)
OPENCLAW_ALLOWED_ROLES = OPENCLAW_READ_ONLY_ROLES | OPENCLAW_WARNED_ROLES

failures: list[str] = []
warnings: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"[{check}] {message}")


def warn(check: str, message: str) -> None:
    warnings.append(f"[{check}] {message}")


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError as exc:
        fail("frontmatter", f"{path.relative_to(ROOT)}: invalid YAML ({exc.__class__.__name__})")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        # A scalar or list document parses fine but has no fields; without this the callers
        # would raise AttributeError and the run would end in a traceback, not a finding.
        fail("frontmatter", f"{path.relative_to(ROOT)}: frontmatter is {type(data).__name__}, expected a mapping")
        return {}
    return data


def check_skills() -> set[str]:
    """Every skill dir has a valid SKILL.md whose name matches the directory."""
    ids: set[str] = set()
    skills_root = ROOT / "skills"
    if not skills_root.is_dir():
        fail("skills", "skills/ does not exist")
        return ids
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        rel = skill_dir.relative_to(ROOT)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            fail("skills", f"{rel}: no SKILL.md")
            continue
        ids.add(skill_dir.name)
        meta = frontmatter(skill_md)
        name = str(meta.get("name", ""))
        description = str(meta.get("description", ""))
        if not SKILL_NAME_RE.match(skill_dir.name):
            fail("skills", f"{rel}: directory name is not kebab-case")
        if name != skill_dir.name:
            fail("skills", f"{rel}: frontmatter name '{name}' != directory name")
        if not description.strip():
            fail("skills", f"{rel}: no description — the agent cannot know when to load it")
        elif len(description) > DESCRIPTION_LIMIT:
            fail("skills", f"{rel}: description is {len(description)} chars (limit {DESCRIPTION_LIMIT})")
    return ids


def check_step_backend(step: dict, where: str) -> None:
    """`backend` and `agentId` are optional; agent existence is a dispatch-time check, not this one."""
    backend = step.get("backend", DEFAULT_BACKEND)
    has_agent = "agentId" in step
    if not isinstance(backend, str) or backend not in BACKENDS:
        fail("workflows", f"{where}: unknown backend {backend!r} (allowed: {', '.join(sorted(BACKENDS))})")
        return
    if backend != "openclaw-agent":
        if has_agent:
            fail("workflows", f"{where}: agentId is only valid with backend 'openclaw-agent'")
        if backend == "lane" and not roles_policy.lane_eligible(step.get("subagent_type")):
            fail("workflows", f"{where}: role '{step.get('subagent_type')}' is not eligible on 'lane' "
                              f"(no write contract declared; known: "
                              f"{', '.join(sorted(roles_policy.LANE_ELIGIBILITY))})")
        return
    agent_id = step.get("agentId")
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        fail("workflows", f"{where}: backend 'openclaw-agent' needs an agentId matching "
                          f"{AGENT_ID_RE.pattern} (got {agent_id!r})")
    role = step.get("subagent_type")
    if role in OPENCLAW_WARNED_ROLES:
        warn("workflows", f"{where}: role '{role}' on 'openclaw-agent' runs commands or writes files; "
                          f"the post-step check detects a violation but cannot prevent one")
    elif role not in OPENCLAW_READ_ONLY_ROLES:
        fail("workflows", f"{where}: role '{role}' cannot run on 'openclaw-agent' "
                          f"(one writer per run; allowed: {', '.join(sorted(OPENCLAW_ALLOWED_ROLES))})")


def _as_id_list(value) -> list | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def check_lane_workflow(rel, doc: dict, steps: list, groups: dict) -> None:
    """ADR-0007: `lane` steps, scope writers, the integrating writer and the verifier chain."""
    def f(where: str, message: str) -> None:
        fail("workflows", f"{where}: {message}")

    integrating_writer = roles_policy.DEFAULT_INTEGRATING_WRITER
    if "integrating_writer" in doc:
        value = doc["integrating_writer"]
        if not isinstance(value, str) or roles_policy.INTEGRATING not in roles_policy.contracts_for(value):
            able = sorted(r for r, c in roles_policy.LANE_ELIGIBILITY.items() if roles_policy.INTEGRATING in c)
            f(str(rel), f"integrating_writer must be one role that may integrate, one per workflow "
                        f"(got {value!r}; allowed: {', '.join(able)})")
        else:
            integrating_writer = value

    index = {s.get("id"): i for i, s in enumerate(steps)}
    by_id = {s.get("id"): s for s in steps}

    def is_lane(step: dict) -> bool:
        return step.get("backend") == "lane"

    def has_scope(step: dict) -> bool:
        return "write_scope" in step

    def is_integrating(step: dict) -> bool:
        return (is_lane(step) and step.get("subagent_type") == integrating_writer and not has_scope(step))

    scope_steps: list[dict] = []
    for step in steps:
        sid = step.get("id", "?")
        where = f"{rel}:{sid}"
        role = step.get("subagent_type")
        hints = step.get("execution_hints") or {}
        if "write_scope" in step:
            if not is_lane(step):
                f(where, "write_scope is only valid with backend 'lane'")
            elif role not in roles_policy.SCOPE_ROLES:
                f(where, f"write_scope is not valid for role '{role}' "
                         f"(scope writers: {', '.join(sorted(roles_policy.SCOPE_ROLES))})")
            else:
                scope = step["write_scope"]
                if not isinstance(scope, list) or not scope:
                    f(where, "write_scope must be a non-empty list of repository-relative paths")
                else:
                    ok = True
                    seen: set = set()
                    for path in scope:
                        problems = roles_policy.scope_problems(path)
                        if problems:
                            f(where, f"write_scope entry {path!r} {'; '.join(problems)}")
                            ok = False
                        elif path in seen:
                            f(where, f"write_scope entry {path!r} is listed twice")
                            ok = False
                        seen.add(path)
                    if ok:
                        scope_steps.append(step)
        if "integrates" in step:
            targets = _as_id_list(step["integrates"])
            if not is_lane(step):
                f(where, "integrates is only valid with backend 'lane'")
            elif role != integrating_writer:
                f(where, f"integrates is only valid on the integrating writer step "
                         f"(integrating_writer is '{integrating_writer}', this step is '{role}')")
            elif not targets:
                f(where, "integrates must name one or more scope step ids")
            else:
                for target in targets:
                    ref = by_id.get(target)
                    if ref is None:
                        f(where, f"integrates names '{target}', which is not a step in this workflow")
                    elif index[target] >= index[sid]:
                        f(where, f"integrates names '{target}', which does not run before this step")
                    elif not (is_lane(ref) and has_scope(ref)):
                        f(where, f"integrates names '{target}', which is not a lane step with a write_scope")
        if "verifies" in step:
            targets = _as_id_list(step["verifies"])
            if role != "verifier":
                f(where, "verifies is only valid on a verifier step")
            elif not targets:
                f(where, "verifies must name one or more step ids")
            else:
                for target in targets:
                    if target not in index:
                        f(where, f"verifies names '{target}', which is not a step in this workflow")
                    elif index[target] >= index[sid]:
                        f(where, f"verifies names '{target}', which does not run before this step")
        if "lane_member" in hints:
            group = hints.get("parallel_group")
            if hints["lane_member"] != "second":
                f(where, f"lane_member must be 'second' (got {hints['lane_member']!r})")
            elif not is_lane(step):
                f(where, "lane_member is only valid on a 'lane' step")
            elif role not in roles_policy.REPORT_ROLES:
                f(where, f"lane_member is only valid for report-contract roles; '{role}' writes files")
            elif not group:
                f(where, "lane_member needs execution_hints.parallel_group")
            elif not any(m is not step and m.get("subagent_type") == role for m in groups.get(group, ())):
                f(where, f"lane_member 'second' needs a primary member of role '{role}' in group '{group}'")
        if is_lane(step) and "Update {{handoff_file}}" in str(step.get("prompt_template", "")):
            f(where, "lane children never write the handoff: the orchestrator joins the step-scoped report")
        if is_integrating(step) and hints.get("parallel_group"):
            f(f"{rel}:{hints['parallel_group']}:{sid}",
              f"the integrating writer step '{sid}' is never a parallel member")

    for group, members in groups.items():
        if not any(is_lane(m) for m in members):
            continue
        where = f"{rel}:{group}"
        for member in members:
            sid = member.get("id")
            role = member.get("subagent_type")
            if (not is_lane(member) and member.get("backend", DEFAULT_BACKEND) == "claude-code"
                    and role not in roles_policy.REPORT_ROLES):
                f(f"{where}:{sid}", f"a claude-code writer step ('{role}') may not share a parallel group "
                                    f"with a 'lane' step")
        lane_members = [m for m in members if is_lane(m)]
        for member in lane_members:
            if member.get("subagent_type") in roles_policy.CHECKER_ROLES and len(lane_members) > 1:
                f(f"{where}:{member.get('id')}",
                  f"checker lane step ('{member.get('subagent_type')}') may not share a parallel group with "
                  f"another 'lane' step: the shared report directory cannot attribute a write before verify; "
                  f"a checker may share a group with claude-code or non-lane steps")
        scoped = [m for m in members if is_lane(m) and has_scope(m) and isinstance(m.get("write_scope"), list)]
        for i, left in enumerate(scoped):
            for right in scoped[i + 1:]:
                for a in left["write_scope"]:
                    for b in right["write_scope"]:
                        if (isinstance(a, str) and isinstance(b, str) and not roles_policy.scope_problems(a)
                                and not roles_policy.scope_problems(b) and roles_policy.scopes_overlap(a, b)):
                            f(where, f"write scopes of '{left.get('id')}' ({a}) and '{right.get('id')}' ({b}) "
                                     f"overlap; parallel scope writers need disjoint scopes")

    def verifiers_of(target: str, after: int) -> list[dict]:
        out = []
        for s in steps:
            names = _as_id_list(s.get("verifies")) or []
            if s.get("subagent_type") == "verifier" and target in names and index[s.get("id")] > after:
                out.append(s)
        return out

    for step in scope_steps:
        sid = step.get("id")
        where = f"{rel}:{sid}"
        vs = verifiers_of(sid, index[sid])
        if not vs:
            f(where, "a write_scope step needs a following verifier step that names it in 'verifies'")
            continue
        first_verifier = min(index[v["id"]] for v in vs)
        integrators = [s for s in steps if is_lane(s) and sid in (_as_id_list(s.get("integrates")) or [])
                       and s.get("subagent_type") == integrating_writer]
        if not integrators:
            f(where, f"a write_scope step needs an integrating step ('{integrating_writer}', lane) whose "
                     f"'integrates' names it")
            continue
        for integ in integrators:
            iid = integ.get("id")
            if index[iid] <= first_verifier:
                f(f"{rel}:{iid}", f"the integrating step must run after the verifier of '{sid}'")
            if not verifiers_of(iid, index[iid]):
                f(f"{rel}:{iid}", "an integrating step needs a following verifier step that names it in 'verifies'")


def check_skill_scope(skill_ids: set[str], role_names: set[str]) -> None:
    """The lean native skill list must not hide a skill a workflow step needs.

    A skill named by a workflow step stays reachable without the native listing: the step loads
    `$AGENT_KIT/skills/<id>/SKILL.md` by path (check_workflows already fails an unknown id), or the role's
    subagent preloads it (ROLE_SKILLS, which must be a subset of what EVERY step of the role names).
    Every generated workflow-* skill must carry `disable-model-invocation: true`, and the native include
    list must name real skills.
    """
    for sid in skill_scope.INCLUDE_IDS:
        if sid not in skill_ids:
            fail("skill-scope", f"skill_scope.INCLUDE_IDS names unknown skill '{sid}'")
    for sid in sorted(skill_ids):
        if sid.startswith("workflow-") and not frontmatter(ROOT / "skills" / sid / "SKILL.md").get("disable-model-invocation"):
            fail("skill-scope", f"skills/{sid}: workflow skills must set disable-model-invocation: true (run `ai-resources generate`)")
    for sid in skill_scope.LOADABLE_BY_PATH:
        if sid not in skill_ids:
            fail("skill-scope", f"skill_scope.LOADABLE_BY_PATH names unknown skill '{sid}'")
    linked = skill_scope.include_list(skill_ids, env={}) or set()
    loadable = set(skill_scope.LOADABLE_BY_PATH)
    role_steps: dict[str, list[set[str]]] = {}
    for wf_path in sorted((ROOT / "workflows").glob("*.workflow.yaml")):
        try:
            doc = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for step in doc.get("steps") or []:
            role, skills = step.get("subagent_type"), set(step.get("skills") or [])
            role_steps.setdefault(role, []).append(skills)
            for skill in sorted(skills):
                if skill in skill_ids and skill not in linked and skill not in loadable \
                        and skill not in skill_scope.ROLE_SKILLS.get(role, ()):
                    fail("skill-scope", f"{wf_path.name} step '{step.get('id')}' names '{skill}', which is not linked "
                                        f"(INCLUDE_*), not preloaded for role '{role}' (ROLE_SKILLS) and not marked "
                                        f"loadable-by-path (skill_scope.LOADABLE_BY_PATH)")

    for role, skills in skill_scope.ROLE_SKILLS.items():
        for skill in skills:
            if skill not in linked:
                fail("skill-scope", f"ROLE_SKILLS[{role}] preloads '{skill}', which is not linked (Claude Code skips it)")
            elif skill in skill_ids and skill not in skill_scope.INCLUDE_IDS and (ROOT / "skills" / skill / "SKILL.md").is_file() \
                    and not frontmatter(ROOT / "skills" / skill / "SKILL.md").get("disable-model-invocation"):
                fail("skill-scope", f"skills/{skill}: a preloaded skill outside INCLUDE_IDS must set disable-model-invocation: true")
        if role not in role_names:
            fail("skill-scope", f"ROLE_SKILLS names unknown role '{role}'")
        for skill in skills:
            if skill not in skill_ids:
                fail("skill-scope", f"ROLE_SKILLS[{role}] names unknown skill '{skill}'")
            elif not all(skill in step_skills for step_skills in role_steps.get(role, [set()])):
                fail("skill-scope", f"ROLE_SKILLS[{role}] preloads '{skill}', which not every '{role}' step names")

    # ADR-0015 S9: every cockpit is scoped by the one rule, and the skills an OpenClaw kit-role agent may use are linked.
    for cockpit in ("claude", "openclaw", "codex", "cursor"):
        if (skill_scope.scope_for(cockpit, skill_ids, env={}) or set()) != linked:
            fail("skill-scope", f"skill_scope.scope_for({cockpit!r}) differs from the Claude Code include list")
        if skill_scope.scope_for(cockpit, skill_ids, env={skill_scope.ENV_VAR: "all"}) is not None:
            fail("skill-scope", f"skill_scope.scope_for({cockpit!r}) must be unscoped when {skill_scope.ENV_VAR}=all")
    for skill in ("kit-orchestration",):
        if skill not in linked:
            fail("skill-scope", f"OpenClaw kit-role agents always allow '{skill}', which is not linked")


def check_haiku_budget() -> None:
    """A role that any shipped profile runs on Haiku must start under the 100k-token Haiku price tier (ADR-0014 D1)."""
    for role, tokens in haiku_budget.over_budget(ROOT):
        fail("haiku-budget", f"role '{role}' runs on Haiku and its static prompt is estimated at ~{tokens:,} tokens "
                             f"(4 chars/token, x{haiku_budget.SAFETY_MARGIN} margin, {haiku_budget.FIXED_PREFIX_TOKENS:,} fixed prefix), "
                             f"above the {haiku_budget.HAIKU_PROMPT_THRESHOLD_TOKENS:,}-token price tier: "
                             f"shrink its role file, preloaded skills or the native skill listing")


EFFORT_COMPLETE_PROFILES = ("balanced", "performance")


def check_role_effort(role_names: set[str]) -> None:
    """Every kit role has a `role_effort` entry with an allowed level in the profiles that declare the full table
    (ADR-0015 S3). The key stays `role_effort`; a profile may never carry an `effort` key."""
    import yaml
    allowed = {"low", "medium", "high"}
    for name in EFFORT_COMPLETE_PROFILES:
        path = ROOT / "profiles" / f"{name}.yaml"
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            fail("role-effort", f"{path.name}: {e}")
            continue
        table = doc.get("role_effort") or {}
        if "effort" in doc:
            fail("role-effort", f"{path.name}: the key is `role_effort`, never `effort`")
        for role in sorted(role_names):
            if table.get(role) not in allowed:
                fail("role-effort", f"{path.name}: role '{role}' needs a role_effort of {sorted(allowed)}, "
                                    f"got {table.get(role)!r}")
        for role in table:
            if role not in role_names:
                fail("role-effort", f"{path.name}: role_effort names unknown role '{role}'")


def check_workflows(skill_ids: set[str], role_names: set[str]) -> None:
    """Steps reference real roles and skills; routing targets resolve; invariants hold."""
    for wf_path in sorted((ROOT / "workflows").glob("*.workflow.yaml")):
        rel = wf_path.relative_to(ROOT)
        try:
            doc = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            fail("workflows", f"{rel}: invalid YAML ({exc})")
            continue
        for key in ("name", "description", "trigger", "steps"):
            if not doc.get(key):
                fail("workflows", f"{rel}: missing top-level '{key}'")
        steps = doc.get("steps") or []
        ids = {s.get("id") for s in steps}
        groups: dict[str, list[dict]] = {}
        for step in steps:
            sid = step.get("id", "?")
            where = f"{rel}:{sid}"
            if step.get("subagent_type") not in role_names:
                fail("workflows", f"{where}: unknown subagent_type '{step.get('subagent_type')}'")
            for skill in step.get("skills") or []:
                if skill not in skill_ids:
                    fail("workflows", f"{where}: unknown skill '{skill}'")
            for field in ("handoff_to", "on_concern_return_to"):
                target = step.get(field)
                if target not in (None, *ids):
                    fail("workflows", f"{where}: {field} '{target}' is not a step in this workflow")
            # The verifier checks acceptance criteria against evidence; documenting is a separate step.
            if step.get("subagent_type") == "verifier":
                if VERIFIER_DOC_SKILL in (step.get("skills") or []):
                    fail("workflows", f"{where}: verifier must not own '{VERIFIER_DOC_SKILL}'")
                if "SDD closure" in str(step.get("prompt_template", "")):
                    fail("workflows", f"{where}: verifier must not run SDD closure — use a document step")
            check_step_backend(step, where)
            group = (step.get("execution_hints") or {}).get("parallel_group")
            if group:
                groups.setdefault(group, []).append(step)
        check_lane_workflow(rel, doc, steps, groups)
        for group, members in groups.items():
            where = f"{rel}:{group}"
            if len({m.get("entry_criteria") for m in members}) > 1:
                fail("workflows", f"{where}: steps in a parallel group need the same entry criteria")
            for member in members:
                if (member.get("backend") == "openclaw-agent"
                        and member.get("subagent_type") not in OPENCLAW_READ_ONLY_ROLES):
                    fail("workflows", f"{where}:{member.get('id')}: a parallel member on 'openclaw-agent' "
                                      f"must be one of {', '.join(sorted(OPENCLAW_READ_ONLY_ROLES))}; "
                                      f"'{member.get('subagent_type')}' is a gate or writes files")
                if "Update {{handoff_file}}" in str(member.get("prompt_template", "")):
                    fail("workflows", f"{where}:{member.get('id')}: parallel steps must not write the handoff")


def check_roles() -> set[str]:
    names: set[str] = set()
    for role_path in sorted((ROOT / "agents" / "roles").glob("*.md")):
        meta = frontmatter(role_path)
        name = str(meta.get("name", ""))
        if name != role_path.stem:
            fail("roles", f"{role_path.relative_to(ROOT)}: frontmatter name '{name}' != file name")
        if not str(meta.get("description", "")).strip():
            fail("roles", f"{role_path.relative_to(ROOT)}: no description")
        names.add(role_path.stem)
    return names


def check_personas(role_names: set[str]) -> int:
    """Every persona must name a real role, or it generates nothing at all.

    Personas are `<role>-<domain>.md` and the setup step resolves the role by
    longest prefix match. A typo in the role half matches nothing, so the file
    is skipped silently: no subagent, no warning, no failing command. This turns
    that into a build error.
    """
    personas_dir = ROOT / "agents" / "personas"
    if not personas_dir.is_dir():
        return 0
    count = 0
    for persona_path in sorted(personas_dir.glob("*.md")):
        name = persona_path.stem
        if not any(name.startswith(f"{role}-") for role in role_names):
            fail("personas", f"{persona_path.relative_to(ROOT)}: no role matches the '<role>-<domain>' prefix")
            continue
        meta = frontmatter(persona_path)
        if str(meta.get("name", "")) != name:
            fail("personas", f"{persona_path.relative_to(ROOT)}: frontmatter name '{meta.get('name','')}' != file name")
        if not str(meta.get("description", "")).strip():
            fail("personas", f"{persona_path.relative_to(ROOT)}: no description")
        count += 1
    return count


def check_workflow_scripts() -> None:
    """Dynamic workflow scripts must satisfy the runtime's rules."""
    forbidden = ("Date.now(", "Math.random(", "new Date(", "import(", "require(", "process.")
    for script in sorted((ROOT / "workflows" / "scripts").glob("*.js")):
        rel = script.relative_to(ROOT)
        text = script.read_text(encoding="utf-8")
        if not text.startswith("export const meta = {"):
            fail("scripts", f"{rel}: 'export const meta' must be the first statement")
            continue
        meta_block = text[: text.index("\n}\n") + 3]
        for token in forbidden:
            if token in text:
                fail("scripts", f"{rel}: uses {token} — unavailable in the workflow runtime")
        if re.search(r"\$\{|\.\.\.", meta_block.split("phases:")[0]):
            fail("scripts", f"{rel}: meta must be a pure literal (no interpolation or spreads)")
        declared = set(re.findall(r"title: '([^']+)'", meta_block))
        used = set(re.findall(r"phase\('([^']+)'\)", text[len(meta_block):]))
        for title in sorted(used - declared):
            fail("scripts", f"{rel}: phase('{title}') has no entry in meta.phases")


def check_hooks() -> None:
    for hook in sorted((ROOT / "hooks").glob("*.py")):
        result = subprocess.run([sys.executable, "-m", "py_compile", str(hook)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail("hooks", f"{hook.relative_to(ROOT)}: does not compile\n{result.stderr.strip()}")


def check_references() -> None:
    """Paths and rule/skill references named in the kit's own docs must exist."""
    patterns = (
        (re.compile(r"\$AGENT_KIT/([A-Za-z0-9_./-]+)"), lambda m: m.group(1)),
        (re.compile(r"`rules/([A-Za-z0-9_.-]+\.mdc)`"), lambda m: f"rules/{m.group(1)}"),
    )
    targets = list((ROOT / "workflows").glob("*.yaml")) + list((ROOT / "rules").glob("*.mdc"))
    targets += [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "handoff.md.template"]
    targets += list((ROOT / "skills" / "kit-orchestration").rglob("*.md"))
    for doc in targets:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        for pattern, extract in patterns:
            for match in pattern.finditer(text):
                ref = extract(match).rstrip(".,:;)")
                if "{" in ref or "*" in ref or ref.endswith("/"):
                    continue
                if not (ROOT / ref).exists():
                    fail("references", f"{doc.relative_to(ROOT)}: '{ref}' does not exist")


def check_index(skill_ids: set[str]) -> None:
    """skills-index.json must match what is on disk, with kit-relative paths.

    Compared field by field rather than by regenerating: running `generate` here would
    rewrite generated workflow skills as a side effect of a read-only check.
    """
    index_path = ROOT / "skills-index.json"
    if not index_path.is_file():
        fail("index", "skills-index.json is missing — run `ai-resources generate`")
        return
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("index", f"skills-index.json is not valid JSON ({exc})")
        return

    stale = "run `ai-resources generate` (with AGENT_SKILLS_ROOT unset) and commit the result"
    entries = {entry["id"]: entry for entry in data.get("skills", [])}
    missing = sorted(skill_ids - set(entries))
    extra = sorted(set(entries) - skill_ids)
    if missing:
        fail("index", f"not indexed: {', '.join(missing[:5])} — {stale}")
    if extra:
        fail("index", f"indexed but gone from disk: {', '.join(extra[:5])} — {stale}")

    for skill_id, entry in sorted(entries.items()):
        if Path(entry.get("path", "")).is_absolute():
            fail("index", f"{skill_id}: index paths must be relative to the kit root — {stale}")
            break
    def normalized(value: object) -> str:
        # The index collapses whitespace, so a `description: >` block keeps its meaning but
        # not its line breaks; compare the same way rather than flagging every block scalar.
        return " ".join(str(value).split())

    drifted: list[str] = []
    for skill_id in sorted(skill_ids & set(entries)):
        on_disk = frontmatter(ROOT / "skills" / skill_id / "SKILL.md")
        entry = entries[skill_id]
        if normalized(on_disk.get("description", "")) != normalized(entry.get("description", "")):
            drifted.append(f"{skill_id} (description)")
        elif str(on_disk.get("name", skill_id)) != entry.get("name", ""):
            drifted.append(f"{skill_id} (name)")
    if drifted:
        fail("index", f"{len(drifted)} entr(ies) differ from their SKILL.md "
                      f"({', '.join(drifted[:5])}) — {stale}")


def check_plugin_manifest(role_names: set[str]) -> None:
    """The plugin manifest must list every role file and point at paths that exist.

    `agents` takes files, not a directory, so the list rots as roles come and go;
    `ai-resources generate` rewrites it and this check keeps it honest.
    """
    manifest_path = ROOT / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        fail("plugin", ".claude-plugin/plugin.json is missing")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("plugin", f"plugin.json is not valid JSON ({exc})")
        return

    # Sort by file name, exactly as `generate.py` does: sorting stems would order
    # `doc.md` before `doc-writer.md` and leave the two permanently disagreeing.
    expected = [f"./agents/roles/{p.name}" for p in sorted((ROOT / "agents" / "roles").glob("*.md"))]
    if manifest.get("agents") != expected:
        fail("plugin", "plugin.json agents is out of step with agents/roles/ — run `ai-resources generate`")
    for key in ("agents", "workflows", "hooks", "skills"):
        value = manifest.get(key)
        for path in ([value] if isinstance(value, str) else value or []):
            if isinstance(path, str) and not (ROOT / path).exists():
                fail("plugin", f"plugin.json {key}: '{path}' does not exist")

    marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
    if not marketplace_path.is_file():
        fail("plugin", ".claude-plugin/marketplace.json is missing")
        return
    try:
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("plugin", f"marketplace.json is not valid JSON ({exc})")
        return
    names = {entry.get("name") for entry in marketplace.get("plugins", [])}
    if manifest.get("name") not in names:
        fail("plugin", f"marketplace.json does not list the plugin '{manifest.get('name')}'")


def check_vendored_provenance() -> None:
    """Every vendored skill records where it came from, and the source is pinned."""
    manifest = json.loads((ROOT / "resources.json").read_text(encoding="utf-8"))
    for source in manifest.get("skills", {}).get("sources", []):
        if not source.get("sha"):
            fail("supply-chain",
                 f"resources.json: source '{source.get('id')}' is not pinned to a commit "
                 f"(add \"sha\"); ref '{source.get('ref')}' can move under us")
    for meta_path in sorted((ROOT / "skills").glob("*/.skill-source.yaml")):
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        if not meta.get("git_revision") or meta.get("git_revision") == "unknown":
            fail("supply-chain", f"{meta_path.relative_to(ROOT)}: no git_revision recorded")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    skill_ids = check_skills()
    role_names = check_roles()
    persona_count = check_personas(role_names)
    check_workflows(skill_ids, role_names)
    check_skill_scope(skill_ids, role_names)
    check_haiku_budget()
    check_role_effort(role_names)
    check_workflow_scripts()
    check_hooks()
    check_references()
    check_plugin_manifest(role_names)
    check_vendored_provenance()
    check_index(skill_ids)

    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} problem(s):\n", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"kit OK — {len(skill_ids)} skills, {len(role_names)} roles, "
          f"{persona_count} personas, "
          f"{len(list((ROOT / 'workflows').glob('*.workflow.yaml')))} workflows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
