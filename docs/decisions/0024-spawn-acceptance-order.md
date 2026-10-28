# ADR-0024 — Spawn acceptance proves the config first and trusts only sessions newer than it

Status: accepted (3.2.9)

## Context

`spawn.landed-on-role-agent` read the newest spawned session of each kit-role agent, newest by `updatedAt`, and failed when its
model was outside the role's chain. On 2026-10-28 it failed for `kit-role-explore`: the row was a Sonnet spawn started at 13:21,
before 3.2.4 pinned `subagents.model` (14:32); a later write to it (17:03) made it outrank the Haiku spawn of 14:41. Every live
agent already resolved to its own model (OpenClaw order: target `subagents.model`, `agents.defaults.subagents.model`, target
primary), so nothing was wrong on the host.

## Decision

1. The case first resolves the spawn model from the live config in OpenClaw's order; it must be in the role's chain. This needs no
   history and is the primary proof.
2. Session evidence is the newest session by `sessionStartedAt`. A session that started before the live config last changed is
   reported as unproven (it says nothing about today's config); one that started after it must match the chain.
3. The setup note for an agent whose model the operator owns is silent when nothing differs and otherwise carries the live and matrix
   values, the spawn model and the commands to follow the matrix.

## Consequences

A stale spawn can no longer fail the case; a real regression (a default outranking a role, or a new spawn on a wrong model) still does.
`updatedAt` is never used to order sessions.
