"""Main CLI dispatcher for ai-resources."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .generate import cmd_generate
from . import daemon, doctor, executors_cmd, audit, models_cmd, openclaw_host, verify, lanes_cmd
from .setup import flow, wizard
from .orchestrator import cli as orchestrator_cli


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


_SETUP_EPILOG = """\
The wizard asks three things in four screens, over 10 internal steps (a header, a progress rail and one line of
key hints each):
  1 Your tools (every coding CLI is a tool: tick the ones to use, tick a missing one to install it)
  2 Sign-in (each ticked tool asks its own: Claude Code subscription, API key or Vertex AI; OpenClaw providers; agy Google account)
  3 Your team (a starting team - Balanced, Performance, Low cost or Custom - and who does what)
  4 Review (Apply, Change tools, Change sign-in, Change team, More options, Cancel), then Apply and Verification
There is no gateway and no global team question (ADR-0015): the old answer is migrated once, shown as old -> new.

Keys:
  Enter   confirm the highlighted answer
  Ctrl-B  go back one step (only in steps that have changed nothing on this machine yet)
  Ctrl-C  cancel; Ctrl-D also cancels. Nothing past the last confirmed step is applied.
Review lists the answers by name (Sign-in, Team, Tools; a key is never shown or stored). A Change jumps
back to that screen and returns to the review; More options holds parallel runs and the OpenClaw questions.

Unattended and preview runs:
  --non-interactive   use saved answers, never prompt (a prompt with no default aborts)
  --dry-run           render the targets and print a masked unified diff; nothing is written
Parallel runs (subscription lanes, ADR-0007, off by default): --lanes enables, --no-lanes clears, --lanes-dry-run
shows the proposed table and writes nothing, --lanes-orchestrator ID names the dispatching agent.

Output: NO_COLOR (set and non-empty) or a non-UTF-8 stream selects ASCII glyphs and no colour.
"""


def cmd_setup(args: argparse.Namespace) -> int:
    if getattr(args, "restore_hygiene", False):
        from .setup import hygiene
        return hygiene.cmd_restore()
    if getattr(args, "restore_agy_mcp", False):
        from .setup import agy_mcp
        return agy_mcp.cmd_restore()
    if getattr(args, "restore_claude_md", False):
        from .setup import claude_md_migrate
        return claude_md_migrate.cmd_restore()
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
        help="Interactive setup wizard: tools, how they work together, team",
        description="Guided setup wizard: your tools, how they should work, your team, then a review.",
        epilog=_SETUP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_st.add_argument("--non-interactive", action="store_true",
                      help="Use saved answers without prompting (for CI)")
    p_st.add_argument("--profile", default="",
                      help="Skip prompts and use this profile")
    p_st.add_argument("--yes", action="store_true",
                      help="Say yes, for this run only, to installing the missing ones of Claude Code, Antigravity (agy) "
                           "and OpenClaw; no other tool is installed by --yes. Without it nothing is installed "
                           "unattended, and the tool list on a terminal offers every missing tool")
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
    p_st.add_argument("--backend", default="", choices=["", "litellm", "openrouter", "cli"],
                      help="Deprecated (ADR-0015): the gateway is no longer offered. Any value is accepted and pre-fills "
                           "Claude Code's subscription sign-in (no gateway, no keys asked) with a deprecation note")
    p_st.add_argument("--cockpits", default="",
                      help="Comma-separated cockpits to configure (pre-fills the tools; fails if the matrix cannot serve "
                           "the models)")
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
    p_st.add_argument("--lanes", action="store_true",
                      help="Enable subscription lanes (ADR-0007): propose the profile's lane table, sign the "
                           "account acknowledgement and write lanes.yaml and the OpenClaw lane agents")
    p_st.add_argument("--no-lanes", dest="no_lanes", action="store_true",
                      help="Clear subscription lanes: remove lanes.yaml and the lane agents the kit created")
    p_st.add_argument("--lanes-antigravity", dest="lanes_antigravity", action="store_true",
                      help="Open the Antigravity lane (ADR-0007 section 11, off by default): sign the Google "
                           "account acknowledgement for the roles listed; every phase 3 gate must also be proven")
    p_st.add_argument("--no-lanes-antigravity", dest="no_lanes_antigravity", action="store_true",
                      help="Close the Antigravity lane: agy agents leave lanes.yaml and the OpenClaw config; "
                           "the claude-cli lanes stay")
    p_st.add_argument("--lanes-dry-run", dest="lanes_dry_run", action="store_true",
                      help="Show the proposed lane table and write nothing")
    p_st.add_argument("--lanes-orchestrator", dest="lanes_orchestrator", action="append", default=[], metavar="ID",
                      help="Agent id that receives the lane ids in subagents.allowAgents (repeatable)")
    p_st.add_argument("--restore-hygiene", dest="restore_hygiene", action="store_true",
                      help="Put back every MCP server and plugin the hygiene step disabled, then exit")
    p_st.add_argument("--restore-claude-md", dest="restore_claude_md", action="store_true",
                      help="Move back into ~/.claude/CLAUDE.md every block the migration moved out, then exit")
    p_st.add_argument("--restore-agy-mcp", dest="restore_agy_mcp", action="store_true",
                      help="Remove from agy's mcp_config.json the servers the kit added (where unchanged), then exit")
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
    orchestrator_cli.add_subparser(sub)

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
    try:
        return int(args.func(args))
    except (flow.Cancelled, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
