# Prompt: Setup Cursor Agentic in a new project

**Usage:** Paste this block in a new chat in the target project. The agent will ask which technologies you use before configuring.

---

I need to configure Cursor for agentic work in this project following proven best practices.

**Important:** Before creating files, ask me which technologies the project uses (framework, database, observability, infra, design, task management, etc.). Only then enable the needed MCPs in `.cursor/mcp.json`. Do not assume technologies.

**Stack (fill in if you know it; otherwise answer when asked):**
- Type: `[mobile | web | backend | infra | multi]`
- Framework: `[Flutter | Angular | React | Node | Python | Terraform | …]`
- Services: `[Supabase | Firebase | Sentry | Cloudflare | Figma | ClickUp | RevenueCat | …]`

**What to configure:**

- **`.cursor/mcp.json`** — Include only MCPs from the technology→MCP table in the cursor-agentic-setup skill, for the technologies I confirm. Use the list in mcp.json as the single source of what's in scope.

- **`.cursor/agents/`** — Custom subagents (Cursor Subagents): `planner.md`, `implementer.md`, `tester.md`, `verifier.md`. Each with YAML frontmatter (name, description) and short prompt. Used for mcp_task/subagent delegation.

- **`.cursor/workflows/`** — YAML workflows for complex tasks. Minimum: `feature-implementation.workflow.yaml` (plan → implement → test → verify) and `bugfix.workflow.yaml` (explore → implement → test → verify). Each step has: id, subagent_type, handoff_to, prompt_template with placeholders `{{workspace}}`, `{{domain}}`, `{{user_goal}}`.

- **`.cursor/handoff.md.template`** — Handoff template between steps. Fields: Goal reached, Changes made, Unresolved/risks, Next assigned role, Domain. Add: Blocked, Return_to_step, Block_reason for rollback.

- **`.cursor/worktrees.json`** (optional) — If you use Parallel Agents. setup-worktree-unix: `flutter pub get` (Flutter) or `npm ci` (Node), copy .env from `$ROOT_WORKTREE_PATH` if it exists.

- **`.cursorrules`** at repo root — Resolve domain (mobile/web/infra), allowed MCPs, reference to handoff, and **Hack 2 (Kill the Documentation Generation):** Do NOT proactively create or update README, CHANGELOG, or other docs; only when user explicitly asks. Prefer minimal inline comments. Keep it short.

- **`.gitignore`** — Add `.cursor/agents/` so agent outputs and analysis stay local (do not version).

**Global rules to assume (if they exist):** Handover Protocol in `~/.cursor/rules/050-subagent-delegation.mdc`; MCP security in `~/.cursor/rules/200-security-mcp.mdc`. Do not modify global rules; only local config.

**Expected output:** Create the files in this project. Use only MCPs from the cursor-agentic-setup mapping table that Cursor/official docs support. If a needed MCP is missing from the table, indicate what's needed and how to add it manually.

**Reference skill:** Invoke or consult the `cursor-agentic-setup` skill if available for detailed guidance and exact templates.
