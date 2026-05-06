"""ai-resources audit — basic cost report from LiteLLM logs.

Reads the LiteLLM container logs and tallies tokens/cost per model. Without a
LiteLLM database backend this is best-effort: parses recent log lines.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import datetime, timezone

from .setup import litellm, ui


# Match the cost lines LiteLLM emits when verbose logging is on.
COST_RE = re.compile(
    r"model=([\w\-./@]+).*?total_tokens=(\d+).*?cost=([\d.]+)",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"role=(\w[\w\-]*)")


def cmd_audit(args: argparse.Namespace) -> int:
    ui.require_deps()
    logs = litellm.container_logs(tail=args.tail)
    if not logs:
        ui.warn("No logs available — is the LiteLLM container running?")
        return 1

    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})
    by_role: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})

    for line in logs.splitlines():
        m = COST_RE.search(line)
        if not m:
            continue
        model = m.group(1)
        tokens = int(m.group(2))
        cost = float(m.group(3))
        by_model[model]["tokens"] += tokens
        by_model[model]["cost"] += cost
        by_model[model]["calls"] += 1
        rm = TAG_RE.search(line)
        if rm:
            role = rm.group(1)
            by_role[role]["tokens"] += tokens
            by_role[role]["cost"] += cost
            by_role[role]["calls"] += 1

    if not by_model:
        ui.warn("No cost lines found in logs (LiteLLM may not be logging cost details).")
        ui.detail("Enable in litellm.yaml under litellm_settings: { set_verbose: true }")
        return 1

    ui.banner("Cost report (from container logs)",
              subtitle=f"Last {args.tail} log lines")

    rows_model = sorted(by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    ui.role_table(
        [(m, f"{int(v['calls'])} calls", f"${v['cost']:.4f} ({int(v['tokens'])} tok)")
         for m, v in rows_model],
        title="By model",
    )

    if by_role:
        rows_role = sorted(by_role.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        ui.role_table(
            [(r, f"{int(v['calls'])} calls", f"${v['cost']:.4f} ({int(v['tokens'])} tok)")
             for r, v in rows_role],
            title="By role",
        )

    total_cost = sum(v["cost"] for v in by_model.values())
    total_tokens = sum(v["tokens"] for v in by_model.values())
    ui.console().print()
    ui.info(f"Total: ${total_cost:.4f} across {int(total_tokens)} tokens")
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("audit", help="Show cost report from gateway logs")
    p.add_argument("--tail", type=int, default=5000,
                   help="Number of log lines to scan (default: 5000)")
    p.set_defaults(func=cmd_audit)
