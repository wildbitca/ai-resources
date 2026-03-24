# Orchestration Guide

This document explains how the ai-resources kit orchestrates AI coding agents through workflows, skill discovery, subagent delegation, and handoff protocols.

## Architecture Overview

The kit follows a **router pattern**: a main orchestrator receives user requests, detects the domain and intent, selects a workflow (or discovers skills ad-hoc), and delegates work to specialized subagents through structured phases.

```mermaid
graph TB
    subgraph Input
        U[User Request]
    end

    subgraph Orchestrator["Orchestrator (Main Thread)"]
        PF[Step 0: Preflight<br/>Skills + MCP + Memory]
        DD[Step 1: Domain Detection<br/>specs/PROJECT.md + signals]
        WS[Step 2: Workflow Selection<br/>Match trigger table]
        WT[Step 3: Worktree Setup<br/>Git branch isolation]
        SI[Step 4: Step Iteration<br/>Dispatch subagents]
        CB[Step 5: Conditional Branching<br/>Tests? Security?]
        CO[Step 6: Completion<br/>Merge + cleanup]
    end

    U --> PF
    PF --> DD
    DD --> WS
    WS --> WT
    WT --> SI
    SI --> CB
    CB --> SI
    CB --> CO

    subgraph Subagents
        SA1[Subagent: Phase N]
        SA2[Subagent: Phase N+1]
    end

    SI -->|Task tool| SA1
    SA1 -->|Handoff file| SI
    SI -->|Task tool| SA2
    SA2 -->|Handoff file| SI

    subgraph Resources
        SKI[skills-index.json]
        SM[SKILL.md files]
        WY[Workflow YAML]
        HF[Handoff File]
        PS[Persona / Role]
    end

    PF -.->|reads| SKI
    SI -.->|reads| WY
    SA1 -.->|reads| SM
    SA1 -.->|reads| PS
    SA1 -.->|writes| HF
```

## Workflow Pipelines

### Feature Implementation

The most comprehensive workflow. All others are subsets of this pattern.

```mermaid
graph LR
    R[Research<br/><i>generalPurpose</i>] -->|handoff| PL[Plan<br/><i>planner</i>]
    PL -->|handoff| AR[Architect<br/><i>software-architect</i>]
    AR -->|approved| IM[Implement<br/><i>implementer</i>]
    AR -->|rejected| PL
    IM -->|handoff| TE[Test<br/><i>tester</i>]
    TE -->|pass| RE[Review<br/><i>code-reviewer</i>]
    TE -->|fail| IM
    RE -->|approved| SE[Security<br/><i>security-auditor</i>]
    RE -->|changes needed| IM
    SE -->|pass| VE[Verify<br/><i>verifier</i>]
    SE -->|fail| IM
    VE -->|concern| RE
    VE -->|done| DONE[Merge & Cleanup]

    style R fill:#e1f5fe
    style PL fill:#e8f5e9
    style AR fill:#fff3e0
    style IM fill:#fce4ec
    style TE fill:#f3e5f5
    style RE fill:#e0f2f1
    style SE fill:#fff8e1
    style VE fill:#e8eaf6
```

**Skills loaded per phase:**

| Phase | Subagent | Skills |
|-------|----------|--------|
| Research | generalPurpose | common-best-practices, common-context-optimization |
| Plan | planner | common-product-requirements, common-system-design |
| Architect | software-architect | common-architecture-diagramming, common-system-design |
| Implement | implementer | common-best-practices, common-error-handling |
| Test | tester | common-tdd |
| Review | code-reviewer | common-code-review, common-security-standards |
| Security | security-auditor | common-security-audit, common-security-standards |
| Verify | verifier | common-protocol-enforcement, knowledge-audit |

### Bugfix

Similar to feature but starts with research + exploration instead of planning.

```mermaid
graph LR
    R[Research<br/><i>generalPurpose</i>] -->|handoff| EX[Explore<br/><i>explore</i>]
    EX -->|handoff| IM[Implement<br/><i>implementer</i>]
    IM -->|handoff| TE[Test<br/><i>tester</i>]
    TE -->|pass| SE[Security<br/><i>security-auditor</i>]
    TE -->|fail| IM
    SE -->|pass| VE[Verify<br/><i>verifier</i>]
    SE -->|fail| IM
    VE -->|done| DONE[Merge & Cleanup]

    style R fill:#e1f5fe
    style EX fill:#e8f5e9
    style IM fill:#fce4ec
    style TE fill:#f3e5f5
    style SE fill:#fff8e1
    style VE fill:#e8eaf6
```

### Explore and Plan

Read-only workflow for understanding a codebase before committing to implementation.

```mermaid
graph LR
    EX[Explore<br/><i>generalPurpose</i>] -->|handoff| PL[Plan<br/><i>planner</i>]
    PL --> DONE[Plan delivered to user]

    style EX fill:#e8f5e9
    style PL fill:#e1f5fe
```

### Cross-Domain (Backend + Infra)

For tasks that span application code and infrastructure (e.g., deploy a new service).

```mermaid
graph LR
    AP[App Step<br/><i>implementer</i>] -->|handoff| IN[Infra Step<br/><i>generalPurpose</i>]
    IN -->|handoff| TE[Test<br/><i>tester</i>]
    TE -->|pass| RE[Review<br/><i>code-reviewer</i>]
    TE -->|fail| AP
    RE -->|approved| VE[Verify<br/><i>verifier</i>]
    RE -->|changes needed| AP
    VE -->|done| DONE[Merge & Cleanup]

    style AP fill:#fce4ec
    style IN fill:#fff3e0
    style TE fill:#f3e5f5
    style RE fill:#e0f2f1
    style VE fill:#e8eaf6
```

### Security / DevSecOps Audit

Standalone security assessment pipeline.

```mermaid
graph LR
    RC[Recon<br/><i>security-auditor</i>] -->|handoff| SA[SAST<br/><i>security-auditor</i>]
    SA -->|handoff| DS[Dependency Scan<br/><i>security-auditor</i>]
    DS -->|handoff| SD[Secret Detection<br/><i>security-auditor</i>]
    SD -->|handoff| DP[DAST Probe<br/><i>security-auditor</i>]
    DP -->|handoff| EV[Exploit Validation<br/><i>security-auditor</i>]
    EV -->|handoff| RP[Report<br/><i>security-auditor</i>]
    RP --> DONE[Report delivered]

    style RC fill:#fff8e1
    style SA fill:#fff8e1
    style DS fill:#fff8e1
    style SD fill:#fff8e1
    style DP fill:#fff8e1
    style EV fill:#fff8e1
    style RP fill:#fff8e1
```

### Dart/Flutter Release

Gather release info, generate notes, and verify before publishing.

```mermaid
graph LR
    GA[Gather<br/><i>generalPurpose</i>] -->|handoff| GE[Generate<br/><i>generalPurpose</i>]
    GE -->|handoff| VE[Verify<br/><i>verifier</i>]
    VE --> DONE[Ready to publish]

    style GA fill:#e1f5fe
    style GE fill:#e8f5e9
    style VE fill:#e8eaf6
```

## Skill Discovery Flow

When no workflow matches (ad-hoc tasks), the agent discovers skills dynamically:

```mermaid
graph TB
    T[Current Task] --> IDX[Read skills-index.json<br/>116 skills with triggers]
    IDX --> M{Match description<br/>and triggers?}
    M -->|Yes| LD[Load matching SKILL.md files]
    M -->|No| NM[Proceed without skills]
    LD --> AP[Apply skill instructions<br/>Skill authority > pretraining]
    AP --> EX[Execute task]
    NM --> EX
```

**How matching works:**

Each skill in `skills-index.json` has:
- `description` — what the skill does (natural language)
- `triggers` — keywords and file glob patterns (e.g. `**/*.dart, flutter, bloc, state management`)

The agent compares the user's request and current file context against these fields. Multiple skills can match simultaneously.

## Subagent Delegation

The orchestrator delegates to subagents via the **Task tool**. Each subagent runs in isolation with no chat context — it receives everything through its prompt.

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant S as Subagent
    participant H as Handoff File
    participant SK as SKILL.md

    U->>O: "Add user authentication"
    O->>O: Detect domain, select workflow
    O->>S: Task(subagent_type, prompt)
    Note over O,S: Prompt includes:<br/>workspace path, handoff path,<br/>SKILL.md paths, goal
    S->>SK: Read skill instructions
    S->>S: Execute phase
    S->>H: Write results + status
    S->>O: Return structured result
    O->>O: Read handoff, route next phase
    O->>U: Status update (short)
```

**Every subagent prompt must include:**
1. Workspace root (absolute path)
2. Handoff file path
3. SKILL.md paths to read first
4. Goal and constraints
5. Required return format

**Subagent return format:**
```
## Result
- Status: (success | partial | blocked)
- Executive summary: (1-3 sentences)
- Summary: (max 5 bullet points)
- Handoff: (path to updated handoff file)

## Artifacts
- Files touched: (paths)
- Commands run: (or "none")

## Routing
- Next recommended: (workflow step)
- Blocked: (yes/no)
- Risks: (or "none")
```

## Handoff Protocol

Handoff files are the communication channel between workflow phases. They live at `.agent-output/handoff-<branch>.md` and contain structured fields that each phase reads and updates.

```mermaid
graph TB
    subgraph Phase 1
        S1[Subagent writes:<br/>Research_ref, Domain,<br/>Research_needed, Next]
    end

    subgraph Handoff File
        HF[".agent-output/handoff-feature-auth.md"<br/><br/>Domain: dart-flutter<br/>Research_ref: specs/knowledge/research/...<br/>Plan_ref: .agent-output/planner/...<br/>Architect_approval: yes<br/>Requires_tests: true<br/>Security_critical_feature: no<br/>Code_review_verdict: APPROVED<br/>Next: verifier]
    end

    subgraph Phase 2
        S2[Subagent reads handoff,<br/>continues from Phase 1's output]
    end

    S1 -->|writes| HF
    HF -->|reads| S2
```

**Key handoff fields:**

| Field | Set by | Purpose |
|-------|--------|---------|
| `Domain` | Orchestrator | Detected technology domain |
| `Research_ref` | Research phase | Path to research document |
| `Plan_ref` | Plan phase | Path to plan document |
| `Architect_approval` | Architect | yes/no |
| `Requires_tests` | Planner | Whether test phase runs |
| `Security_critical_feature` | Planner | Whether security audit runs |
| `Code_review_verdict` | Reviewer | APPROVED / REQUIRES_CHANGES |
| `Blocked` | Any phase | true if phase cannot proceed |
| `Return_to_step` | Any phase | Which phase to retry |

**Lifecycle:** Handoff files are created when a workflow starts and **deleted** when the workflow completes successfully.

## Conditional Branching

Workflows support dynamic routing based on handoff fields:

```mermaid
graph TB
    PL[Plan Phase] -->|Requires_tests: true| TE[Test Phase]
    PL -->|Requires_tests: false| RE[Review Phase]
    PL -->|Security_critical_feature: true| SE[Security Phase]
    PL -->|Security_critical_feature: false| VE[Verify Phase]

    TE -->|All tests pass| RE
    TE -->|Tests fail| IM[Implement Phase<br/>retry up to 3x]
    IM --> TE

    RE -->|APPROVED| SE
    RE -->|REQUIRES_CHANGES| IM

    SE -->|PASS| VE
    SE -->|FAIL| IM
```

**Retry policy:** Each phase can retry up to 3 times when blocked. If retries are exhausted, the workflow stops and reports to the user.

## Domain Detection

The orchestrator determines the technology domain to select the right personas and domain-specific commands:

```mermaid
graph TB
    P[specs/PROJECT.md<br/>Technologies field] -->|primary| D{Domain}
    F[Open files<br/>extensions + imports] -->|secondary| D
    UM[User message<br/>keywords] -->|tertiary| D
    D --> flutter[dart-flutter]
    D --> angular[angular]
    D --> api[api-platform]
    D --> devops[devops]
    D --> symfony[symfony]
    D --> other["(no persona,<br/>use base role)"]
```

Once detected, the domain is used to:
- Load domain-specific **personas** (`agents/personas/{role}-{domain}.md`)
- Apply **domain commands** from `workflows/_domain-commands.yaml`
- Pass `{{domain}}` to workflow templates

## Workflow YAML Structure

Each workflow file follows the contract in `workflows/WORKFLOW_CONTRACT.md`:

```yaml
name: feature-implementation          # Stable ID
description: "..."                     # Human summary
domain: "{{domain}}"                   # Placeholder or fixed
trigger: "User asks to..."            # When orchestrator selects this
prerequisites: []                      # Optional
steps:
  - id: research                       # Phase ID
    subagent_type: generalPurpose      # Which role to use
    skills:                            # Skills to load
      - common-best-practices
      - common-context-optimization
    entry_criteria: "..."              # What must be true to start
    exit_criteria: "..."               # What must be true to finish
    handoff_to: plan                   # Next phase
    on_concern_return_to: null         # Where to go if blocked
    prompt_template: |                 # Template for subagent prompt
      Workspace: {{workspace}}. Domain: {{domain}}.
      Goal: {{user_goal}}.
      ...
```

## Token Economics

The orchestrator manages context window usage following `rules/100-token-economics.mdc`:

- The **main thread** stays minimal: routing, workflow selection, short user updates
- **Subagents** hold execution context (code, diffs, test output)
- Broad exploration (many file reads, wide globs) always goes to a subagent
- Handoff files and `.agent-output/` directories carry context between phases instead of chat history

## Quick Reference

| Resource | Path |
|----------|------|
| Skills index | `skills-index.json` |
| Skills root | `skills/` |
| Workflows | `workflows/` |
| Workflow contract | `workflows/WORKFLOW_CONTRACT.md` |
| Agent roles | `agents/roles/` |
| Agent personas | `agents/personas/` |
| Orchestration rules | `rules/010-orchestrator.mdc` |
| Delegation rules | `rules/050-subagent-delegation.mdc` |
| Handoff protocol | `rules/051-handoff-protocol.mdc` |
| Token economics | `rules/100-token-economics.mdc` |
| Kit architecture | `rules/016-kit-architecture.mdc` |
