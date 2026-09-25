import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import plugin, {
  resolveBinaries,
  parseAgyLine,
  buildAgyBackend,
  buildClaudeBackend,
  buildClaudeCommand,
  makeClaudeCommandHandler,
  claudeSubagentSessionKey,
  FORBIDDEN_CLAUDE_FLAGS,
} from "../index.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function loadFixture(name) {
  return readFileSync(path.join(HERE, "fixtures", name), "utf-8")
    .split("\n")
    .filter((l) => l.trim().length > 0);
}

function parseAll(name) {
  return loadFixture(name).map(parseAgyLine);
}

// --- resolveBinaries -----------------------------------------------------------

test("resolveBinaries falls back to bare command names with no config", () => {
  assert.deepEqual(resolveBinaries(), { agy: "agy", claude: "claude", python3: "python3" });
});

test("resolveBinaries prefers absolute paths from plugin config", () => {
  const bins = resolveBinaries({
    agyBin: "/home/user/.local/bin/agy",
    claudeBin: "/home/user/.local/bin/claude",
    python3Bin: "/usr/bin/python3",
  });
  assert.equal(bins.agy, "/home/user/.local/bin/agy");
  assert.equal(bins.claude, "/home/user/.local/bin/claude");
  assert.equal(bins.python3, "/usr/bin/python3");
});

// --- parseAgyLine: init + text delta ---------------------------------------------

test("init line yields a sessionId event", () => {
  const [first] = parseAll("init-and-text.jsonl");
  assert.deepEqual(first, { kind: "sessionId", sessionId: "conv-synthetic-001" });
});

test("agent_response text_delta lines become text events, in order", () => {
  const [, second, third] = parseAll("init-and-text.jsonl");
  assert.deepEqual(second, { kind: "text", text: "Hello" });
  assert.deepEqual(third, { kind: "text", text: ", world." });
});

test("an unparseable line returns null, not a crash", () => {
  assert.equal(parseAgyLine("not json"), null);
});

test("an empty text_delta produces no text event", () => {
  const line = JSON.stringify({ step_update: { step_type: "agent_response", text_delta: "" } });
  assert.deepEqual(parseAgyLine(line), []);
});

// --- parseAgyLine: tool start/result ---------------------------------------------

test("a tool step in ACTIVE state becomes toolStart with its call id and args", () => {
  const events = parseAll("tool-call.jsonl");
  const toolStart = events[1];
  assert.equal(toolStart.kind, "toolStart");
  assert.equal(toolStart.toolCallId, "conv-synthetic-002:2");
  assert.equal(toolStart.name, "sessions_spawn");
  assert.deepEqual(toolStart.args, { agentId: "claude", cwd: "/home/example/Development/example-project" });
});

test("a tool step in DONE state becomes toolResult with the matching call id", () => {
  const events = parseAll("tool-call.jsonl");
  const toolResult = events[2];
  assert.deepEqual(toolResult, {
    kind: "toolResult", toolCallId: "conv-synthetic-002:2", name: "sessions_spawn",
  });
});

test("a successful terminal result carries text, sessionId and usage", () => {
  const events = parseAll("tool-call.jsonl");
  const result = events[3];
  assert.equal(result.kind, "result");
  assert.equal(result.text, "Delegated to claude.");
  assert.equal(result.sessionId, "conv-synthetic-002");
  assert.deepEqual(result.usage, { input: 120, output: 45 });
  assert.equal(result.errorText, undefined);
});

// --- parseAgyLine: AC-04, never an empty reply -----------------------------------

test("a non-SUCCESS status becomes an errorText result, never a reply", () => {
  const events = parseAll("result-error.jsonl");
  const result = events[1];
  assert.equal(result.kind, "result");
  assert.equal(result.text, undefined);
  assert.equal(result.errorText, "synthetic failure for testing");
});

test("an empty response on a SUCCESS status still becomes an errorText result", () => {
  const events = parseAll("result-empty-response.jsonl");
  const result = events[1];
  assert.equal(result.kind, "result");
  assert.equal(result.text, undefined);
  assert.match(result.errorText, /no text/);
});

// --- claude-kit backend argv -------------------------------------------------------

test("the claude-kit args carry --dangerously-skip-permissions and none of the forbidden flags", () => {
  const backend = buildClaudeBackend({ claude: "/abs/path/claude" });
  const allArgs = [...backend.config.args, ...backend.config.resumeArgs];
  assert.ok(allArgs.includes("--dangerously-skip-permissions"));
  for (const flag of FORBIDDEN_CLAUDE_FLAGS) {
    assert.ok(!allArgs.includes(flag), `forbidden flag present: ${flag}`);
  }
  assert.equal(backend.bundleMcp, false);
  assert.equal(backend.jsonlDialect, "claude-stream-json");
  assert.equal(backend.config.command, "/abs/path/claude");
});

test("the claude-kit backend resumes sessions with --resume and starts them with --session-id", () => {
  const backend = buildClaudeBackend({ claude: "claude" });
  assert.deepEqual(backend.config.sessionArgs, ["--session-id", "{sessionId}"]);
  assert.ok(backend.config.resumeArgs.includes("--resume"));
  assert.ok(backend.config.resumeArgs.includes("{sessionId}"));
  assert.equal(backend.config.sessionMode, "always");
  assert.equal(backend.config.input, "stdin");
});

test("the claude-kit backend does NOT declare instruction isolation with exact tools", () => {
  // DO NOT "FIX" A FAILING skill-collection-review JOB BY ADDING THIS FLAG.
  //
  // OpenClaw's Skill Workshop registers a weekly `skill-collection-review-<agent>` job that runs
  // under `rootedExecution`. The gateway then requires the resolved CLI backend to declare
  // `isolatesInstructionsWithExactTools === true` AND `bundleMcp`. On a kit host the `claude`
  // worker is primary-bound to `claude-kit/*`, so that job fails with
  // 'CLI backend "claude-kit" does not declare instruction isolation with exact tools'
  // (measured 2026-09-25, openclaw 2026.9.6). Setting the flag here would be:
  //   1. a false claim about a security property — this backend runs
  //      --dangerously-skip-permissions and keeps bundleMcp false precisely so core never
  //      injects --strict-mcp-config / --mcp-config / --disallowedTools (AC-03); and
  //   2. useless anyway — the gate's very next condition requires bundleMcp, which stays false.
  // The supported fix is `skills.workshop.autonomous.mode` (the kit's setup authors "propose"),
  // which stops the job from being registered at all.
  const backend = buildClaudeBackend({ claude: "claude" });
  assert.ok(
    !("isolatesInstructionsWithExactTools" in backend),
    "claude-kit must not claim instruction isolation: it cannot enforce it",
  );
  assert.equal(backend.bundleMcp, false);
});

// --- agy-cli backend shape ---------------------------------------------------------

test("the agy-cli backend bundles MCP through gemini-system-settings and resumes via --conversation", () => {
  const backend = buildAgyBackend({ agy: "/abs/path/agy" });
  assert.equal(backend.id, "agy-cli");
  assert.equal(backend.bundleMcp, true);
  assert.equal(backend.bundleMcpMode, "gemini-system-settings");
  assert.equal(backend.config.command, "/abs/path/agy");
  assert.deepEqual(backend.config.sessionIdFields, ["conversation_id"]);
  assert.ok(backend.config.resumeArgs.includes("--conversation"));
  assert.equal(backend.parseJsonlEvent, parseAgyLine);
});

// --- manifest <-> registered backend ids (AC-02) -----------------------------------

test("the plugin manifest's cliBackends match what register() actually registers", () => {
  const manifestPath = path.join(HERE, "..", "openclaw.plugin.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
  const registered = [];
  const api = {
    pluginConfig: {},
    registerCliBackend(def) { registered.push(def.id); },
    registerCommand() {},
  };
  plugin.register(api);
  assert.deepEqual([...manifest.cliBackends].sort(), [...registered].sort());
});

// --- register() reads plugin config from api.pluginConfig, not api.config ----------
// api.config is the *whole* OpenClawConfig snapshot (docs/plugins/sdk-overview.md);
// plugins.entries.<id>.config is exposed separately as api.pluginConfig. Reading
// api.config for agyBin/claudeBin would always see `undefined` on a live gateway.

test("register() resolves binaries from api.pluginConfig, never from the whole api.config snapshot", () => {
  const backends = [];
  const api = {
    config: { agents: { defaults: {} }, agyBin: "/wrong/agy", claudeBin: "/wrong/claude" },
    pluginConfig: { agyBin: "/abs/agy", claudeBin: "/abs/claude" },
    registerCliBackend(def) { backends.push(def); },
    registerCommand() {},
  };
  plugin.register(api);
  assert.deepEqual(backends.map((b) => b.id).sort(), ["agy-cli", "claude-kit"]);
  assert.equal(backends.find((b) => b.id === "agy-cli").config.command, "/abs/agy");
  assert.equal(backends.find((b) => b.id === "claude-kit").config.command, "/abs/claude");
});

test("register() falls back to bare command names when pluginConfig is empty, even if api.config carries other data", () => {
  const backends = [];
  const api = {
    config: { agyBin: "/decoy/agy", claudeBin: "/decoy/claude" },
    pluginConfig: {},
    registerCliBackend(def) { backends.push(def); },
    registerCommand() {},
  };
  plugin.register(api);
  assert.equal(backends.find((b) => b.id === "agy-cli").config.command, "agy");
  assert.equal(backends.find((b) => b.id === "claude-kit").config.command, "claude");
});

// --- /claude and /equipo shortcut --------------------------------------------------

test("buildClaudeCommand matches the OpenClawPluginCommandDefinition contract: name + description, no id/aliases/continueAgent", () => {
  const handler = async () => ({ text: "ok" });
  const cmd = buildClaudeCommand("claude", handler);
  assert.equal(cmd.name, "claude");
  assert.equal(typeof cmd.description, "string");
  assert.ok(cmd.description.length > 0);
  assert.equal(cmd.acceptsArgs, true);
  assert.equal(cmd.requireAuth, true);
  assert.equal(cmd.handler, handler);
  assert.equal(cmd.id, undefined, "OpenClawPluginCommandDefinition has no top-level id field");
  assert.equal(cmd.aliases, undefined, "aliases isn't part of the command definition; register a second command instead");
  assert.equal(cmd.continueAgent, undefined, "continueAgent belongs on the handler's PluginCommandResult, not the definition");
});

test("register() registers /claude and /equipo as two command definitions sharing one handler", () => {
  const commands = [];
  const api = {
    pluginConfig: {},
    registerCliBackend() {},
    registerCommand(def) { commands.push(def); },
    runtime: { subagent: { async run() { return { runId: "r1" }; } } },
  };
  plugin.register(api);
  assert.deepEqual(commands.map((c) => c.name).sort(), ["claude", "equipo"]);
  assert.equal(commands[0].handler, commands[1].handler);
});

test("claudeSubagentSessionKey targets the claude agent, derived from the invoking context", () => {
  const key = claudeSubagentSessionKey({ sessionKey: "telegram:main:123" });
  assert.match(key, /^agent:claude:subagent:/);
  assert.match(key, /telegram:main:123/);
});

test("claudeSubagentSessionKey never crashes with no context at all", () => {
  assert.match(claudeSubagentSessionKey(undefined), /^agent:claude:subagent:/);
});

test("the command handler calls runtime.subagent.run with a claude-agent sessionKey, the raw args as message, and deliver:true", async () => {
  const calls = [];
  const api = {
    runtime: {
      subagent: {
        async run(opts) { calls.push(opts); return { runId: "r1", sessionKey: "agent:claude:subagent:abc" }; },
      },
    },
  };
  const handler = makeClaudeCommandHandler(api);
  const result = await handler({ args: "implement X in pacha", sessionKey: "telegram:main:123" });
  assert.equal(calls.length, 1);
  assert.match(calls[0].sessionKey, /^agent:claude:subagent:/);
  assert.equal(calls[0].message, "implement X in pacha");
  assert.equal(calls[0].deliver, true);
  assert.equal(typeof result.text, "string");
  assert.equal(result.continueAgent, false);
});

test("the command handler throws a clear error when the runtime subagent API is missing", async () => {
  const handler = makeClaudeCommandHandler({});
  await assert.rejects(
    () => handler({ args: "hello" }),
    /runtime.subagent.run is not available/,
  );
});

// --- register() wires everything together ------------------------------------------

test("register() adds both CLI backends and the claude command", () => {
  const backends = [];
  const commands = [];
  const api = {
    pluginConfig: { agyBin: "/abs/agy", claudeBin: "/abs/claude" },
    registerCliBackend(def) { backends.push(def); },
    registerCommand(def) { commands.push(def); },
    runtime: { subagent: { async run() { return { runId: "r1" }; } } },
  };
  plugin.register(api);
  assert.deepEqual(backends.map((b) => b.id).sort(), ["agy-cli", "claude-kit"]);
  assert.equal(backends.find((b) => b.id === "agy-cli").config.command, "/abs/agy");
  assert.equal(backends.find((b) => b.id === "claude-kit").config.command, "/abs/claude");
  assert.deepEqual(commands.map((c) => c.name).sort(), ["claude", "equipo"]);
});
