---
name: cloudflare-cf-cli
description: Use Cloudflare's `cf` CLI instead of raw Cloudflare API calls (curl, fetch) or Wrangler for any Cloudflare account/zone/Workers/DNS task. Cuts agent token usage via condensed JSON output and command search.
triggers: "cloudflare, wrangler, cf cli, workers, cf deploy, cf dev, dns record, zone, cloudflare api, CLOUDFLARE_API_TOKEN"
---

# Cloudflare `cf` CLI

`cf` (installed globally via `npm i -g cf`) is Cloudflare's agent-first CLI. It covers the entire Cloudflare API (~3,000
operations, vs. Wrangler's ~280) and defaults to condensed JSON for agents —
this is the reason to prefer it over raw `curl` against the Cloudflare API or
Wrangler: it saves context/tokens on every call.

## Rules

- **REQUIRED:** For any Cloudflare account, zone, DNS, Workers, D1, R2, KV, or
  Access task, use `cf` over `curl https://api.cloudflare.com/...` or Wrangler.
- **REQUIRED:** Discover commands with `cf cli search "<task description>"`
  first — do not chain `cf <guess> --help` calls. It returns five compact JSON
  matches; pick the closest one and run `<command> --help` for full usage.
- **REQUIRED:** Keep `cf cli search` queries anonymous — describe the action
  and resource type only, never names, emails, domains, account/resource IDs,
  or tokens.
- **REQUIRED:** For raw API request shape (method, path, params), swap the
  discovered command's leading `cf` for `cf schema` instead of guessing.
- Output is JSON by default (pretty for humans, condensed for agents) — pipe
  through `jq` only when you need to filter a specific field, not to reformat.
- Auth is per-directory via named profiles: `cf auth create <name>` then
  `cf auth activate <name> [dir]`. `cf auth whoami` checks current status;
  there is no `cf auth status`. Check it before assuming a profile is
  active, and request a scoped API token via `secrets` before running `cf auth create`,
  never reuse a production token pulled from a project's `.env`/vars file.
- This repo's Cloudflare zone/DNS records are managed as IaC via Crossplane +
  the Cloudflare provider (`apps/wildbit/cloudflare/projects.yaml`,
  `infrastructure/cloudflare/compositions/project.yaml`) — `cf` is for
  interactive/ad-hoc account and Workers work, not a replacement for that IaC
  flow. Don't use `cf` to hand-edit DNS records already declared there.
