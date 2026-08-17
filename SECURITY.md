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

## Supported versions

Only the latest release is supported. The fix ships in a new release; there are no
backports.
