"""The S12 artifacts: re-render the files that embed a model id when a slot moves.

`executors.yaml`, `litellm.yaml`, the Claude subagent frontmatter, Claude's settings (the OpenRouter
`ANTHROPIC_DEFAULT_*_MODEL` pins and `modelOverrides`) and `~/.aider.conf.yml` all spell a model id. When
a slot's effective id changes, each of them that the compatibility matrix configures for this host and that
actually embeds the old id is rewritten, through `model_fanout`, so the interactive answer, the timer and a
rollback all treat them as one change:

* `affected()` is true only when the cockpit's matrix action is configure or via_gateway AND the file embeds
  an id of a slot that moves. Single-model Claude aliases (`opus`, `sonnet`) embed no id, so they are never
  rewritten; a slot on a `fixed` track never moves, so its ids are never rewritten either.
* Existing renderers are reused (`claude.render_subagent_files`, `claude._build_settings_patch`,
  `profiles.write_executors`, `litellm.write_configs`); there is no new renderer.
* Every file is backed up next to the openclaw.json backup before it is written (mode 0600), and `restore`
  puts it back byte for byte. The renderers write `os.environ/<VAR>` references only; no key is read or
  written here (the aider key already in the file is left untouched).
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from . import model_fanout, model_pins, model_providers
from .model_fanout import Artifact, ArtifactError, Change, Ctx

# --- id spelling ---------------------------------------------------------------------------------

def spellings(provider: str, model_id: str) -> dict[str, str]:
    """Every way a file may spell `model_id` of `provider`, keyed by spelling style: `bare`,
    `prefix:<p>` for each vendor prefix, and `openrouter` (dotted minor for Claude)."""
    adapter = model_providers.REGISTRY.get(provider)
    out = {"bare": model_id}
    if adapter is None:
        return out
    for prefix in dict.fromkeys(p for p in (provider, adapter.ref_prefix, adapter.litellm_prefix, adapter.openrouter_ns) if p):
        out[f"prefix:{prefix}"] = f"{prefix}/{model_id}"
    if provider == "anthropic":
        out["openrouter"] = model_pins.openrouter_id(model_id)
    return out


def substitution(changes: dict[str, tuple[str, str]]) -> dict[str, str]:
    """old spelling -> new spelling, for every slot that moves (paired by spelling style, never by position)."""
    mapping: dict[str, str] = {}
    for slot, (old, new) in changes.items():
        if old == new:
            continue
        provider = model_pins.slot_provider(slot)
        new_sp = spellings(provider, new)
        for style, a in spellings(provider, old).items():
            b = new_sp.get(style)
            if b and a != b:
                mapping[a] = b
    return mapping


def substitute(obj: Any, mapping: dict[str, str]) -> tuple[Any, bool]:
    """A copy of `obj` with every string equal to a mapping key replaced (whole strings only: the id
    `claude-sonnet-5` must not rewrite `claude-sonnet-5-5`). Returns (copy, changed)."""
    if isinstance(obj, str):
        return (mapping[obj], True) if obj in mapping else (obj, False)
    if isinstance(obj, list):
        pairs = [substitute(x, mapping) for x in obj]
        return [p[0] for p in pairs], any(p[1] for p in pairs)
    if isinstance(obj, dict):
        pairs = {k: substitute(v, mapping) for k, v in obj.items()}
        return {k: p[0] for k, p in pairs.items()}, any(p[1] for p in pairs.values())
    return obj, False


def embeds(obj: Any, mapping: dict[str, str]) -> bool:
    return substitute(obj, mapping)[1]


# --- files ---------------------------------------------------------------------------------------

def backup_file(path: Path, backup_dir: Path | None, art_id: str) -> dict:
    """Copy `path` into `backup_dir` (0600). The payload records where, or that the file was absent."""
    payload: dict = {"path": str(path), "backup": None}
    if backup_dir is None or not path.is_file():
        return payload
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = backup_dir / f"{art_id}-{path.name}"
    shutil.copyfile(path, dest)
    os.chmod(dest, 0o600)
    payload["backup"] = str(dest)
    return payload


def restore_file(payload: dict) -> None:
    path = Path(payload["path"])
    if payload.get("backup"):
        src = Path(payload["backup"])
        if not src.is_file():
            raise ArtifactError(f"the backup {src} is gone; cannot restore {path}")
        shutil.copyfile(src, path)
    elif path.exists():
        path.unlink()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class _FileArtifact(Artifact):
    """One file that embeds ids. Subclasses say how to read and write it."""
    cockpit: str | None = None            # the matrix row that gates it (None: gated by the mode only)

    def __init__(self, path: Path, *, enabled: Callable[[Ctx], bool] = lambda ctx: True):
        self.path, self._enabled = path, enabled

    def _text(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _gated(self, ctx: Ctx) -> bool:
        if not self._enabled(ctx):
            return False
        if self.cockpit is None:
            return True
        action = (ctx.plan or {}).get(self.cockpit)
        return action is not None and action.action in ("configure", "via_gateway")

    def _embeds(self, mapping: dict[str, str]) -> bool:
        raise NotImplementedError

    def affected(self, change: Change, ctx: Ctx) -> bool:
        mapping = substitution(change.slots)
        return bool(mapping) and self.path.is_file() and self._gated(ctx) and self._embeds(mapping)

    def _write(self, mapping: dict[str, str], change: Change, ctx: Ctx) -> None:
        raise NotImplementedError

    def render(self, change: Change, ctx: Ctx) -> dict:
        payload = backup_file(self.path, ctx.extras.get("backup_dir"), self.id)
        try:
            self._write(substitution(change.slots), change, ctx)
        except Exception as e:  # noqa: BLE001 - leave the file as it was, then report
            restore_file(payload)
            raise ArtifactError(f"{self.id}: {e}") from e
        return payload

    def restore(self, payload: dict, ctx: Ctx) -> None:
        restore_file(payload)


class ExecutorsArtifact(_FileArtifact):
    """`~/.config/ai-resources/executors.yaml`: the user's role -> model map. Multi-model only."""
    id = "executors"

    def _doc(self) -> Any:
        import yaml
        return yaml.safe_load(self._text() or "") or {}

    def _embeds(self, mapping):
        return embeds(self._doc(), mapping)

    def _write(self, mapping, change, ctx):
        from .setup import profiles
        new, _ = substitute(self._doc(), mapping)
        profiles.write_executors(new, self.path)


class AiderConfArtifact(_FileArtifact):
    """`~/.aider.conf.yml`: the model, architect-model and weak-model values only; every other key
    (including the gateway key already in the file) is rewritten exactly as it was."""
    id = "aider-conf"
    cockpit = "aider"
    KEYS = ("model", "architect-model", "weak-model")

    def _conf(self) -> dict:
        import yaml
        data = yaml.safe_load(self._text() or "") or {}
        return data if isinstance(data, dict) else {}

    def _embeds(self, mapping):
        conf = self._conf()
        return any(isinstance(conf.get(k), str) and conf[k] in mapping for k in self.KEYS)

    def _write(self, mapping, change, ctx):
        import yaml
        conf = self._conf()
        for k in self.KEYS:
            if isinstance(conf.get(k), str):
                conf[k] = mapping.get(conf[k], conf[k])
        _atomic_write(self.path, yaml.safe_dump(conf, default_flow_style=False, sort_keys=False))


class ClaudeSettingsArtifact(_FileArtifact):
    """`~/.claude/settings.json`: under OpenRouter, the `ANTHROPIC_DEFAULT_*_MODEL` pins and the
    `modelOverrides` map. Nothing else in the file is touched, and no credential is read."""
    id = "claude-settings"
    cockpit = "claude"

    def _doc(self) -> dict:
        try:
            data = json.loads(self._text() or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _model_env(doc: dict) -> dict:
        env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
        return {k: v for k, v in env.items() if k.startswith("ANTHROPIC_DEFAULT_") and k.endswith("_MODEL")}

    def _embeds(self, mapping):
        doc = self._doc()
        return embeds(self._model_env(doc), mapping) or embeds(doc.get("modelOverrides") or {}, mapping)

    def _write(self, mapping, change, ctx):
        doc = self._doc()
        env = doc.setdefault("env", {})
        for k, v in self._model_env(doc).items():
            env[k] = mapping.get(v, v)
        overrides = doc.get("modelOverrides")
        if isinstance(overrides, dict):
            for slot, (old, new) in change.slots.items():
                if model_pins.slot_provider(slot) == "anthropic" and old in overrides:
                    overrides[new] = model_pins.openrouter_id(new)       # the new bare id OpenClaw will launch
        _atomic_write(self.path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


class ClaudeSubagentsArtifact(Artifact):
    """`~/.claude/agents/*.md`: only the files whose `model:` embeds a moved id are rewritten, each backed up."""
    id = "claude-subagents"

    def __init__(self, agents_dir: Path, render: Callable[[dict, str], dict[str, str]],
                 executors: Callable[[], dict], enabled: Callable[[Ctx], bool] = lambda ctx: True):
        self.dir, self._render, self._executors, self._enabled = agents_dir, render, executors, enabled

    def _gated(self, ctx: Ctx) -> bool:
        action = (ctx.plan or {}).get("claude")
        return self._enabled(ctx) and action is not None and action.action in ("configure", "via_gateway")

    @staticmethod
    def _model_of(text: str) -> str:
        for line in text.splitlines()[:12]:
            if line.startswith("model:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _changed(self, mapping: dict[str, str]) -> dict[str, str]:
        """name -> new content, for the files whose `model:` is a moved id and whose rendering differs.
        Compared with the file on disk, so it does not matter whether executors.yaml was rewritten first."""
        executors = self._executors()
        new_exec, _ = substitute(executors, mapping)
        out: dict[str, str] = {}
        for name, text in self._render(new_exec, "new").items():
            path = self.dir / f"{name}.md"
            try:
                disk = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if self._model_of(disk) in mapping and disk != text:
                out[name] = text
        return out

    def affected(self, change: Change, ctx: Ctx) -> bool:
        mapping = substitution(change.slots)
        return bool(mapping) and self._gated(ctx) and bool(self._changed(mapping))

    def render(self, change: Change, ctx: Ctx) -> dict:
        files = self._changed(substitution(change.slots))
        payloads: list[dict] = []
        try:
            for name, text in files.items():
                path = self.dir / f"{name}.md"
                payloads.append(backup_file(path, ctx.extras.get("backup_dir"), f"{self.id}-{name}"))
                _atomic_write(path, text)
        except Exception as e:  # noqa: BLE001
            for p in reversed(payloads):
                restore_file(p)
            raise ArtifactError(f"{self.id}: {e}") from e
        return {"files": payloads}

    def restore(self, payload: dict, ctx: Ctx) -> None:
        for p in reversed(payload.get("files", [])):
            restore_file(p)


class LiteLLMArtifact(_FileArtifact):
    """`litellm.yaml`, re-rendered from the (already updated) executors.yaml, then ONE health-checked LiteLLM
    restart. Not used for OpenRouter (nothing to restart) or a remote gateway (not ours to manage)."""
    id = "litellm"

    def __init__(self, path: Path, *, executors_path: Path, providers: Callable[[], dict],
                 write_configs: Callable[[dict, dict], Any], restart: Callable[[], bool],
                 healthy: Callable[[], bool], running: Callable[[], bool], enabled: Callable[[Ctx], bool]):
        super().__init__(path, enabled=enabled)
        self._executors_path, self._providers, self._write_configs = executors_path, providers, write_configs
        self._restart, self._healthy, self._running = restart, healthy, running

    def _embeds(self, mapping):
        # litellm.yaml embeds the upstream spelling (`gemini/gemini-3.7-flash`) and the alias; either one
        # counts, as a whole token (`claude-sonnet-5` is not inside `claude-sonnet-5-5`).
        text = self._text() or ""
        return any(re.search(rf"(?<![\w.\-]){re.escape(old)}(?![\w.\-])", text) for old in mapping)

    def affected(self, change: Change, ctx: Ctx) -> bool:
        mapping = substitution(change.slots)
        return bool(mapping) and self.path.is_file() and self._gated(ctx) and self._embeds(self._with_upstream(change))

    @staticmethod
    def _with_upstream(change: Change) -> dict[str, str]:
        mapping = dict(substitution(change.slots))
        for slot, (old, new) in change.slots.items():
            adapter = model_providers.REGISTRY.get(model_pins.slot_provider(slot))
            if adapter and adapter.litellm_prefix and old != new:
                mapping[f"{adapter.litellm_prefix}/{old}"] = f"{adapter.litellm_prefix}/{new}"
        return mapping

    def _write(self, mapping, change, ctx):
        import yaml
        executors = yaml.safe_load(self._executors_path.read_text(encoding="utf-8")) or {}
        was_running = self._running()
        extra = [new for slot, (_old, new) in change.slots.items() if model_pins.slot_provider(slot) == "anthropic"]
        self._write_configs(executors, self._providers(), extra_claude_ids=extra)
        if was_running:
            if not self._restart() or not self._healthy():
                raise ArtifactError("LiteLLM did not become healthy with the new model ids")

    def render(self, change: Change, ctx: Ctx) -> dict:
        payload = backup_file(self.path, ctx.extras.get("backup_dir"), self.id)
        payload["restarted"] = False
        try:
            self._write(substitution(change.slots), change, ctx)
            payload["restarted"] = True
        except Exception as e:  # noqa: BLE001 - put the old file back and bring the old config up again
            restore_file({k: payload[k] for k in ("path", "backup")})
            try:
                if self._running():
                    self._restart()
                    self._healthy()
            except Exception:  # noqa: BLE001
                pass
            raise ArtifactError(f"{self.id}: {e}") from e
        return payload

    def restore(self, payload: dict, ctx: Ctx) -> None:
        restore_file({k: payload[k] for k in ("path", "backup")})
        if payload.get("restarted") and self._running():
            if not self._restart() or not self._healthy():
                raise ArtifactError("LiteLLM did not become healthy after restoring litellm.yaml")


def mode_is_multi(ctx: Ctx) -> bool:
    return getattr(ctx.extras.get("state"), "mode", "") == "multi-model"


def build(*, state, selection, detected: list[str], home: Path | None = None) -> list[Artifact]:
    """The artifacts for this host, with the plan table attached through `extras` by the caller."""
    from .setup import litellm, profiles, state as setup_state
    from .setup.cockpits import aider, claude, _shared
    from . import repo_root

    mode, backend = state.mode, state.backend
    multi = lambda ctx: mode == "multi-model"          # noqa: E731
    litellm_ours = lambda ctx: mode == "multi-model" and backend == "litellm" and state.litellm.deployment == "local"   # noqa: E731
    ak_path = str(_shared.stable_kit_root(repo_root()))
    strict = selection is not None

    def executors() -> dict:
        import yaml
        path = setup_state.executors_path()
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}

    def render_subagents(doc: dict, _tag: str) -> dict[str, str]:
        return claude.render_subagent_files(doc, ak_path, mode, strict)

    url = f"http://{state.litellm.local.bind_address}:{state.litellm.local.port}/health/liveliness"
    arts: list[Artifact] = [
        ExecutorsArtifact(setup_state.executors_path(), enabled=multi),
        LiteLLMArtifact(
            setup_state.litellm_path(), executors_path=setup_state.executors_path(),
            providers=lambda: {k: dataclasses.asdict(v) for k, v in state.providers.items()},
            write_configs=litellm.write_configs, restart=litellm.restart_service,
            healthy=lambda: litellm.wait_for_health(url, max_attempts=60, delay=1.0),
            running=lambda: litellm.health_check(url), enabled=litellm_ours),
        ClaudeSubagentsArtifact(claude.AGENTS_DIR, render_subagents, executors, enabled=multi),
        ClaudeSettingsArtifact(claude.SETTINGS_PATH, enabled=lambda ctx: mode == "multi-model" and backend == "openrouter"),
        AiderConfArtifact(aider.CONF_PATH, enabled=multi),
    ]
    return arts
