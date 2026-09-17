// ai-resources OpenClaw plugin — agy-cli and claude-kit CLI backends, the /claude shortcut.
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
    if (result.status !== "SUCCESS" || !text) {
      const reason = (result.error && (result.error.message || result.error))
        || `agy returned status=${result.status} with ${text ? "text" : "no text"}`;
      return { kind: "result", sessionId: result.conversation_id, usage, errorText: reason };
    }
    return { kind: "result", text, sessionId: result.conversation_id, usage };
  }
  return null;
}

/** @param {{agy: string}} bins */
export function buildAgyBackend(bins) {
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
      serialize: true,
    },
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

export default {
  id: "ai-resources",
  name: "ai-resources",
  description: "agy-cli and claude-kit CLI backends, plus the /claude and /equipo shortcut.",
  register(api) {
    const bins = resolveBinaries((api && api.pluginConfig) || {});
    api.registerCliBackend(buildAgyBackend(bins));
    api.registerCliBackend(buildClaudeBackend(bins));
    const handler = makeClaudeCommandHandler(api);
    api.registerCommand(buildClaudeCommand("claude", handler));
    api.registerCommand(buildClaudeCommand("equipo", handler));
  },
};

export { FORBIDDEN_CLAUDE_FLAGS };
