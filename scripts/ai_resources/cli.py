"""Main CLI dispatcher for ai-resources."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .generate import cmd_generate
from . import daemon, doctor, executors_cmd, audit, models_cmd, openclaw_host, verify, lanes_cmd
from .setup import wizard


_EPILOG = """Typical flows:
  Initial setup (interactive wizard):
    ai-resources setup

  After updating the kit:
    brew upgrade ai-resources
    ai-resources generate

  Health check:
    ai-resources doctor

  Check, read-only, what setup left behind:
    ai-resources verify

  Show role → model map:
    ai-resources executors show

  Newer models for the OpenClaw host and your gateways (read-only check, then apply):
    ai-resources models status
    ai-resources models check --refresh
    ai-resources models update --dry-run
    ai-resources models update            # on a terminal: shows what was found and asks
    ai-resources models approve --slot google:gemini-flash <id>

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
    p_st.add_argument("--yes", action="store_true",
                      help="Answer yes to the offer to install missing tools (claude, agy, openclaw). "
                           "Without it that question defaults to No and an unattended run installs nothing")
    p_st.add_argument("--dry-run", action="store_true",
                      help="Preview generated configs + test gateway without writing anything")
    p_st.add_argument("--shape", default="", choices=["", "single", "single-provider", "multi-provider"],
                      help="Model selection shape (with --models): one model, one provider, several providers")
    p_st.add_argument("--providers", default="", help="Comma-separated providers the models come from")
    p_st.add_argument("--models", default="",
                      help="Comma-separated models: provider/id, provider:family, or a family or id")
    p_st.add_argument("--smoke-path", dest="smoke_path", default="",
                      choices=["", "claude-cli", "litellm", "openrouter", "direct"],
                      help="How `models update` probes a new model")
    p_st.add_argument("--backend", default="", choices=["", "litellm", "openrouter"],
                      help="Gateway for multi-model (litellm or openrouter); omitted: keep the saved mode")
    p_st.add_argument("--cockpits", default="",
                      help="Comma-separated cockpits to configure (fails if the matrix cannot serve the models)")
    p_st.add_argument("--allow-unverified", dest="allow_unverified", action="store_true",
                      help="Also configure compatibility cells that are not verified end to end (labelled)")
    p_st.add_argument("--compose", default="", choices=["", "uniform", "single-provider", "mixed"],
                      help="Propose a team from the scores database (ADR-0006): one model, one provider, or "
                           "balanced across providers (mixed needs --allow-unverified and is always marked)")
    p_st.add_argument("--headroom", default="",
                      help="Per-pool static headroom for --compose, pool=ample|normal|scarce,... (1.0/0.7/0.3)")
    p_st.add_argument("--headroom-live", dest="headroom_live", default="",
                      help="Pools whose live headroom may be read now (opt-in, read-only), comma-separated")
    p_st.add_argument("--why-json", dest="why_json", default="",
                      help="Write the composition's why table as JSON to this path (workspace or ~/.cache)")
    p_st.add_argument("--allow-fable", dest="allow_fable", action="store_true",
                      help="Declare a Max or Premium seat, or API billing: Claude Fable 5.1 may be a candidate")
    p_st.set_defaults(func=cmd_setup)

    # version
    p_v = sub.add_parser("version", help="Print the current ai-resources version")
    p_v.set_defaults(func=cmd_version)

    # daemon, doctor, executors, audit
    daemon.add_subparser(sub)
    doctor.add_subparser(sub)
    verify.add_subparser(sub)
    executors_cmd.add_subparser(sub)
    audit.add_subparser(sub)
    openclaw_host.add_subparser(sub)
    models_cmd.add_subparser(sub)
    lanes_cmd.add_subparser(sub)

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
