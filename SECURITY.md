# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.** An issue is visible to everyone the
moment it is created, including to whoever would exploit it.

Use GitHub's private vulnerability reporting instead — it is enabled on this repository:

1. Go to the **Security** tab.
2. **Report a vulnerability**.
3. Describe what you found, how to reproduce it, and what an attacker gains.

The report is visible only to the maintainers.

## What matters most here

This repository ships **instructions that other people's coding agents execute**, plus the
shell scripts that install them. That makes two things especially serious, and they are the
reports we most want to receive:

- **Prompt content that induces an agent to do something harmful** — exfiltrate secrets,
  weaken a security control, or run a destructive command — whether by mistake or because
  someone slipped it in.
- **Anything in `scripts/` or the Homebrew formula that executes untrusted input**, writes
  outside its expected paths, or fetches code over an unverified channel.

A finding does not need a working exploit to be worth reporting.

## OpenClaw `antigravity` engine — unrestricted code execution

The `antigravity` OpenClaw engine (`ai-resources setup` → OpenClaw → antigravity) wires a
Telegram bot to unrestricted code execution on the host it runs on. Read this before
enabling it; the wizard requires an explicit, saved acknowledgement of the same risk
before it will apply this engine.

- **What "unrestricted" means:** both the agy orchestrator (`agy-cli` backend) and the
  `claude` worker agent (`claude-kit` backend) run with `--dangerously-skip-permissions`.
  Neither backend carries `--setting-sources user`, so any `.claude/settings.json` hooks
  in a project the `claude` worker agent opens run too — including in a client or
  third-party repo the agent was asked to look at, not just your own.
- **The blast radius:** anyone who reaches the bot — because they were added to
  OpenClaw's `channels.telegram.allowFrom`, or because they took over that Telegram
  account — gets unrestricted code execution as whichever OS user runs the OpenClaw
  gateway. That includes every credential reachable from that account: MCP server
  tokens (ClickUp, Supabase, or whatever else is configured), cloud and Kubernetes
  credentials, SSH keys, and anything else on `$PATH` or in the environment.
- **The allowlist is the only guard.** The kit never patches
  `channels.telegram.allowFrom` itself — that stays entirely in your hands. Treat every
  credential reachable from the gateway's environment as compromised if the allowlist
  or the Telegram account is ever compromised, and rotate accordingly.
- **Cluster-separation, `--context` rules, and similar prompt-level policies are
  enforced only by the prompt** the bot reads (its `AGENTS.md`), not by anything
  technical. A `kubectl` command with no `--context` will run against whatever the
  ambient kubeconfig points at; there is no code-level guard against this.
- **Mitigations the kit applies:** the wizard shows this warning and requires an
  explicit acknowledgement before applying the engine (`ai-resources setup` is
  interactive by default; an unattended `--non-interactive` run with no prior
  acknowledgement skips applying it and prints why); teardown restores the previous
  engine and unlinks the kit's OpenClaw plugin.

## Supported versions

Only the latest release is supported. The fix ships in a new release; there are no
backports.
