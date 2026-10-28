"""Retired components: what `ai-resources setup` removes when a component is no longer installed or used.

A component the kit used to wire (OpenRouter, LiteLLM, Telegram) leaves artifacts behind when the operator stops
using it: an auth profile, a plugin entry, a key in the env file, bindings, an owner id, a token file. Left in place
they dangle (a SecretRef that resolves to nothing, a plugin that loads for nobody) and a later setup or `doctor` run
can even bring a stub back. This module is the one declarative list of those components and the reconciler that
removes their artifacts.

Rules, all enforced here:
- A component is removed only when it is NOT in use: each entry has an `in_use` guard over the live config and the
  setup state (a model reference, a credential, a deployment). A guard that cannot tell keeps the component.
- `plan()` is pure: it reads, it never writes. `apply()` with dry_run only reports. Running either twice is a no-op.
- Every change is backed up first, under `<config>/ai-resources/backups/retired-<stamp>/` (0600): the removed config
  fragments as JSON, the removed env lines, and a retired file is MOVED there, not deleted.
- openclaw.json is only ever changed through the injected `apply_patch` (`openclaw config patch`), and only the
  leaves named in RULES; `assert_patch_safe` refuses anything else.
- Root `bindings[]` catch-all entries (no `match.peer`) stay LAST whenever the array is rewritten.
- Nothing here stops, restarts or reloads the gateway: a leaf that needs a restart is reported as pending.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# --- the declarative list -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Retired:
    """One retired component and every place it leaves something behind."""
    id: str
    label: str
    config_delete: tuple[tuple[str, ...], ...] = ()      # openclaw.json leaves removed whole (null in a patch)
    binding_channels: tuple[str, ...] = ()               # bindings[] entries whose match.channel is one of these
    owner_prefixes: tuple[str, ...] = ()                 # commands.ownerAllowFrom entries "<prefix>:<id>"
    env_keys: tuple[str, ...] = ()                       # KEY lines removed from the kit-owned env files
    files: tuple[str, ...] = ()                          # paths relative to $HOME, moved into the backup
    state_fields: tuple[str, ...] = ()                   # what the setup-state normaliser fixes (see _normalise_state)


RETIRED: tuple[Retired, ...] = (
    Retired(
        id="openrouter", label="OpenRouter",
        config_delete=(("auth", "profiles", "openrouter:default"), ("plugins", "entries", "openrouter")),
        env_keys=("OPENROUTER_API_KEY",)),
    Retired(
        id="litellm", label="LiteLLM",
        config_delete=(("plugins", "entries", "litellm"), ("auth", "profiles", "litellm:default")),
        env_keys=("LITELLM_MASTER_KEY",),
        state_fields=("backend",)),
    Retired(
        id="telegram", label="Telegram",
        config_delete=(("channels", "telegram"),),
        binding_channels=("telegram",),
        owner_prefixes=("telegram",),
        env_keys=("OPENCLAW_OWNER_TELEGRAM_ID",),
        files=(".openclaw/telegram.token",)),
)

# Env files the kit owns. ~/.openclaw/.env belongs to OpenClaw and is never edited here.
def _env_files(home: Path) -> list[Path]:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [cfg / "ai-resources" / ".env", home / ".openclaw" / "kit-host.env"]


# --- the in-use guards (True keeps the component) -------------------------------------------------------------

def _text(doc: Any) -> str:
    try:
        return json.dumps(doc, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _model_refs(doc: dict, prefix: str) -> bool:
    return f'"{prefix}/' in _text(doc.get("agents")) or f'"{prefix}/' in _text(doc.get("models"))


def in_use(component: Retired, doc: dict, s: Any, home: Path) -> bool:
    mode, backend = getattr(s, "mode", ""), getattr(s, "backend", "")
    if component.id == "openrouter":
        return (mode == "multi-model" and backend == "openrouter") or _model_refs(doc, "openrouter")
    if component.id == "litellm":
        lite = getattr(s, "litellm", None)
        deployed = getattr(lite, "deployment", "") in ("local", "remote")
        providers = (doc.get("models") or {}).get("providers") or {}
        return (mode == "multi-model" and backend == "litellm") or deployed or "litellm" in providers \
            or _model_refs(doc, "litellm")
    if component.id == "telegram":
        tg = (doc.get("channels") or {}).get("telegram")
        if isinstance(tg, dict) and set(tg) - {"enabled", "streaming"}:
            return True                        # a token, a token file, groups or accounts: someone configured it
        o = getattr(s, "openclaw", None)
        if any(k == "telegram" or k.startswith("telegram:") for k in (getattr(o, "channels_wanted", None) or [])):
            return True
        # A bot token the stub would pick up from the environment.
        for path in (home / ".openclaw" / ".env",):
            try:
                if re.search(r"^TELEGRAM_BOT_TOKEN=", path.read_text(encoding="utf-8"), re.M):
                    return True
            except OSError:
                pass
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    return True                                 # an unknown component is never removed


# --- the plan -------------------------------------------------------------------------------------------------

@dataclass
class Action:
    component: str
    kind: str          # config | bindings | owner | env | file | state | reorder
    target: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.component}: {self.kind} {self.target}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    patch: dict = field(default_factory=dict)
    replace_paths: list[str] = field(default_factory=list)
    removed_config: dict = field(default_factory=dict)       # dotted path -> removed fragment (for the backup)
    env: dict[Path, list[str]] = field(default_factory=dict)  # file -> keys present
    files: list[Path] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)            # components skipped because they are in use
    state_changes: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.actions


def _get(doc: Any, path: Iterable[str]) -> Any:
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _nest(path: tuple[str, ...], value: Any) -> dict:
    out: Any = value
    for key in reversed(path):
        out = {key: out}
    return out


def _merge(into: dict, other: dict) -> None:
    for k, v in other.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = v


def is_catch_all(entry: Any) -> bool:
    return isinstance(entry, dict) and not ((entry.get("match") or {}).get("peer"))


def order_catch_all_last(bindings: list) -> list:
    """Stable: every peer route keeps its place, every catch-all moves after them (a catch-all earlier shadows them)."""
    return [b for b in bindings if not is_catch_all(b)] + [b for b in bindings if is_catch_all(b)]


def _env_has(path: Path, key: str) -> bool:
    try:
        return re.search(rf"^\s*{re.escape(key)}=", path.read_text(encoding="utf-8"), re.M) is not None
    except OSError:
        return False


def plan(doc: dict, s: Any, *, home: Path | None = None, components: Iterable[Retired] = RETIRED) -> Plan:
    home = home or Path.home()
    out = Plan()
    bindings = doc.get("bindings") if isinstance(doc.get("bindings"), list) else None
    new_bindings = list(bindings) if bindings is not None else None
    owners = _get(doc, ("commands", "ownerAllowFrom"))
    new_owners = list(owners) if isinstance(owners, list) else None
    for comp in components:
        if in_use(comp, doc, s, home):
            out.kept.append(comp.id)
            continue
        for path in comp.config_delete:
            frag = _get(doc, path)
            if frag is None:
                continue
            out.actions.append(Action(comp.id, "config", ".".join(path)))
            out.removed_config[".".join(path)] = frag
            _merge(out.patch, _nest(path, None))
        if new_bindings is not None and comp.binding_channels:
            drop = [b for b in new_bindings if isinstance(b, dict)
                    and (b.get("match") or {}).get("channel") in comp.binding_channels]
            if drop:
                out.actions.append(Action(comp.id, "bindings", f"{len(drop)} entr{'y' if len(drop) == 1 else 'ies'}"))
                out.removed_config.setdefault("bindings", []).extend(drop)
                new_bindings = [b for b in new_bindings if b not in drop]
        if new_owners is not None and comp.owner_prefixes:
            drop_o = [o for o in new_owners if isinstance(o, str) and o.split(":", 1)[0] in comp.owner_prefixes
                      and ":" in o]
            if drop_o:
                out.actions.append(Action(comp.id, "owner", "commands.ownerAllowFrom", f"{len(drop_o)} entry"))
                out.removed_config.setdefault("commands.ownerAllowFrom", []).extend(drop_o)
                new_owners = [o for o in new_owners if o not in drop_o]
        for file in _env_files(home):
            present = [k for k in comp.env_keys if _env_has(file, k)]
            if present:
                out.env.setdefault(file, []).extend(present)
                out.actions.append(Action(comp.id, "env", str(file), ", ".join(present)))
        for rel in comp.files:
            p = home / rel
            if p.is_file() and not p.is_symlink():
                out.files.append(p)
                out.actions.append(Action(comp.id, "file", str(p), "moved into the backup"))
        if "backend" in comp.state_fields and comp.id == "litellm":
            if getattr(s, "mode", "") != "multi-model" and getattr(s, "backend", "") == "litellm":
                out.state_changes["backend"] = "cli"
                out.actions.append(Action(comp.id, "state", "setup-state backend", "litellm -> cli (no gateway)"))
    if new_bindings is not None:
        ordered = order_catch_all_last(new_bindings)
        if ordered != bindings:
            if len(ordered) == len(bindings or []):
                out.actions.append(Action("bindings", "reorder", "bindings[]", "catch-all entries moved last"))
            out.patch["bindings"] = ordered
            out.replace_paths.append("bindings")
    if new_owners is not None and new_owners != owners:
        _merge(out.patch, {"commands": {"ownerAllowFrom": new_owners}})
        out.replace_paths.append("commands.ownerAllowFrom")
    return out


# --- the guard on what may be sent ----------------------------------------------------------------------------

def assert_patch_safe(patch: dict, replace_paths: list[str] | None, components: Iterable[Retired] = RETIRED) -> None:
    """The retired writer may send only: a null at a leaf named in RULES, `bindings` (replaced, no telegram route, catch-all
    last) and `commands.ownerAllowFrom` (replaced). Anything else is refused before it reaches `config patch`."""
    allowed_nulls = {path for c in components for path in c.config_delete}
    allowed_replace = {"bindings", "commands.ownerAllowFrom"}
    for rp in replace_paths or []:
        if rp not in allowed_replace:
            raise ValueError(f"the retired-components writer may only replace {sorted(allowed_replace)}; refused: {rp}")

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if path == ("bindings",):
            if not isinstance(node, list) or order_catch_all_last(node) != node:
                raise ValueError("bindings[] must keep its catch-all entries last")
            return
        if path == ("commands", "ownerAllowFrom"):
            if not isinstance(node, list):
                raise ValueError("commands.ownerAllowFrom must stay a list")
            return
        if node is None:
            if path not in allowed_nulls:
                raise ValueError("the retired-components writer may not delete " + ".".join(path))
            return
        if not isinstance(node, dict):
            raise ValueError("the retired-components writer may not set " + ".".join(path))
        for k, v in node.items():
            walk(v, path + (k,))

    walk(patch, ())


# --- backup and apply -----------------------------------------------------------------------------------------

def backup_dir(home: Path | None = None, stamp: str | None = None) -> Path:
    home = home or Path.home()
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return cfg / "ai-resources" / "backups" / ("retired-" + (stamp or time.strftime("%Y%m%d-%H%M%S")))


def _write_private(path: Path, text: str) -> None:
    from .openclaw_host import write_file_atomically
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomically(path, text, mode=0o600)


def _strip_env_keys(file: Path, keys: list[str]) -> tuple[str, list[str]]:
    """(new text, the removed lines)."""
    original = file.read_text(encoding="utf-8")
    kept, removed = [], []
    for line in original.splitlines():
        key = line.partition("=")[0].strip()
        if "=" in line and key in keys and not line.lstrip().startswith("#"):
            removed.append(line)
        else:
            kept.append(line)
    return ("\n".join(kept).rstrip("\n") + "\n" if kept else ""), removed


@dataclass
class Result:
    removed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    backup: str = ""
    dry_run: bool = False


def apply(p: Plan, s: Any, *, apply_patch: Callable[..., tuple[bool, str]], dry_run: bool = False,
          home: Path | None = None, stamp: str | None = None, restart_error: type = RuntimeError) -> Result:
    """Carry out `p`. With `dry_run` nothing is written and `removed` lists what would be. Backup first, then the config
    patch, the env lines, the files and the state field. A failed step is reported and never stops the others."""
    res = Result(dry_run=dry_run)
    if p.empty:
        return res
    if dry_run:
        res.removed = [a.line() for a in p.actions]
        return res
    home = home or Path.home()
    bdir = backup_dir(home, stamp)
    res.backup = str(bdir)
    if p.removed_config:
        _write_private(bdir / "openclaw-removed.json", json.dumps(p.removed_config, indent=2, sort_keys=True) + "\n")
    config_actions = [a for a in p.actions if a.kind in ("config", "bindings", "owner", "reorder")]
    if p.patch:
        assert_patch_safe(p.patch, p.replace_paths)
        ok, out = apply_patch(p.patch, dry_run=True, replace_paths=p.replace_paths)
        if not ok:
            res.failed.append(f"config patch dry run rejected the change: {out[-200:]}")
        else:
            try:
                ok, out = apply_patch(p.patch, dry_run=False, replace_paths=p.replace_paths)
            except restart_error as e:
                res.pending += [f"{a.line()} (needs a gateway restart: {e})" for a in config_actions]
            else:
                if ok:
                    res.removed += [a.line() for a in config_actions]
                else:
                    res.failed.append(f"config patch failed: {out[-200:]}")
    for file, keys in p.env.items():
        try:
            text, removed = _strip_env_keys(file, keys)
            if removed:
                # The key names are kept; the values (a credential) are not copied anywhere.
                _write_private(bdir / "env" / f"{file.parent.name}-{file.name}.removed-keys", "\n".join(
                    re.sub(r"=.*", "", ln) for ln in removed) + "\n")
                if file.name == "kit-host.env":
                    _write_host_env(file, keys)
                else:
                    _write_private(file, text)
                res.removed += [a.line() for a in p.actions if a.kind == "env" and a.target == str(file)]
        except OSError as e:
            res.failed.append(f"{file}: {e.strerror or e}")
    for path in p.files:
        try:
            dest = bdir / "files" / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(dest.parent, 0o700)
            os.replace(path, dest)
            os.chmod(dest, 0o600)
            res.removed.append(f"file {path} (moved to {dest})")
        except OSError as e:
            res.failed.append(f"{path}: {e.strerror or e}")
    for attr, value in p.state_changes.items():
        setattr(s, attr, value)
        res.removed += [a.line() for a in p.actions if a.kind == "state"]
    return res


def _write_host_env(file: Path, keys: list[str]) -> None:
    from . import openclaw_host
    openclaw_host.write_host_env({k: None for k in keys}, path=file)


def report(res: Result) -> list[str]:
    """Plain lines for the setup output."""
    lines = [("Would remove: " if res.dry_run else "Removed: ") + r for r in res.removed]
    lines += ["Pending (not applied): " + r for r in res.pending]
    lines += ["Failed: " + r for r in res.failed]
    if res.backup and res.removed and not res.dry_run:
        lines.append(f"Backup of what was removed: {res.backup}")
    return lines
