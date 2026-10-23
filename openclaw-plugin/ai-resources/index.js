import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

import { createOrchestrator, formatActions, formatStatus, RUN_ID_RE } from "./orchestrator.js";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));

// ai-resources OpenClaw plugin — agy-cli and claude-kit CLI backends, /claude, /lanes and /workflow commands.
//
// Plain JS, no build step, no third-party imports: OpenClaw loads this file directly.
// Binary paths come from the plugin config (`agyBin`, `claudeBin`), written by
// `ai-resources setup` as absolute paths resolved at configure time, because the
// gateway's systemd unit PATH is not guaranteed to see `~/.local/bin` or an fnm shim.
// Falls back to the bare command name so the plugin still loads (and fails loudly at
// spawn time, not at load time) if setup never ran.
//
// `register(api)` must read plugin-scoped config from `api.pluginConfig`
// (`plugins.entries.<id>.config`), never from `api.config` — the latter is the whole
// `OpenClawConfig` snapshot (docs/plugins/sdk-overview.md), so `api.config.agyBin` is
// always undefined and would silently fall back to the bare `agy`/`claude` names.

/** @param {Record<string, string>} config */
export function resolveBinaries(config = {}) {
  return {
    agy: config.agyBin || "agy",
    claude: config.claudeBin || "claude",
    python3: config.python3Bin || "python3",
  };
}

// --- agy-cli: parses agy's `--output-format stream-json` lines --------------------
//
// Verified against agy 1.2.5 in the S0 spike (see .agent-output/implementer/handoff.md):
//   - `init.conversation_id`                                  -> sessionId
//   - `step_update` with step_type "agent_response"           -> text delta
//   - `step_update` with step_type "tool", state ACTIVE/DONE   -> toolStart/toolResult
//   - `result`                                                 -> terminal event
// A `result` whose status isn't SUCCESS, or whose response text is empty, must never
// surface as an empty reply — it becomes an `errorText` instead (AC-04).
export function parseAgyLine(line) {
  let event;
  try {
    event = JSON.parse(line);
  } catch {
    return null;
  }
  if (event && event.event === "init" && event.conversation_id) {
    return { kind: "sessionId", sessionId: event.conversation_id };
  }
  const step = event && event.step_update;
  if (step) {
    if (step.step_type === "agent_response" && typeof step.text_delta === "string" && step.text_delta) {
      return { kind: "text", text: step.text_delta };
    }
    if (step.step_type === "tool" && step.state === "ACTIVE") {
      return {
        kind: "toolStart",
        toolCallId: `${step.conversation_id}:${step.step_index}`,
        name: step.tool_name || "tool",
        args: step.tool_info && step.tool_info.parameters,
      };
    }
    if (step.step_type === "tool" && step.state === "DONE") {
      return {
        kind: "toolResult",
        toolCallId: `${step.conversation_id}:${step.step_index}`,
        name: step.tool_name,
      };
    }
    return [];
  }
  const result = event && event.result;
  if (result) {
    const usage = result.usage
      ? { input: result.usage.input_tokens, output: result.usage.output_tokens }
      : undefined;
    const text = (result.response || "").trim();
    const quotaErr = /^(?:error:\s*)?(?:you(?:'ve| have) hit your limit|usage limit|rate[_ -]?limit|quota[_ -]?exceeded|resource[_ -]?exhausted|429\b|overloaded_error)/i.test(text);
    if (result.status !== "SUCCESS" || !text || quotaErr) {
      const reason = (result.error && (result.error.message || result.error))
        || (quotaErr ? text : `agy returned status=${result.status} with ${text ? "text" : "no text"}`);
      return { kind: "result", sessionId: result.conversation_id, usage, errorText: reason };
    }
    return { kind: "result", text, sessionId: result.conversation_id, usage };
  }
  return null;
}

/** @param {{agy: string}} bins @param {{serializeAgy?: boolean, serialize?: boolean}} [options] */
export function buildAgyBackend(bins, options = {}) {
  const serialize = Boolean(options.serializeAgy ?? options.serialize ?? false);
  return {
    id: "agy-cli",
    bundleMcp: true,
    bundleMcpMode: "gemini-system-settings",
    nativeToolMode: "always-on",
    parseJsonlEvent: parseAgyLine,
    config: {
      command: bins.agy,
      args: [
        "--dangerously-skip-permissions", "--disable-slash-commands",
        "--output-format", "stream-json", "-p", "{prompt}",
      ],
      resumeArgs: [
        "--dangerously-skip-permissions", "--disable-slash-commands",
        "--conversation", "{sessionId}", "--output-format", "stream-json", "-p", "{prompt}",
      ],
      output: "jsonl",
      resumeOutput: "jsonl",
      input: "arg",
      modelArg: "--model",
      sessionMode: "existing",
      sessionIdFields: ["conversation_id"],
      serialize,
    },
  };
}

// --- agy-cli-lane: the Antigravity lane backend (ADR-0007 section 11) -------------------------
//
// A separate backend id so a lane child's flags never leak into the classic `agy-cli` backend, which stays
// byte-identical (snapshot test). Same parser, model argument and serialisation. Differences, each as a fact:
//   - `bundleMcp: false`: OpenClaw-bundled MCP is not injected into a lane child (V7 a is not proven; the
//     writer additionally denies `bundle-mcp` per agent).
//   - an absolute `agyBin` is required: with a bare name the backend refuses to register (the classic one
//     keeps "agy"), so a lane never resolves `agy` through the walked PATH.
//   - `--dangerously-skip-permissions` stays: no non-interactive alternative has been shown (V6 not run).
// Not added, because no recorded run supports them: a sandbox flag (V6), a tool allowlist or exclusions (V7),
// a print timeout, an agy-home override (V12). `AGY_LANE_UNPROVEN` lists them so the gap is visible in code.
export const AGY_LANE_UNPROVEN = Object.freeze({
  sandbox: "V6 not run",
  toolAllowlist: "V7 not run",
  agyHome: "V12 not run",
});

/** @param {string} p */
export function isAbsoluteBin(p) {
  return typeof p === "string" && p.startsWith("/");
}

/** @param {{agy: string}} bins @param {{serializeAgy?: boolean, serialize?: boolean}} [options] */
export function buildAgyLaneBackend(bins, options = {}) {
  const base = buildAgyBackend(bins, options);
  const config = JSON.parse(JSON.stringify(base.config));
  return {
    id: "agy-cli-lane",
    bundleMcp: false,
    nativeToolMode: "always-on",
    parseJsonlEvent: parseAgyLine,
    config,
  };
}

// --- claude-kit: the unrestricted Claude Code backend ------------------------------
//
// Deliberately does NOT set bundleMcp, so core never injects `--strict-mcp-config`,
// `--mcp-config` or `--disallowedTools` (see `injectClaudeMcpConfigArgs` in
// dist/prompt-context-Dv953bax.mjs). It also never carries `--setting-sources` or
// `--permission-mode`: those are only added by the *bundled* `claude-cli` backend's
// `normalizeClaudeBackendArgs`, which this backend bypasses entirely by registering
// under a different id. `--dangerously-skip-permissions` survives because nothing
// strips it here (AC-03).
const FORBIDDEN_CLAUDE_FLAGS = Object.freeze([
  "--strict-mcp-config", "--setting-sources", "--disallowedTools", "--permission-mode",
]);

/** @param {{claude: string}} bins */
export function buildClaudeBackend(bins) {
  return {
    id: "claude-kit",
    bundleMcp: false,
    nativeToolMode: "always-on",
    jsonlDialect: "claude-stream-json",
    config: {
      command: bins.claude,
      args: [
        "-p", "--output-format", "stream-json", "--include-partial-messages",
        "--verbose", "--dangerously-skip-permissions",
      ],
      resumeArgs: [
        "-p", "--output-format", "stream-json", "--include-partial-messages",
        "--verbose", "--dangerously-skip-permissions", "--resume", "{sessionId}",
      ],
      output: "jsonl",
      resumeOutput: "jsonl",
      input: "stdin",
      modelArg: "--model",
      sessionMode: "always",
      sessionArgs: ["--session-id", "{sessionId}"],
      sessionIdFields: ["session_id"],
      systemPromptFileArg: "--append-system-prompt-file",
      systemPromptWhen: "first",
      serialize: true,
    },
  };
}

// --- /claude and /equipo shortcut ---------------------------------------------------
//
// S0(e) showed `before_model_resolve` never runs for `openclaw agent` turns, so the
// shortcut is a real command (`api.registerCommand`), not a hook.
//
// `OpenClawPluginCommandDefinition` (dist/plugin-entry-*.d.ts) has `name` and
// `description`, no `aliases` and no top-level `continueAgent` — aliases for
// native slash/menu surfaces go in `nativeNames`, but that does not create a second
// invocable command name for text-command routing, so `/equipo` is registered as its
// own command definition, sharing the same handler closure as `/claude`.
// The handler takes exactly one `ctx: PluginCommandContext` argument (not a second
// `ctx` parameter carrying `ctx.api` — there is no such second argument), so it is
// built as a closure over the `api` captured in `register(api)`. It hands the work to
// the `claude` worker agent through `api.runtime.subagent.run({ sessionKey, message,
// deliver })` (docs/plugins/sdk-runtime/background-work.md) — not `agentId`/`prompt`/
// `thread`/`cwd`, which is not that function's contract. `deliver: true` announces the
// subagent's completion back through the normal delivery path once it finishes; this
// handler's own return value is only the immediate acknowledgement.
// If the runtime subagent API is unavailable (older gateway), the handler throws
// instead of silently doing nothing: the kit's AGENTS.md block (S5) tells agy to call
// `sessions_spawn agentId=claude` itself whenever a message starts with `/claude` or
// `/equipo`, which is the fallback path and works whether or not this command runs.

/** Build a session key that targets a fresh subagent run on the `claude` worker agent. */
export function claudeSubagentSessionKey(ctx) {
  const seed = (ctx && (ctx.sessionKey || ctx.sessionId || ctx.senderId)) || `anon-${Date.now()}`;
  const safe = String(seed).replace(/\s+/g, "-").replace(/[^a-zA-Z0-9:_-]/g, "");
  return `agent:claude:subagent:${safe || "anon"}`;
}

/** @param {{name: string, handler: (ctx: object) => Promise<object>}} */
export function buildClaudeCommand(name, handler) {
  return {
    name,
    nativeNames: { default: name },
    description: "Hand a task to the Claude Code team (unrestricted claude-kit worker agent).",
    acceptsArgs: true,
    requireAuth: true,
    handler,
  };
}

/** @param {object} api The register(api) object; the handler closes over it. */
export function makeClaudeCommandHandler(api) {
  return async function claudeCommandHandler(ctx) {
    const run = api && api.runtime && api.runtime.subagent && api.runtime.subagent.run;
    if (typeof run !== "function") {
      throw new Error(
        "ai-resources: runtime.subagent.run is not available on this gateway; "
        + "agy's AGENTS.md instructions cover /claude and /equipo instead."
      );
    }
    const message = String((ctx && ctx.args) || "").trim();
    const sessionKey = claudeSubagentSessionKey(ctx);
    const result = await run.call(api.runtime.subagent, { sessionKey, message, deliver: true });
    const startedKey = (result && result.sessionKey) || sessionKey;
    return {
      text: `Handed off to the claude worker agent (session ${startedKey}).`,
      continueAgent: false,
    };
  };
}

// --- native team orchestrator (ADR-0015) -------------------------------------------
//
// With `enableOrchestrator` in the plugin config, /equipo and /workflow run|status|approve|cancel|resume|cost drive the
// Python engine (`ai-resources orchestrate`) through ./orchestrator.js. Without it every command behaves as before.

const ORCHESTRATORS = new WeakMap();
export const EQUIPO_WORKFLOW = "feature-implementation";

/** The orchestrator of this `api`, or null when the plugin config does not enable it. `api.orchestratorDeps` is the test seam. */
export function orchestratorFor(api) {
  const pluginConfig = (api && api.pluginConfig) || {};
  if (!api || !pluginConfig.enableOrchestrator) return null;
  if (!ORCHESTRATORS.has(api)) {
    ORCHESTRATORS.set(api, createOrchestrator(api, {
      pluginConfig,
      kitRoot: resolveKitRoot(pluginConfig),
      python3: resolveBinaries(pluginConfig).python3,
      ...(api.orchestratorDeps || {}),
    }));
  }
  return ORCHESTRATORS.get(api);
}

/** `--repo <path>` anywhere in a goal text, removed from it. */
export function splitRepoFlag(text) {
  const m = /(?:^|\s)--repo\s+(\S+)/.exec(text);
  if (!m) return { goal: text.trim(), workspace: "" };
  return { goal: text.replace(m[0], " ").replace(/\s+/g, " ").trim(), workspace: m[1] };
}

async function orchestratedStart(orch, workflow, rawGoal, ctx) {
  const { goal, workspace } = splitRepoFlag(rawGoal);
  if (!goal) return { text: "⚠️ **Usage**: give the team a goal, e.g. `/equipo add rate limiting to the API --repo /path/to/repo`", continueAgent: false };
  const result = await orch.start(workflow, goal, ctx, { workspace: workspace || (ctx && (ctx.workspaceDir || ctx.cwd)) || "" });
  const where = result.branch ? `\n**Branch**: \`${result.branch}\` (own worktree)` : "";
  return {
    text: `👥 **Team started**: \`${workflow}\`\n**Goal**: ${goal}${where}\n${formatActions(result)}\n\nFollow it with \`/workflow status ${result.run}\`.`,
    continueAgent: false,
  };
}

// --- /equipo team lifecycle command (ADR-0012) --------------------------------------

/** @param {{name: string, handler: (ctx: object) => Promise<object>}} */
export function buildEquipoCommand(name, handler) {
  return {
    name,
    nativeNames: { default: name },
    description: "Dispatch the full engineering team lifecycle (planner ➔ implementer ➔ verifier) for a goal.",
    acceptsArgs: true,
    requireAuth: true,
    handler,
  };
}

/** @param {object} api The register(api) object; the handler closes over it. */
export function makeEquipoCommandHandler(api) {
  return async function equipoCommandHandler(ctx) {
    const run = api && api.runtime && api.runtime.subagent && api.runtime.subagent.run;
    if (typeof run !== "function") {
      throw new Error(
        "ai-resources: runtime.subagent.run is not available on this gateway; "
        + "agy's AGENTS.md instructions cover /equipo instead."
      );
    }
    const goal = String((ctx && ctx.args) || "").trim() || "No goal specified";
    const orch = orchestratorFor(api);
    if (orch) {
      return orchestratedStart(orch, EQUIPO_WORKFLOW, String((ctx && ctx.args) || "").trim(), ctx);
    }
    const sessionKey = claudeSubagentSessionKey(ctx);

    const teamMessage = [
      "[AI-RESOURCES TEAM LIFECYCLE: ADR-0012]",
      `Goal: ${goal}`,
      "Pipeline Protocol:",
      "1. PLANNER: Analyze specs and research, generate plan with explicit acceptance criteria.",
      "2. IMPLEMENTER: Make code changes and write tests. (Self-approval is forbidden).",
      "3. VERIFIER: Independently verify all acceptance criteria before completion.",
      "Execute Phase 1 (planner) now.",
    ].join("\n");

    const result = await run.call(api.runtime.subagent, { sessionKey, message: teamMessage, deliver: true });
    const startedKey = (result && result.sessionKey) || sessionKey;
    return {
      text: `👥 **Team Lifecycle Dispatched** (Session \`${startedKey}\`)\n`
        + `**Goal**: ${goal}\n`
        + `**Pipeline**: \`planner\` ➔ \`implementer\` ➔ \`verifier\` (Active: \`planner\`)`,
      continueAgent: false,
    };
  };
}

// --- /lanes status cockpit command (ADR-0012) ---------------------------------------

/**
 * Parses rendered lanes.yaml summary without external dependencies.
 * @param {string} content
 */
export function parseLanesSummary(content) {
  const lines = (content || "").split("\n");
  let mode = "unknown";
  const roles = {};
  let currentSection = null;
  let currentRole = null;
  let inResolvedRoles = false;
  let inFallbacks = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const indent = line.search(/\S/);

    if (indent === 0) {
      const match = trimmed.match(/^([a-zA-Z0-9_-]+):\s*(.*)$/);
      if (match) {
        currentSection = match[1];
        if (currentSection === "mode") mode = match[2].trim();
        inResolvedRoles = false;
        currentRole = null;
      }
      continue;
    }

    if (currentSection === "resolved") {
      if (indent === 2 && trimmed.startsWith("roles:")) {
        inResolvedRoles = true;
        continue;
      }
      if (inResolvedRoles) {
        if (indent === 4 && trimmed.endsWith(":")) {
          currentRole = trimmed.slice(0, -1);
          roles[currentRole] = { lane: "", model: "", fallbacks: [], reason: "" };
          inFallbacks = false;
          continue;
        }
        if (currentRole && indent >= 6) {
          if (trimmed.startsWith("fallbacks:")) {
            inFallbacks = true;
            continue;
          }
          if (trimmed.startsWith("evidence:") || trimmed.startsWith("effort:")) {
            inFallbacks = false;
            continue;
          }
          if (inFallbacks) {
            if (trimmed.startsWith("- lane:")) {
              const fbLane = trimmed.replace("- lane:", "").trim().replace(/^['"]|['"]$/g, "");
              roles[currentRole].fallbacks.push({ lane: fbLane, model: "" });
            } else if (trimmed.startsWith("model:") && roles[currentRole].fallbacks.length > 0) {
              const fbModel = trimmed.replace("model:", "").trim().replace(/^['"]|['"]$/g, "");
              roles[currentRole].fallbacks[roles[currentRole].fallbacks.length - 1].model = fbModel;
            }
            continue;
          }

          const m = trimmed.match(/^([a-zA-Z0-9_-]+):\s*(.*)$/);
          if (m) {
            const key = m[1];
            const val = m[2].trim().replace(/^['"]|['"]$/g, "");
            if (key === "lane") roles[currentRole].lane = val;
            if (key === "model") roles[currentRole].model = val;
            if (key === "reason") roles[currentRole].reason = val;
          }
        }
      }
    }
  }
  return { mode, roles };
}

/**
 * Formats a lanes summary object into markdown suitable for chat output.
 * @param {{mode: string, roles: Record<string, {lane: string, model: string, fallbacks: Array<{lane: string, model: string}>}>}} summary
 * @param {string} lanesPath
 */
export function formatLanesMarkdown(summary, lanesPath) {
  if (!summary || !summary.roles || Object.keys(summary.roles).length === 0) {
    return `### 🚦 ai-resources: Subscription Lanes\n\nNo resolved lanes found in \`${lanesPath}\`.\nRun \`ai-resources setup --lanes\` to resolve subscription lanes.`;
  }
  let md = `### 🚦 ai-resources: Subscription Lanes\n`;
  md += `**Mode:** \`${summary.mode || "unknown"}\` | **Config:** \`${lanesPath}\`\n\n`;
  md += `| Role | Active Lane | Model | Fallbacks |\n`;
  md += `| :--- | :--- | :--- | :--- |\n`;
  for (const [role, info] of Object.entries(summary.roles)) {
    const laneShort = (info.lane || "").replace("openclaw-agent:", "");
    const modelShort = (info.model || "").replace("agy-cli-lane/", "");
    const fbText = info.fallbacks && info.fallbacks.length > 0
      ? info.fallbacks.map((f) => `\`${(f.lane || "").replace("openclaw-agent:", "")}\``).join(", ")
      : "—";
    md += `| **${role}** | \`${laneShort}\` | \`${modelShort}\` | ${fbText} |\n`;
  }
  return md;
}

export function buildLanesCommand(name, handler) {
  return {
    name,
    nativeNames: { default: name },
    description: "Display subscription lanes status and model assignments directly in chat.",
    acceptsArgs: true,
    requireAuth: true,
    handler,
  };
}

export function makeLanesCommandHandler(api) {
  return async function lanesCommandHandler(_ctx) {
    const pluginConfig = (api && api.pluginConfig) || {};
    const lanesPath = pluginConfig.lanesConfigPath
      || path.join(os.homedir(), ".config", "ai-resources", "lanes.yaml");

    let text;
    try {
      if (!fs.existsSync(lanesPath)) {
        return {
          text: `ℹ️ **ai-resources lanes**: No configuration found at \`${lanesPath}\`.\nRun \`ai-resources setup --lanes\` to configure subscription lanes.`,
          continueAgent: false,
        };
      }
      text = fs.readFileSync(lanesPath, "utf8");
    } catch (err) {
      return {
        text: `⚠️ **ai-resources lanes error**: Failed to read \`${lanesPath}\`: ${err.message}`,
        continueAgent: false,
      };
    }

    const summary = parseLanesSummary(text);
    return {
      text: formatLanesMarkdown(summary, lanesPath),
      continueAgent: false,
    };
  };
}

// --- /workflow cockpit commands (ADR-0012) -----------------------------------------

/**
 * Resolves the kit libexec root directory.
 * @param {Record<string, string>} pluginConfig
 * @param {string} [baseDir]
 */
export function resolveKitRoot(pluginConfig = {}, baseDir = PLUGIN_DIR) {
  if (pluginConfig.kitRoot && isAbsoluteBin(pluginConfig.kitRoot)) {
    return pluginConfig.kitRoot;
  }
  if (process.env.AGENT_KIT) {
    return process.env.AGENT_KIT;
  }
  const candidate = path.resolve(baseDir, "../..");
  if (fs.existsSync(path.join(candidate, "workflows"))) {
    return candidate;
  }
  const brewDefault = "/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec";
  if (fs.existsSync(path.join(brewDefault, "workflows"))) {
    return brewDefault;
  }
  return candidate;
}

/**
 * Parses header information and steps from a workflow YAML file without external deps.
 * @param {string} filePath
 */
export function parseWorkflowHeader(filePath) {
  const content = fs.readFileSync(filePath, "utf8");
  const lines = content.split("\n");
  const base = path.basename(filePath);
  const id = base.endsWith(".workflow.yaml") ? base.slice(0, -14) : base.replace(/\.ya?ml$/, "");

  const data = {
    id,
    name: id,
    description: "",
    trigger: "",
    steps: [],
  };

  let inSteps = false;
  let currentStep = null;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const indent = line.search(/\S/);

    if (indent === 0) {
      if (trimmed.startsWith("name:")) data.name = trimmed.replace("name:", "").trim().replace(/^['"]|['"]$/g, "");
      else if (trimmed.startsWith("description:")) data.description = trimmed.replace("description:", "").trim().replace(/^['"]|['"]$/g, "");
      else if (trimmed.startsWith("trigger:")) data.trigger = trimmed.replace("trigger:", "").trim().replace(/^['"]|['"]$/g, "");
      else if (trimmed.startsWith("steps:")) inSteps = true;
      continue;
    }

    if (inSteps) {
      if (trimmed.startsWith("- id:")) {
        currentStep = {
          id: trimmed.replace("- id:", "").trim().replace(/^['"]|['"]$/g, ""),
          role: "",
          to: "",
          entry: "",
          exit: "",
        };
        data.steps.push(currentStep);
      } else if (currentStep) {
        if (trimmed.startsWith("subagent_type:")) currentStep.role = trimmed.replace("subagent_type:", "").trim().replace(/^['"]|['"]$/g, "");
        else if (trimmed.startsWith("handoff_to:")) currentStep.to = trimmed.replace("handoff_to:", "").trim().replace(/^['"]|['"]$/g, "");
        else if (trimmed.startsWith("entry_criteria:")) currentStep.entry = trimmed.replace("entry_criteria:", "").trim().replace(/^['"]|['"]$/g, "");
        else if (trimmed.startsWith("exit_criteria:")) currentStep.exit = trimmed.replace("exit_criteria:", "").trim().replace(/^['"]|['"]$/g, "");
      }
    }
  }
  return data;
}

/**
 * Lists all workflow definitions found in workflowsDir.
 * @param {string} workflowsDir
 */
export function listWorkflows(workflowsDir) {
  if (!fs.existsSync(workflowsDir)) return [];
  try {
    const files = fs.readdirSync(workflowsDir)
      .filter((f) => f.endsWith(".workflow.yaml") && !f.startsWith("_"))
      .sort();
    return files.map((f) => parseWorkflowHeader(path.join(workflowsDir, f)));
  } catch {
    return [];
  }
}

/**
 * Formats workflow listing in markdown table.
 * @param {Array<object>} workflows
 * @param {string} workflowsDir
 */
export function formatWorkflowListMarkdown(workflows, workflowsDir) {
  if (!workflows || workflows.length === 0) {
    return `### 📋 ai-resources: Workflows\n\nNo workflows found in \`${workflowsDir}\`.`;
  }
  let md = `### 📋 ai-resources: Available Workflows\n`;
  md += `**Workflows directory:** \`${workflowsDir}\` (${workflows.length} workflows)\n\n`;
  md += `| Workflow ID | Pipeline / Steps | Description |\n`;
  md += `| :--- | :--- | :--- |\n`;
  for (const w of workflows) {
    const pipeline = w.steps.length > 0
      ? w.steps.map((s) => `\`${s.id}\``).join(" ➔ ")
      : "—";
    const desc = w.description ? w.description.slice(0, 80) + (w.description.length > 80 ? "..." : "") : "—";
    md += `| **${w.id}** | ${pipeline} | ${desc} |\n`;
  }
  md += `\n*Tip: Run \`/workflow info <id>\` for details, or \`/workflow run <id> [goal]\` to execute.*`;
  return md;
}

/**
 * Formats detailed info for a single workflow.
 * @param {object} w
 */
export function formatWorkflowInfoMarkdown(w) {
  let md = `### 📋 Workflow: \`${w.id}\`\n`;
  if (w.name && w.name !== w.id) md += `**Name:** ${w.name}\n`;
  if (w.description) md += `**Description:** ${w.description}\n`;
  if (w.trigger) md += `**Trigger:** ${w.trigger}\n`;
  md += `\n#### Pipeline Steps (${w.steps.length})\n`;
  if (w.steps.length === 0) {
    md += `No structured steps defined.\n`;
  } else {
    md += `| Step ID | Role (Subagent) | Next Step | Criteria |\n`;
    md += `| :--- | :--- | :--- | :--- |\n`;
    for (const s of w.steps) {
      const criteria = s.exit ? `Exit: ${s.exit}` : (s.entry ? `Entry: ${s.entry}` : "—");
      md += `| **${s.id}** | \`${s.role || "standard"}\` | \`${s.to || "finish"}\` | ${criteria} |\n`;
    }
  }
  md += `\n*To execute this workflow: \`/workflow run ${w.id} [goal]\`*`;
  return md;
}

export function buildWorkflowCommand(name, handler) {
  return {
    name,
    nativeNames: { default: name },
    description: "Inspect and run declarative workflows (/workflow list, /workflow info <id>, /workflow run <id>, and with the orchestrator status, approve, cancel, resume, cost).",
    acceptsArgs: true,
    requireAuth: true,
    handler,
  };
}

export function makeWorkflowCommandHandler(api) {
  return async function workflowCommandHandler(ctx) {
    const pluginConfig = (api && api.pluginConfig) || {};
    const kitRoot = resolveKitRoot(pluginConfig);
    const workflowsDir = path.join(kitRoot, "workflows");

    const rawArgs = String((ctx && ctx.args) || "").trim();
    const parts = rawArgs.split(/\s+/).filter(Boolean);
    const subcmd = (parts[0] || "list").toLowerCase();
    const targetId = parts[1];

    const orch = orchestratorFor(api);
    if (orch && ["run", "status", "approve", "cancel", "resume", "cost"].includes(subcmd)) {
      try {
        if (subcmd === "run") {
          if (!targetId) {
            return { text: "⚠️ **Usage**: `/workflow run <workflow-id> <goal> [--repo <path>]`", continueAgent: false };
          }
          return await orchestratedStart(orch, targetId, parts.slice(2).join(" "), ctx);
        }
        if (subcmd === "status") {
          return { text: formatStatus(await orch.status(targetId || "")), continueAgent: false };
        }
        if (subcmd === "resume") {
          return { text: formatActions(await orch.resume(targetId || "")) || "Nothing to resume.", continueAgent: false };
        }
        if (!targetId || !RUN_ID_RE.test(targetId)) {
          return { text: `⚠️ **Usage**: \`/workflow ${subcmd} <run-id>\` (see \`/workflow status\`)`, continueAgent: false };
        }
        if (subcmd === "approve") return { text: formatActions(await orch.approve(targetId)), continueAgent: false };
        if (subcmd === "cancel") return { text: formatActions(await orch.cancel(targetId)) || `Run \`${targetId}\` cancelled.`, continueAgent: false };
        const cost = await orch.cost(targetId);
        const rows = Object.entries(cost.by_role || {}).map(([r, v]) => `| ${r} | ${v.calls} | ${v.input} | ${v.output} | ${v.usd} |`);
        return {
          text: [`**Cost of \`${targetId}\`** — ${cost.label}`, "", "| Role | Calls | Input | Output | USD |", "| :--- | ---: | ---: | ---: | ---: |",
            ...rows, `| **total** | ${cost.total.calls} | ${cost.total.input} | ${cost.total.output} | ${cost.total.usd} |`].join("\n"),
          continueAgent: false,
        };
      } catch (e) {
        return { text: `⚠️ **Orchestrator**: ${e.message}`, continueAgent: false };
      }
    }

    if (subcmd === "list") {
      const workflows = listWorkflows(workflowsDir);
      return {
        text: formatWorkflowListMarkdown(workflows, workflowsDir),
        continueAgent: false,
      };
    }

    if (subcmd === "info") {
      if (!targetId) {
        return {
          text: "⚠️ **Usage**: `/workflow info <workflow-id>` (e.g. `/workflow info feature-lanes`)",
          continueAgent: false,
        };
      }
      const candidates = [
        path.join(workflowsDir, `${targetId}.workflow.yaml`),
        path.join(workflowsDir, `${targetId}.yaml`),
        path.join(workflowsDir, `_${targetId}.workflow.yaml`),
      ];
      const match = candidates.find((p) => fs.existsSync(p));
      if (!match) {
        return {
          text: `⚠️ **Workflow not found**: Could not find workflow \`${targetId}\` in \`${workflowsDir}\`.\nRun \`/workflow list\` to see available workflows.`,
          continueAgent: false,
        };
      }
      const wf = parseWorkflowHeader(match);
      return {
        text: formatWorkflowInfoMarkdown(wf),
        continueAgent: false,
      };
    }

    if (subcmd === "run") {
      if (!targetId) {
        return {
          text: "⚠️ **Usage**: `/workflow run <workflow-id> [goal]` (e.g. `/workflow run explore-and-plan review auth`)",
          continueAgent: false,
        };
      }
      const candidates = [
        path.join(workflowsDir, `${targetId}.workflow.yaml`),
        path.join(workflowsDir, `${targetId}.yaml`),
        path.join(workflowsDir, `_${targetId}.workflow.yaml`),
      ];
      const match = candidates.find((p) => fs.existsSync(p));
      if (!match) {
        return {
          text: `⚠️ **Workflow not found**: Could not find workflow \`${targetId}\` in \`${workflowsDir}\`.`,
          continueAgent: false,
        };
      }
      const wf = parseWorkflowHeader(match);
      const goal = parts.slice(2).join(" ") || "No specific goal provided";

      const run = api && api.runtime && api.runtime.subagent && api.runtime.subagent.run;
      if (typeof run === "function") {
        const sessionKey = claudeSubagentSessionKey(ctx);
        const firstStep = wf.steps[0];
        const initialPrompt = `Executing workflow ${wf.id} (${wf.name}):\nGoal: ${goal}\nInitial Phase: ${firstStep ? firstStep.id : "start"} (role: ${firstStep ? firstStep.role : "standard"})\nWorkflow definition: ${match}`;
        const result = await run.call(api.runtime.subagent, { sessionKey, message: initialPrompt, deliver: true });
        const startedKey = (result && result.sessionKey) || sessionKey;
        return {
          text: `🚀 **Workflow Started**: \`${wf.id}\` (Session \`${startedKey}\`)\n**Goal**: ${goal}\n**Pipeline**: ${wf.steps.map((s) => `\`${s.id}\``).join(" ➔ ") || "direct"}`,
          continueAgent: false,
        };
      }

      return {
        text: `🚀 **Workflow Ready**: \`${wf.id}\`\n**Goal**: ${goal}\nRun subagent via \`sessions_spawn\` to start phase \`${wf.steps[0]?.id || "initial"}\`.`,
        continueAgent: false,
      };
    }

    return {
      text: `⚠️ **Unknown sub-command**: \`/workflow ${subcmd}\`.\nAvailable commands: \`/workflow list\`, \`/workflow info <id>\`, \`/workflow run <id> [goal]\``,
      continueAgent: false,
    };
  };
}

export default {
  id: "ai-resources",
  name: "ai-resources",
  description: "agy-cli, agy-cli-lane and claude-kit CLI backends, plus /claude, /equipo, /lanes and /workflow cockpit commands.",
  register(api) {
    const pluginConfig = (api && api.pluginConfig) || {};
    const bins = resolveBinaries(pluginConfig);
    api.registerCliBackend(buildAgyBackend(bins, pluginConfig));
    if (isAbsoluteBin(bins.agy)) {
      api.registerCliBackend(buildAgyLaneBackend(bins, pluginConfig));
    }
    api.registerCliBackend(buildClaudeBackend(bins));
    const claudeHandler = makeClaudeCommandHandler(api);
    api.registerCommand(buildClaudeCommand("claude", claudeHandler));
    const equipoHandler = makeEquipoCommandHandler(api);
    api.registerCommand(buildEquipoCommand("equipo", equipoHandler));
    const lanesHandler = makeLanesCommandHandler(api);
    api.registerCommand(buildLanesCommand("lanes", lanesHandler));
    const workflowHandler = makeWorkflowCommandHandler(api);
    api.registerCommand(buildWorkflowCommand("workflow", workflowHandler));
    // ADR-0015: hand out again the steps a gateway restart interrupted. Fire and forget: a failure is logged, never thrown.
    const orch = orchestratorFor(api);
    if (orch) orch.resumeAll();
  },
};

export { FORBIDDEN_CLAUDE_FLAGS };
