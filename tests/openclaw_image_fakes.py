"""A fake `openclaw` for the image-generation tests (ADR-0011): a scratch openclaw.json plus a recorder.

`FakeOC` is installed in place of the cockpit's `_openclaw`. `config patch` is simulated with OpenClaw's documented
merge rules (objects merge, arrays and scalars replace, null deletes); a dry run changes nothing. Nothing here starts a
process, and the config file lives under pytest's tmp_path."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable


def merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        elif isinstance(v, dict):
            dst[k] = merge({}, v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


class FakeOC:
    def __init__(self, cfg, doc: dict | None = None):
        self.cfg = cfg
        self.write(doc or {})
        self.calls: list[tuple[list, str | None]] = []
        self.reject_dry = False
        self.reject_live = False
        self.on_dry_run: Callable[[], None] | None = None     # runs on each dry run (simulates a concurrent edit)
        self.extra: dict[tuple, Callable[..., tuple[int, str]]] = {}

    def write(self, doc: dict) -> None:
        self.cfg.write_text(json.dumps(doc), encoding="utf-8")

    def doc(self) -> dict:
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def __call__(self, args, stdin: str | None = None, timeout: int = 120):
        args = list(args)
        self.calls.append((args, stdin))
        for prefix, fn in self.extra.items():
            if tuple(args[:len(prefix)]) == prefix:
                return fn(args, stdin)
        if args[:2] == ["config", "file"]:
            return 0, str(self.cfg)
        if args[:2] == ["config", "patch"]:
            patch = json.loads(stdin or "{}")
            if "--dry-run" in args:
                if self.on_dry_run:
                    self.on_dry_run()
                return (1, "schema: rejected") if self.reject_dry else (0, "dry run ok")
            if self.reject_live:
                return 1, "rejected live"
            doc = self.doc()
            merge(doc, patch)
            self.write(doc)
            return 0, "patched"
        if args[:1] == ["health"]:
            return 0, "ok"
        return 0, ""

    # --- what the tests ask -------------------------------------------------------------------------------------------
    def patches(self, *, dry: bool | None = None) -> list[tuple[list, dict]]:
        out = []
        for args, stdin in self.calls:
            if args[:2] == ["config", "patch"]:
                is_dry = "--dry-run" in args
                if dry is None or dry == is_dry:
                    out.append((args, json.loads(stdin or "{}")))
        return out

    def live_patches(self):
        return self.patches(dry=False)

    def argvs(self) -> list[list]:
        return [a for a, _s in self.calls]
