#!/usr/bin/env python3
"""
Terraform Version Commit: commit changes with semver bump.
Reads last tag, analyzes .tf diff, bumps major/minor/patch, commits and tags.
Generates or updates CHANGELOG.md with release entries.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CHANGELOG_FILENAME = "CHANGELOG.md"
MODULE_NAME_PREFIX = "tf-module-"

# Registry: app.terraform.io/<your-org>/<module_name>/module
REGISTRY_SOURCE_RE = re.compile(
    r'source\s*=\s*["\']app\.terraform\.io/[^/]+/([a-z0-9][a-z0-9-]*)(?:/module)?["\']',
    re.IGNORECASE,
)
# Local: ../tf-module-<name> or ./tf-module-<name>
LOCAL_MODULE_RE = re.compile(
    r'source\s*=\s*["\'][^"\']*tf-module-([a-z0-9][a-z0-9-]*)["\']',
    re.IGNORECASE,
)

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[a-zA-Z0-9.-]+)?$")


def run(cmd: list[str], cwd: Path | None = None, capture: bool = True) -> tuple[int, str]:
    r = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


def get_last_tag(repo: Path) -> str | None:
    code, out = run(["git", "describe", "--tags", "--abbrev=0"], cwd=repo)
    if code == 0 and out.strip() and SEMVER_RE.search(out.strip()):
        return out.strip()
    code, out = run(["git", "tag", "-l", "v*", "--sort=-v:refname"], cwd=repo)
    tags = [t.strip() for t in out.strip().splitlines() if t.strip() and SEMVER_RE.search(t.strip())]
    return tags[0] if tags else None


def needs_release(repo: Path) -> bool:
    """True if repo has uncommitted changes in any file, or at least one commit since last tag (or no tag)."""
    if not _tf_paths(repo):
        return False
    # Uncommitted changes in any file (all files considered)
    code, status = run(["git", "status", "--porcelain"], cwd=repo)
    if code == 0 and status.strip():
        return True
    last_tag = get_last_tag(repo)
    if not last_tag:
        # No tag yet → treat as needs release if there are any commits
        code, out = run(["git", "rev-list", "--count", "HEAD"], cwd=repo)
        return code == 0 and out.strip() and int(out.strip()) > 0
    # Commits since last tag?
    code, out = run(["git", "rev-list", "--count", f"{last_tag}..HEAD"], cwd=repo)
    if code != 0:
        return False
    return out.strip() and int(out.strip()) > 0


def discover_tf_module_repos(parent: Path) -> list[Path]:
    """List tf-module-* directories under parent that are git repos. Prefer parent/tf-modules/ if present."""
    parent = parent.resolve()
    search_dir = parent / "tf-modules" if (parent / "tf-modules").is_dir() else parent
    repos: list[Path] = []
    for p in sorted(search_dir.iterdir()):
        if not p.is_dir() or not p.name.startswith(MODULE_NAME_PREFIX):
            continue
        if (p / ".git").exists():
            repos.append(p)
    return repos


def get_tf_modules_search_dir(parent: Path) -> Path:
    """Return the directory that contains tf-module-* repos (for use as modules_root in upgrade-providers)."""
    parent = parent.resolve()
    return parent / "tf-modules" if (parent / "tf-modules").is_dir() else parent


def get_upgrade_providers_script_path() -> Path | None:
    """Return path to terraform-provider-upgrade upgrade-providers.py (sibling skill). Tries multiple locations."""
    candidates: list[Path] = []
    # 1. Sibling of this skill: .../skills/terraform-version-commit/scripts -> .../skills/terraform-provider-upgrade/scripts/upgrade-providers.py
    script_dir = Path(__file__).resolve().parent
    skills_dir = script_dir.parent.parent
    candidates.append(skills_dir / "terraform-provider-upgrade" / "scripts" / "upgrade-providers.py")
    # 2. Env override (e.g. for CI or custom installs)
    env_path = os.environ.get("TERRAFORM_PROVIDER_UPGRADE_SCRIPT")
    if env_path:
        candidates.insert(0, Path(env_path).resolve())
    # 3. ai-resources layout via AGENT_KIT
    agent_kit = os.environ.get("AGENT_KIT")
    if agent_kit:
        candidates.append(
            Path(agent_kit)
            / "cursor"
            / "skills"
            / "terraform-provider-upgrade"
            / "scripts"
            / "upgrade-providers.py"
        )
    for p in candidates:
        if p.is_file():
            return p
    return None


def run_upgrade_providers(repo: Path, modules_root: Path, upgrade_script: Path, report_path: Path | None) -> int:
    """Run upgrade-providers.py on repo (providers + internal module versions from modules_root). Return 0 on success."""
    # Script expects: root [report_path] [modules_root]; modules_root must be 3rd arg when used
    cmd = [sys.executable, str(upgrade_script), str(repo)]
    if report_path is not None:
        cmd.append(str(report_path))
    cmd.append(str(modules_root))
    code, out = run(cmd, cwd=None, capture=True)
    if out.strip():
        print(out.strip())
    return code


def run_terraform_init_upgrade(repo: Path) -> int:
    """Run terraform init -upgrade in repo so .terraform/modules has latest from registry (editors work properly). Return 0 on success."""
    if not list(repo.glob("*.tf")):
        return 0
    code, out = run(["terraform", "init", "-upgrade", "-input=false"], cwd=repo, capture=True)
    if code != 0 and out.strip():
        print(out.strip(), file=sys.stderr)
    return code


def has_uncommitted_changes(repo: Path) -> bool:
    """Return True if repo has any uncommitted changes (staged or unstaged)."""
    code, out = run(["git", "status", "--porcelain"], cwd=repo, capture=True)
    return code == 0 and bool(out.strip())


def commit_and_push_dependent(proj: Path, no_push: bool) -> int:
    """
    If the dependent project has uncommitted changes (e.g. from upgrade-providers),
    commit and push so the next TFC/CI run uses the new module versions.
    Returns 0 on success, 1 on failure. Skips if no changes or not a git repo.
    """
    if not (proj / ".git").is_dir():
        return 0
    if not has_uncommitted_changes(proj):
        return 0
    print(f"  Commit and push (dependent): {proj.name}")
    code, out = run(["git", "add", "-A"], cwd=proj, capture=True)
    if code != 0:
        print(out.strip(), file=sys.stderr)
        return 1
    code, out = run(
        ["git", "commit", "-m", "chore(terraform): upgrade provider and module versions"],
        cwd=proj,
        capture=True,
    )
    if code != 0:
        if "nothing to commit" in (out or "").lower():
            return 0
        print(out.strip(), file=sys.stderr)
        return 1
    if no_push:
        print(f"  (no-push: skip push for {proj.name})")
        return 0
    code, out = run(["git", "push"], cwd=proj, capture=True)
    if code != 0:
        print(out.strip(), file=sys.stderr)
        return 1
    return 0


def get_dependent_projects(parent: Path, from_args: list[Path] | None) -> list[Path]:
    """Return list of dependent project roots from --dependent-projects args. Returns empty list if not provided."""
    if from_args is not None:
        return [p.resolve() for p in from_args if p.resolve().is_dir()]
    return []


def repo_to_module_name(repo: Path) -> str:
    """Return registry-style module name from repo dir (e.g. tf-module-module-base -> module-base)."""
    name = repo.name
    if name.startswith(MODULE_NAME_PREFIX):
        return name[len(MODULE_NAME_PREFIX):]
    return name


def parse_module_dependencies(repo: Path) -> set[str]:
    """Parse all .tf files in repo and return set of referenced private module names (e.g. module-a)."""
    deps: set[str] = set()
    for tf_path in repo.rglob("*.tf"):
        if ".git" in str(tf_path):
            continue
        try:
            text = tf_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in (REGISTRY_SOURCE_RE, LOCAL_MODULE_RE):
            for m in pattern.finditer(text):
                deps.add(m.group(1))
    return deps


def build_dep_graph(repos: list[Path]) -> dict[Path, list[Path]]:
    """Build dependency graph: dep_graph[repo] = list of repos this repo depends on (same list only)."""
    name_to_repo = {repo_to_module_name(r): r for r in repos}
    dep_graph: dict[Path, list[Path]] = {}
    for repo in repos:
        names = parse_module_dependencies(repo)
        dep_graph[repo] = [name_to_repo[n] for n in names if n in name_to_repo]
    return dep_graph


def collect_upgrade_set(needing: list[Path], dep_graph: dict[Path, list[Path]]) -> list[Path]:
    """
    Return needing plus all (transitive) dependencies of needing.
    Upgrade runs on this set in dependency order (roots first), so e.g. module-base before module-child.
    """
    upgrade_set = set(needing)
    added = True
    while added:
        added = False
        for repo in list(upgrade_set):
            for dep in dep_graph.get(repo, []):
                if dep not in upgrade_set:
                    upgrade_set.add(dep)
                    added = True
    return list(upgrade_set)


def toposort_release_order(repos: list[Path], dep_graph: dict[Path, list[Path]]) -> list[Path]:
    """
    Return repos in release order: dependencies first, then dependents.
    So if A depends on B, B is released before A. Uses Kahn's algorithm.
    If a cycle exists, raises ValueError.
    """
    repo_set = set(repos)
    # rev_deps[B] = list of A such that B is in dep_graph[A] (A depends on B -> B before A)
    rev_deps: dict[Path, list[Path]] = {r: [] for r in repos}
    for a in repos:
        for b in dep_graph.get(a, []):
            if b in repo_set:
                rev_deps[b].append(a)
    # in_degree[A] = number of deps of A that are in repos (must be released before A)
    in_degree = {
        a: len([b for b in dep_graph.get(a, []) if b in repo_set])
        for a in repos
    }
    queue = [a for a in repos if in_degree[a] == 0]
    order: list[Path] = []
    while queue:
        b = queue.pop(0)
        order.append(b)
        for a in rev_deps[b]:
            in_degree[a] -= 1
            if in_degree[a] == 0:
                queue.append(a)
    if len(order) != len(repos):
        raise ValueError("Dependency cycle among tf-modules (cannot determine release order)")
    return order


def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = SEMVER_RE.match(tag.lstrip("v"))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def next_version(major: int, minor: int, patch: int, bump: str) -> tuple[int, int, int]:
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def _tf_paths(repo: Path) -> list[str]:
    return [
        str(p.relative_to(repo))
        for p in repo.rglob("*.tf")
        if ".git" not in str(p) and ".terraform" not in str(p)
    ]


@dataclass
class ChangeItem:
    """One logical change from a diff (variable, resource, module, output, etc.)."""
    file: str
    kind: str  # variable_removed_default, variable_required, variable_added, resource_added, resource_removed, module_version, output_changed, etc.
    name: str  # variable/resource/output name or type
    detail: str = ""  # extra context (e.g. "default removed")


def _get_uncommitted_diff(repo: Path) -> str:
    """Return combined staged + unstaged diff for all .tf files."""
    paths = _tf_paths(repo)
    if not paths:
        return ""
    code1, staged = run(["git", "diff", "--cached", "--unified=0", "--"] + paths, cwd=repo)
    code2, unstaged = run(["git", "diff", "--unified=0", "--"] + paths, cwd=repo)
    return (staged or "") + "\n" + (unstaged or "")


def _get_uncommitted_diff_for_files(repo: Path, files: list[str]) -> str:
    """Return combined staged + unstaged diff for the given .tf files only."""
    if not files:
        return ""
    code1, staged = run(["git", "diff", "--cached", "--unified=5", "--"] + files, cwd=repo)
    code2, unstaged = run(["git", "diff", "--unified=5", "--"] + files, cwd=repo)
    return (staged or "") + "\n" + (unstaged or "")


def _get_modified_tf_files(repo: Path) -> list[str]:
    """Return list of .tf file paths that have uncommitted changes (staged or unstaged)."""
    code, out = run(["git", "status", "--porcelain", "--"] + _tf_paths(repo), cwd=repo)
    if code != 0 or not out.strip():
        return []
    files = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        # porcelain: XY path (XY = 2 chars; path starts after optional space)
        path = line[2:].lstrip() if len(line) > 2 else ""
        if path.endswith(".tf"):
            files.append(path)
    return list(dict.fromkeys(files))


def _parse_var_name(line: str) -> str | None:
    """Extract variable name from 'variable "foo"' or '-variable "foo"'. """
    m = re.search(r'variable\s+"([^"]+)"', line)
    return m.group(1) if m else None


def _parse_resource_type_name(line: str) -> tuple[str, str] | None:
    """Extract type and name from 'resource "type" "name"'. """
    m = re.search(r'resource\s+"([^"]+)"\s+"([^"]+)"', line)
    return (m.group(1), m.group(2)) if m else None


def _parse_output_name(line: str) -> str | None:
    m = re.search(r'output\s+"([^"]+)"', line)
    return m.group(1) if m else None


def analyze_diff_deep(repo: Path) -> list[ChangeItem]:
    """
    Analyze uncommitted .tf diff and return a list of structured change items.
    Used to generate good commit messages and to decide whether to split commits.
    """
    diff = _get_uncommitted_diff(repo)
    if not diff.strip():
        return []

    items: list[ChangeItem] = []
    current_file = ""
    # Split diff by file (git diff shows +++/--- for each file)
    for line in diff.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            # Use the new path (b/) for current file
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
            continue
        if not current_file or not current_file.endswith(".tf"):
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Variable: removed default (line with -default)
        if re.match(r'^-\s*default\s*=', stripped):
            # Find variable name from context: look backwards in diff for same file
            chunk = diff[: diff.find(line)].rsplit("+++ b/", 1)[-1] if line in diff else ""
            for prev in chunk.splitlines()[-15:]:
                v = _parse_var_name(prev)
                if v:
                    items.append(ChangeItem(current_file, "variable_removed_default", v, "default removed"))
                    break
            continue
        # Variable: added (new variable block)
        if re.match(r'^\+\s*variable\s+"', stripped):
            v = _parse_var_name(stripped)
            if v:
                items.append(ChangeItem(current_file, "variable_added", v, ""))
            continue
        # Variable: removed entire block
        if re.match(r'^-\s*variable\s+"', stripped):
            v = _parse_var_name(stripped)
            if v:
                items.append(ChangeItem(current_file, "variable_removed", v, ""))
            continue
        # Variable: description/type change (same variable, modified)
        if re.match(r'^[+-]\s*(description|type)\s*=', stripped):
            chunk = diff[: diff.find(line)].rsplit("+++ b/", 1)[-1] if line in diff else ""
            if "variable" in chunk:
                for prev in chunk.splitlines()[-8:]:
                    v = _parse_var_name(prev)
                    if v:
                        items.append(ChangeItem(current_file, "variable_updated", v, stripped[:50]))
                        break
            continue

        # Resource: added
        if re.match(r'^\+\s*resource\s+"', stripped):
            t = _parse_resource_type_name(stripped)
            if t:
                items.append(ChangeItem(current_file, "resource_added", f"{t[0]}.{t[1]}", ""))
            continue
        # Resource: removed
        if re.match(r'^-\s*resource\s+"', stripped):
            t = _parse_resource_type_name(stripped)
            if t:
                items.append(ChangeItem(current_file, "resource_removed", f"{t[0]}.{t[1]}", ""))
            continue

        # Module: version/source change
        if re.match(r'^[+-]\s*version\s*=\s*"', stripped):
            chunk = diff[: diff.find(line)].rsplit("+++ b/", 1)[-1] if line in diff else ""
            if "module" in chunk or "required_providers" in chunk:
                items.append(ChangeItem(current_file, "module_version", "module", stripped[:60]))
            continue
        if re.match(r'^[+-]\s*source\s*=', stripped):
            items.append(ChangeItem(current_file, "module_source", "module", stripped[:60]))
            continue

        # Output: added/removed/changed
        if re.match(r'^\+\s*output\s+"', stripped):
            o = _parse_output_name(stripped)
            if o:
                items.append(ChangeItem(current_file, "output_added", o, ""))
            continue
        if re.match(r'^-\s*output\s+"', stripped):
            o = _parse_output_name(stripped)
            if o:
                items.append(ChangeItem(current_file, "output_removed", o, ""))
            continue
    # Deduplicate by (file, kind, name) keeping first
    seen: set[tuple[str, str, str]] = set()
    unique: list[ChangeItem] = []
    for it in items:
        key = (it.file, it.kind, it.name)
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def _scope_from_file(file: str) -> str:
    """Return logical scope for ordering: variables < main < outputs < versions < other."""
    base = Path(file).name.lower()
    if base == "variables.tf":
        return "variables"
    if base == "main.tf":
        return "main"
    if base == "outputs.tf":
        return "outputs"
    if base == "versions.tf":
        return "versions"
    return "other"


def _should_split_commits(changes: list[ChangeItem]) -> bool:
    """
    Decide whether to use multiple commits. Split when:
    - Changes touch 2+ files with different scopes (e.g. variables.tf + main.tf), or
    - Many change items (e.g. 6+) so that per-file commits are clearer.
    Single file with few items → one commit.
    """
    if len(changes) <= 2:
        return False
    files = {c.file for c in changes}
    if len(files) == 1:
        # One file: split only if many items (e.g. 5+ variables) so we could do "variables: X" and "variables: Y"
        return len(changes) >= 6
    scopes = {_scope_from_file(f) for f in files}
    # Two or more distinct files → split so each file (or scope) gets its own commit
    return len(scopes) >= 2 or len(files) >= 2


def _group_changes_for_commits(changes: list[ChangeItem]) -> list[tuple[list[str], list[ChangeItem]]]:
    """
    Group changes into commit groups. Each group is (list of files to commit, list of change items).
    Order: variables first, then main, then outputs, then versions, then other.
    """
    if not changes:
        return []
    # Group by file (then by scope for ordering)
    by_file: dict[str, list[ChangeItem]] = {}
    for c in changes:
        by_file.setdefault(c.file, []).append(c)
    scope_order = ("variables", "main", "outputs", "versions", "other")
    sorted_files = sorted(
        by_file.keys(),
        key=lambda f: (scope_order.index(_scope_from_file(f)) if _scope_from_file(f) in scope_order else 99, f),
    )
    if not _should_split_commits(changes):
        all_changes = [c for fl in sorted_files for c in by_file[fl]]
        return [(sorted_files, all_changes)]
    return [([f], by_file[f]) for f in sorted_files]


def _commit_type_and_scope(changes: list[ChangeItem]) -> tuple[str, str]:
    """Infer conventional commit type and scope from change kinds."""
    kinds = [c.kind for c in changes]
    files = [c.file for c in changes]
    scope = _scope_from_file(files[0]) if files else "tf"
    if any(k in kinds for k in ("variable_removed", "variable_removed_default", "resource_removed", "output_removed", "module_source")):
        return "refactor", scope
    if any(k in kinds for k in ("variable_added", "resource_added", "output_added")):
        return "feat", scope
    if any(k in kinds for k in ("module_version",)):
        return "chore", scope
    return "chore", scope


def _short_summary(items: list[ChangeItem], max_names: int = 5) -> str:
    """Build a short summary phrase: what changed (e.g. 'require timeout_seconds, invoker; remove defaults')."""
    vars_removed_default = [c.name for c in items if c.kind == "variable_removed_default"]
    vars_added = [c.name for c in items if c.kind == "variable_added"]
    vars_removed = [c.name for c in items if c.kind == "variable_removed"]
    resources_added = [c.name for c in items if c.kind == "resource_added"]
    resources_removed = [c.name for c in items if c.kind == "resource_removed"]
    module_ver = [c for c in items if c.kind == "module_version"]
    parts = []
    if vars_removed_default:
        names = vars_removed_default[:max_names]
        parts.append("require " + ", ".join(names) + (" (remove defaults)" if len(names) == len(vars_removed_default) else " (remove defaults, ...)"))
    if vars_added:
        names = vars_added[:max_names]
        parts.append("add " + ", ".join(names) + ("" if len(vars_added) <= max_names else ", ..."))
    if vars_removed:
        names = vars_removed[:max_names]
        parts.append("remove variables " + ", ".join(names))
    if resources_added:
        parts.append("add " + ", ".join(resources_added[:max_names]))
    if resources_removed:
        parts.append("remove " + ", ".join(resources_removed[:max_names]))
    if module_ver:
        parts.append("bump module/provider versions")
    if not parts:
        # Fallback: list kinds
        kinds = list({c.kind for c in items})
        parts.append("update " + ", ".join(kinds[:3]))
    return "; ".join(parts)


def generate_commit_message(repo: Path, change_group: list[ChangeItem], custom_message: str | None) -> str:
    """
    Generate one commit message for a group of changes. Concise but descriptive.
    Uses conventional style: type(scope): summary. Optionally body with bullets if many items.
    """
    if custom_message and custom_message.strip():
        return custom_message.strip()
    if not change_group:
        return "chore: terraform changes"
    commit_type, scope = _commit_type_and_scope(change_group)
    summary = _short_summary(change_group)
    # Keep subject line under ~72 chars
    subject = f"{commit_type}({scope}): {summary}"
    if len(subject) > 72:
        subject = subject[:69] + "..."
    return subject


def _bump_from_diff_content(diff: str) -> str:
    """Return 'major'|'minor'|'patch' from diff text. Minor = BC only; anything breaking = major."""
    # Major: removed resources/modules/variables/outputs
    for pat in (
            r'^-resource\s+"',
            r'^-data\s+"',
            r'^-module\s+"',
            r'^-variable\s+"',
            r'^-output\s+"',
    ):
        if re.search(pat, diff, re.MULTILINE):
            return "major"

    # Major: variable or output changed (both - and + present => change/rename, breaking)
    if re.search(r'-\s*variable\s+"', diff, re.MULTILINE) and re.search(r'\+\s*variable\s+"', diff, re.MULTILINE):
        return "major"
    if re.search(r'-\s*output\s+"', diff, re.MULTILINE) and re.search(r'\+\s*output\s+"', diff, re.MULTILINE):
        return "major"

    # Major: provider version major bump (only in versions.tf / required_providers context)
    # Match version lines that look like required_providers (version = "~> N.)
    old_vers = re.findall(r'-\s*version\s*=\s*"~>\s*(\d+)\.', diff)
    new_vers = re.findall(r'\+\s*version\s*=\s*"~>\s*(\d+)\.', diff)
    for o, n in zip(old_vers, new_vers):
        if int(n) > int(o):
            return "major"

    # Minor: purely additive (new resource/data/module/variable/output, no corresponding removal)
    for pat in (
            r'^\+resource\s+"',
            r'^\+data\s+"',
            r'^\+module\s+"',
            r'^\+variable\s+"',
            r'^\+output\s+"',
    ):
        if re.search(pat, diff, re.MULTILINE):
            return "minor"

    return "patch"


def analyze_diff(repo: Path) -> str:
    """Return 'major'|'minor'|'patch' based on staged+unstaged .tf diff (for pre-commit bump)."""
    paths = _tf_paths(repo)
    if not paths:
        return "patch"
    code, staged = run(["git", "diff", "--cached", "--unified=0", "--"] + paths, cwd=repo)
    code2, unstaged = run(["git", "diff", "--unified=0", "--"] + paths, cwd=repo)
    return _bump_from_diff_content(staged + "\n" + unstaged)


def analyze_diff_tag_to_head(repo: Path, tag: str) -> str:
    """Return 'major'|'minor'|'patch' based on .tf diff between tag and HEAD (committed state)."""
    paths = _tf_paths(repo)
    if not paths:
        return "patch"
    code, diff = run(
        ["git", "diff", "--unified=0", f"{tag}..HEAD", "--"] + paths,
        cwd=repo,
    )
    if code != 0:
        return "patch"
    return _bump_from_diff_content(diff)


def analyze_commits_since_tag(repo: Path, tag: str) -> str:
    """Use conventional commits since last tag to determine bump."""
    code, out = run(
        ["git", "log", f"{tag}..HEAD", "--oneline", "--no-merges"],
        cwd=repo,
    )
    if code != 0 or not out.strip():
        return "patch"

    bump = "patch"
    for line in out.strip().splitlines():
        msg = line.split(" ", 1)[-1] if " " in line else line
        msg_lower = msg.lower()
        if "breaking" in msg_lower or "!" in msg or msg_lower.startswith("break:"):
            return "major"
        if msg_lower.startswith(("feat", "feature")):
            bump = "minor"
    return bump


def reason_from_diff(diff: str) -> str:
    """Human-readable reason for bump from diff content."""
    if re.search(r'^-resource\s+"', diff, re.MULTILINE):
        return "removed resource(s) (breaking)"
    if re.search(r'^-data\s+"', diff, re.MULTILINE):
        return "removed data source(s) (breaking)"
    if re.search(r'^-module\s+"', diff, re.MULTILINE):
        return "removed module(s) (breaking)"
    if re.search(r'^-variable\s+"', diff, re.MULTILINE):
        return "removed variable(s) (breaking)"
    if re.search(r'^-output\s+"', diff, re.MULTILINE):
        return "removed output(s) (breaking)"
    old_vers = re.findall(r'-\s*version\s*=\s*"~>\s*(\d+)\.', diff)
    new_vers = re.findall(r'\+\s*version\s*=\s*"~>\s*(\d+)\.', diff)
    for o, n in zip(old_vers, new_vers):
        if int(n) > int(o):
            return "major provider upgrade (breaking)"
    if re.search(r'^\+resource\s+"', diff, re.MULTILINE):
        return "new resource(s) (additive)"
    if re.search(r'^\+data\s+"', diff, re.MULTILINE):
        return "new data source(s) (additive)"
    if re.search(r'^\+module\s+"', diff, re.MULTILINE):
        return "new module(s) (additive)"
    if re.search(r'^\+variable\s+"', diff, re.MULTILINE):
        return "new variable(s) (additive)"
    if re.search(r'^\+output\s+"', diff, re.MULTILINE):
        return "new output(s) (additive)"
    return "minor .tf changes (patch)"


def get_commits_since_tag(repo: Path, tag: str | None) -> list[str]:
    """Return list of one-line commit messages since tag (or all if no tag)."""
    ref = f"{tag}..HEAD" if tag else "HEAD"
    code, out = run(
        ["git", "log", ref, "--oneline", "--no-merges"],
        cwd=repo,
    )
    if code != 0 or not out.strip():
        return []
    return [line.strip() for line in out.strip().splitlines()]


def get_changed_tf_files_since_tag(repo: Path, tag: str | None) -> list[str]:
    """Return list of .tf file paths changed since tag (or all if no tag)."""
    ref = f"{tag}..HEAD" if tag else "HEAD"
    paths = _tf_paths(repo)
    if not paths:
        return []
    code, out = run(
        ["git", "diff", "--name-only", ref, "--"] + paths,
        cwd=repo,
    )
    if code != 0 or not out.strip():
        return []
    return [line.strip() for line in out.strip().splitlines()]


def write_llm_context(
        repo: Path,
        out_path: Path,
        last_tag: str | None,
        new_ver: str,
        bump: str,
        reason_why: str,
        groups: list[tuple[list[str], list[ChangeItem]]],
) -> None:
    """Write a context file for an LLM (e.g. Cursor) to generate commit messages and changelog entries.
    The agent should produce a JSON file with commit_messages (list of strings) and optional changelog_entries.
    """
    lines = [
        "# Terraform version-commit LLM context",
        "",
        "Use this file to generate **commit messages** and optional **changelog entries** for the upcoming release.",
        "Run the script with `--messages-file <path>` after generating the messages file.",
        "",
        "## Repo and version",
        f"- **Repo:** {repo.resolve()}",
        f"- **Last tag:** {last_tag or '(none)'}",
        f"- **New version:** {new_ver}",
        f"- **Bump:** {bump}",
        f"- **Reason:** {reason_why}",
        "",
        "## Commit groups",
        "",
        "Generate one conventional commit message per group (type(scope): description).",
        "Optionally generate human-focused changelog bullets for `changelog_entries`.",
        "",
        "---",
        "",
    ]
    for i, (files_in_group, items_in_group) in enumerate(groups):
        diff = _get_uncommitted_diff_for_files(repo, files_in_group)
        items_repr = [
            {"file": c.file, "kind": c.kind, "name": c.name, "detail": c.detail}
            for c in items_in_group
        ]
        lines.append(f"### Group {i + 1}")
        lines.append("")
        lines.append("- **Files:** " + ", ".join(files_in_group))
        lines.append("- **Change items:**")
        for it in items_repr:
            lines.append(f"  - {it['file']}: {it['kind']} {it['name']}" + (f" ({it['detail']})" if it["detail"] else ""))
        lines.append("")
        lines.append("**Diff:**")
        lines.append("```diff")
        lines.append(diff.strip() if diff.strip() else "(no diff)")
        lines.append("```")
        lines.append("")
    lines.extend([
        "---",
        "",
        "## Output format",
        "",
        "Write a **JSON file** (e.g. `.version-commit-llm-messages.json` in the repo root) with:",
        "",
        "```json",
        '{',
        '  "commit_messages": [',
        '    "feat(variables): add timeout_seconds and invoker (no defaults)",',
        '    "chore(versions): bump provider versions"',
        '  ],',
        '  "changelog_entries": [',
        '    "Added timeout_seconds and invoker variables (breaking: no defaults).",',
        '    "Bumped Terraform provider versions."',
        '  ]',
        '}',
        "```",
        "",
        "- **commit_messages:** One string per group, in order. Conventional Commits style.",
        "- **changelog_entries:** (Optional) Human-focused bullets for CHANGELOG ### Changed. If omitted, commit subjects are used.",
        "",
    ])
    out_path.write_text("\n".join(lines), encoding="utf-8")


def read_llm_messages(path: Path) -> dict:
    """Read LLM-generated messages file (JSON). Returns dict with commit_messages (list) and optional changelog_entries (list)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(text)
    commit_messages = data.get("commit_messages")
    if not isinstance(commit_messages, list):
        raise ValueError("commit_messages must be a list of strings")
    for m in commit_messages:
        if not isinstance(m, str) or not m.strip():
            raise ValueError("Each commit_messages item must be a non-empty string")
    result = {"commit_messages": [m.strip() for m in commit_messages]}
    if "changelog_entries" in data and data["changelog_entries"] is not None:
        entries = data["changelog_entries"]
        if not isinstance(entries, list):
            raise ValueError("changelog_entries must be a list of strings")
        result["changelog_entries"] = [str(e).strip() for e in entries if str(e).strip()]
    return result


def build_release_section(
        repo: Path,
        last_tag: str | None,
        new_ver: str,
        reason_why: str,
        changelog_entries: list[str] | None = None,
) -> str:
    """Build the ## [X.Y.Z] - YYYY-MM-DD section body for CHANGELOG.
    If changelog_entries is provided, use it for ### Changed instead of commit subjects.
    """
    today = date.today().isoformat()
    lines = [f"## [{new_ver.lstrip('v')}] - {today}", ""]
    if changelog_entries:
        lines.append("### Changed")
        lines.append("")
        for msg in changelog_entries[:50]:
            safe = msg.strip()
            if safe.startswith("-"):
                safe = "\\" + safe
            lines.append(f"- {safe}")
        lines.append("")
    else:
        commits = get_commits_since_tag(repo, last_tag)
        files = get_changed_tf_files_since_tag(repo, last_tag)
        if commits:
            lines.append("### Changed")
            lines.append("")
            for msg in commits[:50]:  # cap at 50
                safe = msg.strip()
                if safe.startswith("-"):
                    safe = "\\" + safe
                lines.append(f"- {safe}")
            if len(commits) > 50:
                lines.append(f"- _... and {len(commits) - 50} more commits_")
            lines.append("")
        if files and not commits:
            lines.append("### Changed")
            lines.append("")
            for f in files[:30]:
                lines.append(f"- {f}")
            if len(files) > 30:
                lines.append(f"- _... and {len(files) - 30} more files_")
            lines.append("")
    # Always add a short summary from bump reason
    lines.append("### Summary")
    lines.append("")
    lines.append(f"- {reason_why}")
    lines.append("")
    return "\n".join(lines)


def read_existing_changelog(repo: Path) -> str:
    """Read existing CHANGELOG.md or return empty string."""
    path = repo / CHANGELOG_FILENAME
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def write_changelog(repo: Path, content: str) -> None:
    """Write CHANGELOG.md."""
    (repo / CHANGELOG_FILENAME).write_text(content, encoding="utf-8")


def insert_new_release_into_changelog(
        repo: Path,
        last_tag: str | None,
        new_ver: str,
        reason_why: str,
        changelog_entries: list[str] | None = None,
) -> str:
    """Create or update CHANGELOG.md with the new release. Returns new file content."""
    release_section = build_release_section(
        repo, last_tag, new_ver, reason_why, changelog_entries=changelog_entries
    )
    existing = read_existing_changelog(repo)

    if not existing.strip():
        header = (
            "# Changelog\n\n"
            "All notable changes to this project will be documented in this file.\n\n"
        )
        return header + release_section

    # Insert new release after the first ## [ (Unreleased or latest version) so newest release is first
    lines = existing.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("# "):
            insert_at = i + 1
        if re.match(r"^##\s+\[", line):
            insert_at = i + 1  # insert after this line
            break
    before = "\n".join(lines[:insert_at]) + ("\n\n" if lines[:insert_at] else "")
    after = "\n\n".join(lines[insert_at:]).lstrip() if lines[insert_at:] else ""
    return before + release_section + ("\n\n" + after if after else "")


def ensure_changelog_updated(
        repo: Path,
        last_tag: str | None,
        new_ver: str,
        reason_why: str,
        changelog_entries: list[str] | None = None,
) -> bool:
    """Create or update CHANGELOG.md with the new release. Returns True if file was written."""
    content = insert_new_release_into_changelog(
        repo, last_tag, new_ver, reason_why, changelog_entries=changelog_entries
    )
    path = repo / CHANGELOG_FILENAME
    path.write_text(content, encoding="utf-8")
    return True


def _print_semver_summary(
        repo: Path,
        last_tag: str | None,
        major: int,
        minor: int,
        patch: int,
        bump: str,
        new_ver: str,
        reason_why: str,
) -> None:
    """Print repo, previous tag, semver calculation, new tag, and reason."""
    current_ver = f"v{major}.{minor}.{patch}"
    repo_name = repo.name or (repo.resolve().name if repo.resolve() != repo else ".")
    print("---")
    print(f"Repo:          {repo_name}")
    print(f"Previous tag:  {last_tag or '(none)'}")
    print(f"Calculation:   {current_ver} + bump {bump} → {new_ver}")
    print(f"New tag:       {new_ver}")
    print(f"Reason:        {reason_why}")
    print("---")


def get_bump_reason(repo: Path, last_tag: str | None, bump: str, from_override: bool) -> str:
    """Return human-readable reason for the chosen bump."""
    if from_override:
        return "manual override (--bump)"
    paths = _tf_paths(repo)
    if not paths:
        return "no .tf files (patch by default)"
    if last_tag:
        if paths:
            code, diff = run(
                ["git", "diff", "--unified=0", f"{last_tag}..HEAD", "--"] + paths,
                cwd=repo,
            )
            if code == 0 and diff.strip():
                return reason_from_diff(diff)
        code, out = run(
            ["git", "log", f"{last_tag}..HEAD", "--oneline", "--no-merges"],
            cwd=repo,
        )
        if code == 0 and out.strip():
            for line in out.strip().splitlines():
                msg = (line.split(" ", 1)[-1] if " " in line else line).lower()
                if "breaking" in msg or "!" in msg or msg.startswith("break:"):
                    return "commit(s) with breaking change"
                if msg.startswith(("feat", "feature")):
                    return "commit(s) feat (additive)"
    return "changes since last tag (patch by default)"


def run_single_repo(repo: Path, args: argparse.Namespace) -> int:
    """Run version-commit flow for one repository. Returns 0 on success, 1 on failure."""
    # 1. Last tag and current version
    last_tag = get_last_tag(repo)
    if last_tag:
        parsed = parse_version(last_tag)
        if not parsed:
            print(f"Invalid tag format: {last_tag}", file=sys.stderr)
            return 1
        major, minor, patch = parsed
    else:
        major, minor, patch = 1, 0, 0

    # 2. Determine bump (for dry-run use working tree diff; for real run we recompute after commit)
    order = {"patch": 0, "minor": 1, "major": 2}
    if args.bump:
        bump = args.bump
        reason = f"override (--bump {args.bump})"
    else:
        diff_bump = analyze_diff(repo)
        commit_bump = analyze_commits_since_tag(repo, last_tag or "HEAD") if last_tag else "patch"
        bump = max(diff_bump, commit_bump, key=lambda b: order[b])
        reason = f"diff+commits → {bump}"

    nmaj, nmin, npatch = next_version(major, minor, patch, bump)
    new_ver = f"v{nmaj}.{nmin}.{npatch}"
    reason_why = get_bump_reason(repo, last_tag, bump, bool(args.bump))

    # 2b. --write-llm-context: write context file and exit (no commit/tag)
    write_ctx = getattr(args, "write_llm_context", "") or ""
    if write_ctx.strip():
        write_ctx = write_ctx.strip()
        changes = analyze_diff_deep(repo)
        groups = _group_changes_for_commits(changes) if changes else []
        if not groups:
            # No structured groups: single logical commit
            all_modified = _get_modified_tf_files(repo) or _tf_paths(repo)
            groups = [(all_modified, changes)] if all_modified else [([], [])]
        out_path = Path(write_ctx).resolve() if os.path.isabs(write_ctx) else (repo / write_ctx)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_llm_context(repo, out_path, last_tag, new_ver, bump, reason_why, groups)
        print(f"Wrote LLM context to {out_path}")
        print("Generate commit_messages (and optional changelog_entries) with Cursor, then run with --messages-file <path>")
        return 0

    _print_semver_summary(repo, last_tag, major, minor, patch, bump, new_ver, reason_why)

    if args.dry_run:
        return 0

    # Load LLM messages file if provided (used for commits and optionally CHANGELOG)
    llm_messages: dict | None = None
    messages_file = getattr(args, "messages_file", None)
    if messages_file:
        mpath = Path(messages_file).resolve() if os.path.isabs(messages_file) else (repo / messages_file)
        if not mpath.is_file():
            print(f"Messages file not found: {mpath}", file=sys.stderr)
            return 1
        try:
            llm_messages = read_llm_messages(mpath)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Invalid messages file: {e}", file=sys.stderr)
            return 1

    # 3. Commit any uncommitted .tf changes (smart: one commit or split by scope/file with descriptive messages)
    tf_files = _tf_paths(repo)
    if tf_files:
        run(["git", "add"] + tf_files, cwd=repo)
    code, status = run(["git", "status", "--porcelain"], cwd=repo)
    status = status.strip()
    if status:
        lines = [l.strip() for l in status.splitlines() if l.strip()]
        if len(lines) == 1 and CHANGELOG_FILENAME in lines[0]:
            run(["git", "add", CHANGELOG_FILENAME], cwd=repo)
            code, out = run(["git", "commit", "-m", "chore: add CHANGELOG.md"], cwd=repo)
            if code != 0:
                print(out, file=sys.stderr)
                return 1
            print("Committed CHANGELOG.md (initial).")
        else:
            # Unstage so we can stage per-group when splitting
            run(["git", "reset", "HEAD", "--"] + tf_files, cwd=repo)
            changes = analyze_diff_deep(repo)
            groups = _group_changes_for_commits(changes) if changes else []
            all_modified = _get_modified_tf_files(repo)
            if not groups:
                # No structured changes (e.g. only comments): single commit
                run(["git", "add"] + (all_modified or tf_files), cwd=repo)
                if llm_messages and len(llm_messages["commit_messages"]) == 1:
                    msg = llm_messages["commit_messages"][0]
                else:
                    msg = args.message or "chore: terraform changes (tf-modules)"
                code, out = run(["git", "commit", "-m", msg], cwd=repo)
                if code != 0:
                    print(out, file=sys.stderr)
                    return 1
                print("Committed .tf changes.")
            else:
                if llm_messages:
                    if len(llm_messages["commit_messages"]) != len(groups):
                        print(
                            f"Messages file has {len(llm_messages['commit_messages'])} commit_messages "
                            f"but there are {len(groups)} commit groups.",
                            file=sys.stderr,
                        )
                        return 1
                all_files_in_groups = {f for files, _ in groups for f in files}
                for i, (files_in_group, items_in_group) in enumerate(groups):
                    # Include any modified .tf not in any group in the first commit
                    to_add = list(files_in_group)
                    if i == 0 and all_modified:
                        for f in all_modified:
                            if f not in all_files_in_groups:
                                to_add.append(f)
                    run(["git", "add"] + to_add, cwd=repo)
                    if llm_messages:
                        msg = llm_messages["commit_messages"][i]
                    else:
                        use_custom = bool(args.message and args.message.strip()) and len(groups) == 1
                        msg = generate_commit_message(repo, items_in_group, args.message if use_custom else None)
                    code, out = run(["git", "commit", "-m", msg], cwd=repo)
                    if code != 0:
                        print(out, file=sys.stderr)
                        return 1
                    if len(groups) > 1:
                        print(f"Committed ({i + 1}/{len(groups)}): {msg[:60]}{'...' if len(msg) > 60 else ''}")
                    else:
                        print("Committed .tf changes.")

    # 4. Tag only when repo has no more changes to commit (commit lone CHANGELOG.md if present)
    code, status = run(["git", "status", "--porcelain"], cwd=repo)
    status = status.strip()
    if status:
        lines = [l.strip() for l in status.splitlines() if l.strip()]
        # If only CHANGELOG.md is untracked/modified, commit it so repo is clean
        only_changelog = len(lines) == 1 and CHANGELOG_FILENAME in lines[0]
        if only_changelog:
            run(["git", "add", CHANGELOG_FILENAME], cwd=repo)
            code, out = run(["git", "commit", "-m", "chore: add CHANGELOG.md"], cwd=repo)
            if code == 0:
                print("Committed CHANGELOG.md (initial).")
            code, status = run(["git", "status", "--porcelain"], cwd=repo)
            status = status.strip()
        if status:
            # Fallback: if only .tf files are left uncommitted, stage and commit them (handles parse/group edge cases)
            mod_tf = _get_modified_tf_files(repo)
            if mod_tf:
                run(["git", "add"] + mod_tf, cwd=repo)
                code, out = run(
                    ["git", "commit", "-m", args.message or "chore: terraform changes (tf-modules)"],
                    cwd=repo,
                )
                if code == 0:
                    print("Committed .tf changes (fallback).")
                    code, status = run(["git", "status", "--porcelain"], cwd=repo)
                    status = status.strip()
            if status:
                print("Repo has uncommitted changes. Commit or stash before tagging.", file=sys.stderr)
                return 1

    # 5. Recompute bump from last_tag..HEAD (what actually changed in committed history)
    if args.bump:
        bump = args.bump
    else:
        diff_bump = analyze_diff_tag_to_head(repo, last_tag) if last_tag else "patch"
        commit_bump = analyze_commits_since_tag(repo, last_tag) if last_tag else "patch"
        bump = max(diff_bump, commit_bump, key=lambda b: order[b])
    nmaj, nmin, npatch = next_version(major, minor, patch, bump)
    new_ver = f"v{nmaj}.{nmin}.{npatch}"
    reason_why = get_bump_reason(repo, last_tag, bump, bool(args.bump))

    # Summary and confirmation: print calculated semver and ask whether to use this version
    _print_semver_summary(repo, last_tag, major, minor, patch, bump, new_ver, reason_why)

    if not args.yes:
        try:
            resp = input(f"Use version {new_ver} and create the tag? (y/n) [n]: ").strip().lower() or "n"
        except EOFError:
            resp = "n"
        if resp not in ("y", "yes"):
            print("Tag not applied.")
            return 0

    # 5b. Update or create CHANGELOG.md with this release, then commit it (unless --no-changelog)
    if not getattr(args, "no_changelog", False):
        changelog_entries = None
        if llm_messages and llm_messages.get("changelog_entries"):
            changelog_entries = llm_messages["changelog_entries"]
        ensure_changelog_updated(repo, last_tag, new_ver, reason_why, changelog_entries=changelog_entries)
        run(["git", "add", CHANGELOG_FILENAME], cwd=repo)
        code_staged, _ = run(["git", "diff", "--cached", "--quiet"], cwd=repo)
        if code_staged != 0:
            code, out = run(
                ["git", "commit", "-m", f"chore: update CHANGELOG for {new_ver}"],
                cwd=repo,
            )
            if code == 0:
                print(f"Updated {CHANGELOG_FILENAME} and committed.")

    # 6. Create tag
    code, out = run(["git", "tag", "-a", new_ver, "-m", f"Release {new_ver}"], cwd=repo)
    if code != 0:
        print(out, file=sys.stderr)
        return 1

    print(f"Tagged {new_ver}")

    # 7. Push repo and tags so dependents can use the new version
    if not args.no_push:
        code, out = run(["git", "push"], cwd=repo)
        if code != 0:
            print(out, file=sys.stderr)
            return 1
        code, out = run(["git", "push", "--tags"], cwd=repo)
        if code != 0:
            print(out, file=sys.stderr)
            return 1
        print("Pushed branch and tags.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Terraform version commit with semver bump")
    ap.add_argument("--bump", choices=["patch", "minor", "major"], help="Override auto bump")
    ap.add_argument("--dry-run", action="store_true", help="Show next version only")
    ap.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation; apply tag")
    ap.add_argument("--no-push", action="store_true", help="Do not run git push after tagging")
    ap.add_argument("--no-changelog", action="store_true", help="Do not create or update CHANGELOG.md")
    ap.add_argument("--message", "-m", default="", help="Custom commit message")
    ap.add_argument(
        "--write-llm-context",
        type=str,
        default="",
        metavar="FILE",
        help="Write LLM context file (e.g. .version-commit-llm-context.md) and exit. Use Cursor to generate commit_messages and optional changelog_entries, then run with --messages-file.",
    )
    ap.add_argument(
        "--messages-file",
        type=str,
        default="",
        metavar="FILE",
        help="JSON file with commit_messages (and optional changelog_entries) generated by an LLM (e.g. Cursor). Overrides auto-generated commit messages and CHANGELOG bullets.",
    )
    ap.add_argument(
        "--parent-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Discover tf-module-* git repos under PATH; release only repos with changes",
    )
    ap.add_argument(
        "--recursive",
        action="store_true",
        help="With --parent-dir: loop until nothing to release. Each iteration runs provider+module upgrade on all repos, then releases in dependency order. Requires terraform-provider-upgrade skill.",
    )
    ap.add_argument(
        "--dependent-projects",
        type=str,
        default="",
        metavar="PATH1,PATH2",
        help="With --parent-dir: paths to dependent Terraform projects (comma-separated). Upgrade and terraform init -upgrade run on each. If omitted, auto-detection of common sibling dirs is attempted.",
    )
    ap.add_argument(
        "--release-all-in-upgrade-set",
        action="store_true",
        help="With --parent-dir: release every repo in the upgrade set (roots first), not only those with changes. Use for cascade when roots have no local changes but you want them tagged/pushed so dependents see new refs.",
    )
    ap.add_argument("root", nargs="?", default=".", help="Repository root (ignored if --parent-dir is set)")
    args = ap.parse_args()

    # Parse --dependent-projects: None = auto-detect, [] = none, [Path,...] = explicit list
    _dp_raw = getattr(args, "dependent_projects", "")
    args.dependent_projects_list = (
        [Path(p.strip()).resolve() for p in _dp_raw.split(",") if p.strip()]
        if _dp_raw
        else None
    )

    if args.parent_dir is not None:
        parent = args.parent_dir.resolve()
        if not parent.is_dir():
            print(f"Not a directory: {parent}", file=sys.stderr)
            return 1
        search_dir = get_tf_modules_search_dir(parent)
        repos = discover_tf_module_repos(parent)
        if not repos:
            print("No tf-module repos found.")
            return 0

        if args.recursive:
            # Recursive: run provider+module upgrade on all repos and dependent projects, then release in dep order; repeat until nothing to release
            upgrade_script = get_upgrade_providers_script_path()
            if not upgrade_script:
                print(
                    "Recursive mode requires terraform-provider-upgrade skill: "
                    "terraform-provider-upgrade/scripts/upgrade-providers.py not found.",
                    file=sys.stderr,
                )
                return 1
            report_path = Path(tempfile.gettempdir()) / "tf-version-commit-upgrade-report.txt"
            dependent_projects = get_dependent_projects(parent, args.dependent_projects_list)
            iteration = 0
            while True:
                iteration += 1
                print(f"\n========== Recursive iteration {iteration} ==========")
                try:
                    dep_graph = build_dep_graph(repos)
                    upgrade_order = toposort_release_order(repos, dep_graph)
                except ValueError as e:
                    print(f"Dependency error: {e}", file=sys.stderr)
                    return 1
                # 1. Run provider + internal module upgrade on every tf-module (in dep order)
                print("Step 1: Upgrade providers and internal module versions (terraform-provider-upgrade)...")
                upgrade_failed: list[Path] = []
                for repo in upgrade_order:
                    print(f"\n--- Upgrade: {repo.name} ---")
                    if run_upgrade_providers(repo, search_dir, upgrade_script, report_path) != 0:
                        upgrade_failed.append(repo)
                for proj in dependent_projects:
                    print(f"\n--- Upgrade (dependent): {proj.name} ---")
                    if run_upgrade_providers(proj, search_dir, upgrade_script, report_path) != 0:
                        upgrade_failed.append(proj)
                if upgrade_failed:
                    print(f"Upgrade failed for: {[r.name for r in upgrade_failed]}. Stopping.", file=sys.stderr)
                    return 1
                print("Step 1b: terraform init -upgrade (refresh module cache for editors)...")
                for repo in repos:
                    run_terraform_init_upgrade(repo)
                for proj in dependent_projects:
                    run_terraform_init_upgrade(proj)
                # 2. Which repos need release?
                needing = [r for r in repos if needs_release(r)]
                print(f"\nStep 2: Repos needing release: {len(needing)}")
                for r in repos:
                    mark = " [RELEASE]" if r in needing else ""
                    print(f"  - {r.name}{mark}")
                if not needing:
                    print("Nothing left to release. Done.")
                    return 0
                if args.dry_run:
                    print("Dry-run: would release in dependency order then repeat. Exiting after one iteration.")
                    return 0
                # 3. Release in dependency order
                release_order = toposort_release_order(needing, dep_graph)
                print("Release order (dependencies first):")
                for i, r in enumerate(release_order, 1):
                    dep_names = [d.name for d in dep_graph.get(r, [])]
                    dep_str = f" (depends on: {', '.join(dep_names)})" if dep_names else ""
                    print(f"  {i}. {r.name}{dep_str}")
                failed = 0
                for repo in release_order:
                    print(f"\n--- {repo.name} ---")
                    if run_single_repo(repo, args) != 0:
                        failed += 1
                if failed:
                    print(f"\n{failed} repo(s) failed.", file=sys.stderr)
                    return 1
                # 4. Refresh module cache so editors see latest from registry
                print("\nRefreshing module cache (terraform init -upgrade) in all tf-modules and dependent projects...")
                for repo in repos:
                    run_terraform_init_upgrade(repo)
                for proj in dependent_projects:
                    run_terraform_init_upgrade(proj)
                # 5. Commit and push dependents so next TFC/CI run uses new module versions
                if dependent_projects:
                    print("\nCommit and push dependents (so next run picks up fixed module versions)...")
                    for proj in dependent_projects:
                        if commit_and_push_dependent(proj, args.no_push) != 0:
                            print(f"Failed to commit/push {proj.name}. Stopping.", file=sys.stderr)
                            return 1
                # Loop again: next iteration will upgrade all (picking up new tags) and maybe more need release

        # Non-recursive (single pass): upgrade and release only repos that need release (bottom-up / dependency order)
        upgrade_script = get_upgrade_providers_script_path()
        report_path = Path(tempfile.gettempdir()) / "tf-version-commit-upgrade-report.txt"
        dependent_projects = get_dependent_projects(
            parent, args.dependent_projects_list if args.dependent_projects_list else None
        )

        needing = [r for r in repos if needs_release(r)]
        print(f"Parent: {parent}")
        print(f"Discovered {len(repos)} tf-module repo(s), {len(needing)} with changes (need release).")
        for r in repos:
            mark = " [RELEASE]" if r in needing else ""
            print(f"  - {r.name}{mark}")
        if not needing:
            print("Nothing to release.")
            if upgrade_script and not args.dry_run:
                print("Refreshing module cache (terraform init -upgrade) in all repos and dependents...")
                for repo in repos:
                    run_terraform_init_upgrade(repo)
                for proj in dependent_projects:
                    run_terraform_init_upgrade(proj)
            return 0
        # Release order = bottom-up (dependencies first), so we only run upgrade on this smaller set
        try:
            dep_graph = build_dep_graph(repos)
            release_order = toposort_release_order(needing, dep_graph)
        except ValueError as e:
            print(f"Dependency error: {e}", file=sys.stderr)
            return 1
        print("Release order (dependencies first):")
        for i, r in enumerate(release_order, 1):
            dep_names = [d.name for d in dep_graph.get(r, [])]
            dep_str = f" (depends on: {', '.join(dep_names)})" if dep_names else ""
            print(f"  {i}. {r.name}{dep_str}")
        if args.dry_run:
            print("Dry-run: would run version-commit on repos in the order above.")
            return 0
        # Upgrade set = needing + their dependencies; order = roots first so we can push and then pull.
        upgrade_set = collect_upgrade_set(needing, dep_graph)
        upgrade_order = toposort_release_order(upgrade_set, dep_graph)
        if upgrade_set:
            print("Order (dependency tree, roots first — each repo: upgrade → init → release so next sees new tags):")
            for i, r in enumerate(upgrade_order, 1):
                dep_names = [d.name for d in dep_graph.get(r, [])]
                dep_str = f" (depends on: {', '.join(dep_names)})" if dep_names else " (root)"
                print(f"  {i}. {r.name}{dep_str}")
        failed = 0
        if upgrade_script:
            # Per-repo: upgrade → init → release (commit/tag/push). Next repo then pulls latest (git fetch in siblings sees new tags).
            release_all = getattr(args, "release_all_in_upgrade_set", False)
            for repo in upgrade_order:
                print(f"\n=== {repo.name} ===")
                print("  Upgrade (providers + module refs)...")
                if run_upgrade_providers(repo, search_dir, upgrade_script, report_path) != 0:
                    print(f"Upgrade failed for {repo.name}. Stopping.", file=sys.stderr)
                    return 1
                print("  terraform init -upgrade (pull latest from registry)...")
                run_terraform_init_upgrade(repo)
                do_release = release_all or needs_release(repo)
                if do_release:
                    if release_all and not needs_release(repo):
                        print("  Release (--release-all-in-upgrade-set: commit/tag/push for cascade)...")
                    else:
                        print("  Release (commit, CHANGELOG, tag, push)...")
                    if run_single_repo(repo, args) != 0:
                        failed += 1
                        print(f"Release failed for {repo.name}. Stopping.", file=sys.stderr)
                        return 1
                else:
                    print("  No changes to release.")
            for proj in dependent_projects:
                print(f"\n=== {proj.name} (dependent) ===")
                if run_upgrade_providers(proj, search_dir, upgrade_script, report_path) != 0:
                    print(f"Upgrade failed for {proj.name}. Stopping.", file=sys.stderr)
                    return 1
                run_terraform_init_upgrade(proj)
        else:
            print("terraform-provider-upgrade script not found; releasing in order with init -upgrade before each.")
            for repo in release_order:
                print(f"\n=== {repo.name} ===")
                run_terraform_init_upgrade(repo)
                if needs_release(repo) and run_single_repo(repo, args) != 0:
                    failed += 1
                    return 1
        if failed:
            print(f"\n{failed} repo(s) failed.", file=sys.stderr)
            return 1
        # Final refresh so all repos have latest in .terraform
        print("\nRefreshing module cache (terraform init -upgrade) in all tf-modules and dependent projects...")
        for repo in repos:
            run_terraform_init_upgrade(repo)
        for proj in dependent_projects:
            run_terraform_init_upgrade(proj)
        # Commit and push dependents so next TFC/CI run uses new module versions (fixes picked up)
        if dependent_projects:
            print("\nCommit and push dependents (so next run picks up fixed module versions)...")
            for proj in dependent_projects:
                if commit_and_push_dependent(proj, args.no_push) != 0:
                    print(f"Failed to commit/push {proj.name}. Stopping.", file=sys.stderr)
                    return 1
        return 0

    repo = Path(args.root).resolve()
    if not (repo / ".git").exists():
        print("Not a git repository", file=sys.stderr)
        return 1
    return run_single_repo(repo, args)


if __name__ == "__main__":
    sys.exit(main())
