#!/usr/bin/env python3
"""
Orchestrates provider + internal module upgrades across all tf-module-* repos
in dependency order: modules with no internal deps (or whose deps are already
updated) are processed first. For each module we run upgrade-providers.py,
then version-commit.py --yes (which commits, tags, and pushes) so the next
module in the chain can use the new tag.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Source pattern: app.terraform.io/wildbit/NAME/module
WILDBIT_MODULE_RE = re.compile(
    r'source\s*=\s*"app\.terraform\.io/wildbit/([^/]+)/module"',
    re.IGNORECASE,
)


def get_internal_deps(module_dir: Path, all_module_names: set[str]) -> set[str]:
    """Return set of internal module names (e.g. gcp-kms) this module depends on."""
    deps: set[str] = set()
    for tf in module_dir.rglob("*.tf"):
        if ".git" in str(tf):
            continue
        try:
            content = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in WILDBIT_MODULE_RE.finditer(content):
            name = m.group(1)
            if name in all_module_names:
                deps.add(name)
    return deps


def dependency_order(tf_modules_root: Path) -> list[str]:
    """
    Return list of module names (e.g. gcp-kms, gcp-artifact-registry) in an order
    such that every dependency is processed before its dependents. Modules with
    no internal deps come first.
    """
    module_dirs = [
        d
        for d in tf_modules_root.iterdir()
        if d.is_dir() and d.name.startswith("tf-module-") and (d / ".git").is_dir()
    ]
    # module name = dir name without tf-module- prefix
    module_names = {d.name.replace("tf-module-", "", 1): d for d in module_dirs}
    if not module_names:
        return []

    # deps[name] = set of internal module names that 'name' depends on
    deps: dict[str, set[str]] = {}
    for name, dir_path in module_names.items():
        deps[name] = get_internal_deps(dir_path, set(module_names.keys()))

    # Topological sort: process B before A if A depends on B
    order: list[str] = []
    remaining = set(module_names.keys())
    while remaining:
        ready = [
            m
            for m in remaining
            if deps.get(m, set()) <= set(order)
        ]
        if not ready:
            # Cyclic or missing dep; add remaining in arbitrary order
            ready = list(remaining)
        order.extend(ready)
        remaining -= set(ready)

    return order


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: upgrade-all-with-deps.py <tf_modules_root> [report_path] [--dry-run]", file=sys.stderr)
        return 1

    tf_modules_root = Path(sys.argv[1]).resolve()
    if not tf_modules_root.is_dir():
        print(f"Not a directory: {tf_modules_root}", file=sys.stderr)
        return 1

    # Only allow tf-modules root: directory must contain tf-module-* git repos.
    # This script does commit/tag/push per module; normal repos (org-gitops, pacha/ops)
    # must use upgrade-providers.py only (no commit/tag/push).
    module_dirs_check = [
        d for d in tf_modules_root.iterdir()
        if d.is_dir() and d.name.startswith("tf-module-") and (d / ".git").is_dir()
    ]
    if not module_dirs_check:
        print(
            "This script is only for the tf-modules root (directory containing tf-module-* git repos).\n"
            "It runs commit/tag/push per module. For normal Terraform projects (e.g. org-gitops, pacha/ops)\n"
            "use upgrade-providers.py instead; that only updates providers, init and validate (no commit/tag/push).",
            file=sys.stderr,
        )
        return 1

    report_path: Path | None = None
    dry_run = False
    for i in range(2, len(sys.argv)):
        if sys.argv[i] == "--dry-run":
            dry_run = True
        elif not sys.argv[i].startswith("-"):
            report_path = Path(sys.argv[i]).resolve()

    script_dir = Path(__file__).resolve().parent
    upgrade_script = script_dir / "upgrade-providers.py"
    # Same skills dir: .../terraform-provider-upgrade/scripts -> .../terraform-version-commit/scripts
    skills_parent = script_dir.parent.parent  # .../skills
    version_commit_script = skills_parent / "terraform-version-commit" / "scripts" / "version-commit.py"
    if not upgrade_script.exists():
        print(f"Missing upgrade script: {upgrade_script}", file=sys.stderr)
        return 1
    if not version_commit_script.exists():
        print(f"Missing version-commit script: {version_commit_script}", file=sys.stderr)
        return 1

    order = dependency_order(tf_modules_root)
    print("Dependency order (dependencies first):")
    for i, name in enumerate(order, 1):
        print(f"  {i}. tf-module-{name}")

    if dry_run:
        print("--dry-run: not running upgrade or version-commit.")
        return 0

    report_arg = [str(report_path)] if report_path else []
    failed: list[str] = []

    for name in order:
        module_dir = tf_modules_root / f"tf-module-{name}"
        if not module_dir.is_dir():
            continue
        print(f"\n========== tf-module-{name} ==========")

        # 1. Upgrade providers + internal module refs
        cmd = [sys.executable, str(upgrade_script), str(module_dir)] + report_arg + [str(tf_modules_root)]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"Upgrade failed for tf-module-{name}, skipping version-commit.", file=sys.stderr)
            failed.append(name)
            continue

        # 2. Commit, tag, push (version-commit --yes pushes by default)
        cmd = [sys.executable, str(version_commit_script), "--yes", str(module_dir)]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"Version-commit failed for tf-module-{name}.", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"\nFailed modules: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\nAll modules processed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
