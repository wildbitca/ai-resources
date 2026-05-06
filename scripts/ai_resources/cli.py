"""Main CLI dispatcher for ai-resources."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .generate import cmd_generate
from . import daemon, doctor, executors_cmd, audit
from .setup import wizard


_EPILOG = """Typical flows:
  Initial setup (interactive wizard):
    ai-resources setup

  After updating the kit:
    brew upgrade ai-resources
    ai-resources generate

  Health check:
    ai-resources doctor

  Show role → model map:
    ai-resources executors show

  Daemon (LiteLLM container):
    ai-resources daemon status
    ai-resources daemon logs

Environment:
  AGENT_KIT          Repo root (default: parent of scripts/)
  AGENT_SKILLS_ROOT  Default: $AGENT_KIT/skills
  XDG_CONFIG_HOME    Default: ~/.config (state lives in $XDG_CONFIG_HOME/ai-resources/)
"""


def cmd_setup(args: argparse.Namespace) -> int:
    return wizard.run(args)


def cmd_version(args: argparse.Namespace) -> int:
    print(f"ai-resources {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ai-resources",
        description="Multi-model AI agent kit: skills, workflows, orchestration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    # generate
    p_gen = sub.add_parser(
        "generate",
        help="Import vendor skills + rebuild skills-index.json",
    )
    p_gen.add_argument("--dry-run", action="store_true",
                       help="Print actions without writing")
    p_gen.add_argument("--force", action="store_true",
                       help="Overwrite dirs managed by the configured source")
    p_gen.add_argument("--skip-vendor", action="store_true",
                       help="Only rebuild skills-index.json")
    p_gen.set_defaults(func=cmd_generate)

    # setup (the new wizard)
    p_st = sub.add_parser(
        "setup",
        help="Interactive setup wizard: cockpits, LiteLLM, providers, profiles",
    )
    p_st.add_argument("--non-interactive", action="store_true",
                      help="Use saved answers without prompting (for CI)")
    p_st.add_argument("--profile", default="",
                      help="Skip prompts and use this profile")
    p_st.set_defaults(func=cmd_setup)

    # version
    p_v = sub.add_parser("version", help="Print the current ai-resources version")
    p_v.set_defaults(func=cmd_version)

    # daemon, doctor, executors, audit
    daemon.add_subparser(sub)
    doctor.add_subparser(sub)
    executors_cmd.add_subparser(sub)
    audit.add_subparser(sub)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return 1
    if not hasattr(args, "func"):
        ap.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
