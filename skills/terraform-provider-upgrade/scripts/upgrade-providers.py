#!/usr/bin/env python3
"""
Terraform Provider Upgrade Script.
Finds required_providers in .tf files, fetches latest versions from Registry API,
updates version constraints. Optionally updates internal wildbit module references
from sibling repos' latest tags. Runs terraform init and validate.
"""
from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

REGISTRY_URL = "https://registry.terraform.io/v1/providers"
WILDBIT_SOURCE_PREFIX = "app.terraform.io/wildbit/"


def _ssl_context():
    """Use certifi CA bundle if available (fixes SSL_CERTIFICATE_VERIFY_FAILED on macOS)."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except ImportError:
        return ssl.create_default_context()


def get_latest_version(namespace: str, ptype: str) -> str | None:
    """Fetch latest provider version from Terraform Registry API."""
    url = f"{REGISTRY_URL}/{namespace}/{ptype}/versions"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"Warning: could not fetch {namespace}/{ptype}: {e}", file=sys.stderr)
        return None
    versions = data.get("versions", [])
    if not versions:
        return None

    # Sort by semver (simple: split and compare)
    def key(v):
        parts = v["version"].replace("-", ".").split(".")
        return tuple(int(x) if x.isdigit() else 0 for x in parts[:3])

    sorted_vers = sorted(versions, key=key)
    return sorted_vers[-1]["version"]


def parse_providers(content: str) -> list[tuple[str, str, str, int]]:
    """Extract (key, source, current_version, start_pos) from required_providers."""
    results = []
    # Match: key = { source = "ns/type" ... version = "x.y" } (comma optional, newlines ok)
    pattern = re.compile(
        r'(\w+)\s*=\s*\{[^}]*?source\s*=\s*"([^"]+)"[^}]*?version\s*=\s*"([^"]+)"',
        re.DOTALL,
    )
    for m in pattern.finditer(content):
        key, source, ver = m.groups()
        if "/" in source:
            results.append((key, source, ver, m.start()))
    return results


def update_provider_versions(content: str) -> str:
    """Update version constraints to latest in content."""
    providers = parse_providers(content)
    if not providers:
        return content

    seen = set()
    for key, source, old_ver, _ in providers:
        if (key, source) in seen:
            continue
        seen.add((key, source))
        ns, _, ptype = source.partition("/")
        latest = get_latest_version(ns, ptype)
        if not latest:
            continue
        parts = latest.split(".")
        new_ver = f"~> {parts[0]}.{parts[1]}" if len(parts) >= 2 else f"~> {latest}"
        if new_ver == old_ver:
            continue
        # Replace this provider's version (match key + block until version value)
        sub_pattern = re.compile(
            rf'({re.escape(key)}\s*=\s*\{{[^}}]*?version\s*=\s*)"[^"]*"',
            re.DOTALL,
        )
        content = sub_pattern.sub(rf'\g<1>"{new_ver}"', content, count=1)
    return content


def get_internal_module_versions(tf_modules_root: Path) -> dict[str, str]:
    """
    For each tf-module-* under tf_modules_root, get latest git tag (after fetching
    from remote) and return module_name -> "~> X.Y" so the latest released minor
    is strictly specified (e.g. tag v3.1.1 -> "~> 3.1", replacing any existing
    "~> 3.0" so Terraform and editors use the latest).
    Module name is derived from dir name: tf-module-gcp-kms -> gcp-kms.
    """
    result: dict[str, str] = {}
    for child in tf_modules_root.iterdir():
        if not child.is_dir() or not child.name.startswith("tf-module-"):
            continue
        # tf-module-gcp-kms -> gcp-kms
        module_name = child.name.replace("tf-module-", "", 1)
        git_dir = child / ".git"
        if not git_dir.is_dir():
            continue
        try:
            # Fetch tags from remote so we use the latest released version
            subprocess.run(
                ["git", "fetch", "origin", "--tags", "--prune"],
                cwd=child,
                capture_output=True,
                timeout=15,
            )
            r = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=child,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode != 0 or not r.stdout:
                continue
            tag = r.stdout.strip()
            # v3.1.1 or 3.1.1 -> ~> 3.1 (strictly specify latest minor)
            m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag)
            if m:
                result[module_name] = f"~> {m.group(1)}.{m.group(2)}"
        except (subprocess.TimeoutExpired, Exception):
            continue
    return result


def update_internal_module_versions(content: str, internal_versions: dict[str, str]) -> str:
    """
    Replace version in module blocks that use app.terraform.io/wildbit/XXX/module
    with the constraint from internal_versions (e.g. ~> 0.5).
    """
    if not internal_versions:
        return content
    # Match: source = "app.terraform.io/wildbit/NAME/module" then optional version = "..."
    pattern = re.compile(
        r'(source\s*=\s*"app\.terraform\.io/wildbit/([^/]+)/module")(\s+version\s*=\s*"[^"]*")?',
        re.DOTALL,
    )

    def repl(m: re.Match) -> str:
        name = m.group(2)
        new_ver = internal_versions.get(name)
        if not new_ver:
            return m.group(0)
        # Preserve newline before version (HCL requires newline after each argument)
        if m.group(3):
            return m.group(1) + "\n  " + f'version = "{new_ver}"'
        return m.group(1) + f'\n  version = "{new_ver}"'

    return pattern.sub(repl, content)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    root = root.resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    # Optional: report_path (argv[2]), modules_root (argv[3])
    report_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    modules_root: Path | None = None
    if len(sys.argv) > 3:
        modules_root = Path(sys.argv[3]).resolve()
        if not modules_root.is_dir():
            modules_root = None
    # If modules_root not passed but root is inside a tf-modules tree, infer it
    if modules_root is None and "tf-modules" in root.parts:
        idx = list(root.parts).index("tf-modules")
        candidate = Path(*root.parts[: idx + 1])
        if (candidate / "tf-module-atlas-cluster").is_dir() or any(
                candidate.joinpath(d).is_dir() for d in ["tf-module-gcp-kms", "tf-module-gcp-waf"]
        ):
            modules_root = candidate

    internal_versions: dict[str, str] = {}
    if modules_root:
        internal_versions = get_internal_module_versions(modules_root)
        if internal_versions:
            print(f"Internal module versions (from {modules_root}): {internal_versions}")

    tf_files = list(root.rglob("*.tf"))
    updated = False
    for tf in tf_files:
        content = tf.read_text(encoding="utf-8", errors="replace")
        new_content = update_provider_versions(content)
        if internal_versions:
            new_content = update_internal_module_versions(new_content, internal_versions)
        if new_content != content:
            tf.write_text(new_content, encoding="utf-8")
            print(f"Updated: {tf.relative_to(root)}")
            updated = True

    if not updated:
        print("No provider or internal module version updates needed.")

    # terraform init -upgrade
    print("Running terraform init -upgrade...")
    r = subprocess.run(
        ["terraform", "init", "-upgrade"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return 1

    # terraform validate
    print("Running terraform validate...")
    r = subprocess.run(["terraform", "validate"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        stderr = r.stderr or ""
        # Error in a dependency (submodule from registry) not yet updated → skip and report
        if ".terraform/modules" in stderr:
            print("SKIPPED - pending upgrade of submodules", file=sys.stderr)
            report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
            if report_path:
                report_path = Path(report_path).resolve()
                line = f"{root.name}\tpending upgrade of submodules (validate failed in .terraform/modules)\n"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.open("a", encoding="utf-8").write(line)
            return 0
        print(stderr, file=sys.stderr)
        return 1
    print("Validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
