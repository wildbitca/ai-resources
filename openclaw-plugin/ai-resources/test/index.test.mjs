import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, unlinkSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import plugin, {
  resolveBinaries,
  parseAgyLine,
  buildAgyBackend,
  buildAgyLaneBackend,
  isAbsoluteBin,
  AGY_LANE_UNPROVEN,
  buildClaudeBackend,
  buildClaudeCommand,
  makeClaudeCommandHandler,
  buildEquipoCommand,
  makeEquipoCommandHandler,
  claudeSubagentSessionKey,
  FORBIDDEN_CLAUDE_FLAGS,
  parseLanesSummary,
  formatLanesMarkdown,
  buildLanesCommand,
  makeLanesCommandHandler,
  resolveKitRoot,
  parseWorkflowHeader,
  listWorkflows,
  formatWorkflowListMarkdown,
  formatWorkflowInfoMarkdown,
  buildWorkflowCommand,
  makeWorkflowCommandHandler,
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
    pluginConfig: { agyBin: "/abs/agy" },       // ADR-0007 phase 3: the lane backend needs an absolute agyBin
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
  assert.deepEqual(backends.map((b) => b.id).sort(), ["agy-cli", "agy-cli-lane", "claude-kit"]);
  assert.equal(backends.find((b) => b.id === "agy-cli").config.command, "/abs/agy");
  assert.equal(backends.find((b) => b.id === "agy-cli-lane").config.command, "/abs/agy");
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

test("register() registers /claude and /equipo as cockpit command definitions", () => {
  const commands = [];
  const api = {
    pluginConfig: {},
    registerCliBackend() {},
    registerCommand(def) { commands.push(def); },
    runtime: { subagent: { async run() { return { runId: "r1" }; } } },
  };
  plugin.register(api);
  assert.deepEqual(commands.map((c) => c.name).sort(), ["claude", "equipo", "lanes", "workflow"]);
  const claudeCmd = commands.find((c) => c.name === "claude");
  const equipoCmd = commands.find((c) => c.name === "equipo");
  assert.equal(typeof claudeCmd.handler, "function");
  assert.equal(typeof equipoCmd.handler, "function");
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

test("register() adds both CLI backends and the cockpit commands", () => {
  const backends = [];
  const commands = [];
  const api = {
    pluginConfig: { agyBin: "/abs/agy", claudeBin: "/abs/claude" },
    registerCliBackend(def) { backends.push(def); },
    registerCommand(def) { commands.push(def); },
    runtime: { subagent: { async run() { return { runId: "r1" }; } } },
  };
  plugin.register(api);
  assert.deepEqual(backends.map((b) => b.id).sort(), ["agy-cli", "agy-cli-lane", "claude-kit"]);
  assert.equal(backends.find((b) => b.id === "agy-cli").config.command, "/abs/agy");
  assert.equal(backends.find((b) => b.id === "claude-kit").config.command, "/abs/claude");
  assert.deepEqual(commands.map((c) => c.name).sort(), ["claude", "equipo", "lanes", "workflow"]);
});

// --- agy-cli-lane (ADR-0007 section 11, phase 3) -----------------------------------

// The classic backend, byte for byte as it was before phase 3 (a snapshot, so a lane change cannot leak in).
const AGY_CLI_SNAPSHOT = {
  id: "agy-cli",
  bundleMcp: true,
  bundleMcpMode: "gemini-system-settings",
  nativeToolMode: "always-on",
  config: {
    command: "/abs/agy",
    args: ["--dangerously-skip-permissions", "--disable-slash-commands",
           "--output-format", "stream-json", "-p", "{prompt}"],
    resumeArgs: ["--dangerously-skip-permissions", "--disable-slash-commands",
                 "--conversation", "{sessionId}", "--output-format", "stream-json", "-p", "{prompt}"],
    output: "jsonl", resumeOutput: "jsonl", input: "arg", modelArg: "--model",
    sessionMode: "existing", sessionIdFields: ["conversation_id"], serialize: false,
  },
};

test("buildAgyBackend deep-equals the pre-phase-3 snapshot", () => {
  const { parseJsonlEvent, ...rest } = buildAgyBackend({ agy: "/abs/agy" });
  assert.equal(parseJsonlEvent, parseAgyLine);
  assert.deepEqual(rest, AGY_CLI_SNAPSHOT);
});

test("buildAgyLaneBackend has its own id, no bundled MCP and the same parser, model arg and serialisation", () => {
  const lane = buildAgyLaneBackend({ agy: "/abs/agy" });
  const classic = buildAgyBackend({ agy: "/abs/agy" });
  assert.equal(lane.id, "agy-cli-lane");
  assert.equal(lane.bundleMcp, false);
  assert.equal(lane.bundleMcpMode, undefined);
  assert.equal(lane.parseJsonlEvent, parseAgyLine);
  assert.deepEqual(lane.config, classic.config);
  assert.notEqual(lane.config, classic.config);       // a copy: editing one never edits the other
  assert.equal(lane.config.modelArg, "--model");
  assert.equal(lane.config.serialize, false);
});

test("the lane argv carries no sandbox, allowlist or agy-home argument: V6, V7 and V12 are not proven", () => {
  const lane = buildAgyLaneBackend({ agy: "/abs/agy" });
  assert.ok(lane.config.args.includes("--dangerously-skip-permissions"));
  for (const a of lane.config.args) {
    assert.ok(!/sandbox|allow|home/i.test(a), `unexpected argument ${a}`);
  }
  assert.equal(lane.config.env, undefined);
  assert.deepEqual(Object.keys(AGY_LANE_UNPROVEN).sort(), ["agyHome", "sandbox", "toolAllowlist"]);
});

test("buildAgyBackend defaults serialize to false, but honors serializeAgy/serialize if explicitly true", () => {
  const backendDefault = buildAgyBackend({ agy: "/abs/agy" });
  assert.equal(backendDefault.config.serialize, false);

  const backendSerializeAgy = buildAgyBackend({ agy: "/abs/agy" }, { serializeAgy: true });
  assert.equal(backendSerializeAgy.config.serialize, true);

  const backendSerialize = buildAgyBackend({ agy: "/abs/agy" }, { serialize: true });
  assert.equal(backendSerialize.config.serialize, true);
});

test("register() passes serializeAgy from pluginConfig to agy-cli backends", () => {
  const backends = [];
  plugin.register({
    pluginConfig: { agyBin: "/abs/agy", serializeAgy: true },
    registerCliBackend(d) { backends.push(d); },
    registerCommand() {},
  });
  assert.equal(backends.find((b) => b.id === "agy-cli").config.serialize, true);
  assert.equal(backends.find((b) => b.id === "agy-cli-lane").config.serialize, true);
});

test("the plugin manifest declares serializeAgy in configSchema", () => {
  const manifestPath = path.join(HERE, "..", "openclaw.plugin.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
  assert.equal(manifest.configSchema?.properties?.serializeAgy?.type, "boolean");
});

test("isAbsoluteBin accepts only absolute paths", () => {
  assert.equal(isAbsoluteBin("/opt/agy"), true);
  for (const bad of ["agy", "./agy", "", undefined, null]) assert.equal(isAbsoluteBin(bad), false);
});

test("a relative or missing agyBin makes the lane backend refuse to register; the classic one keeps agy", () => {
  for (const pluginConfig of [{}, { agyBin: "agy" }, { agyBin: "./agy" }]) {
    const backends = [];
    plugin.register({ pluginConfig, registerCliBackend(d) { backends.push(d); }, registerCommand() {} });
    assert.deepEqual(backends.map((b) => b.id).sort(), ["agy-cli", "claude-kit"]);
    assert.equal(backends.find((b) => b.id === "agy-cli").config.command, pluginConfig.agyBin || "agy");
  }
});

// --- /lanes status cockpit command (ADR-0012) ---------------------------------------

test("the plugin manifest declares kitRoot, lanesConfigPath and enableOrchestrator in configSchema", () => {
  const manifestPath = path.join(HERE, "..", "openclaw.plugin.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
  assert.equal(manifest.configSchema?.properties?.kitRoot?.type, "string");
  assert.equal(manifest.configSchema?.properties?.lanesConfigPath?.type, "string");
  assert.equal(manifest.configSchema?.properties?.enableOrchestrator?.type, "boolean");
});

test("buildLanesCommand matches OpenClawPluginCommandDefinition contract", () => {
  const handler = async () => ({ text: "lanes" });
  const cmd = buildLanesCommand("lanes", handler);
  assert.equal(cmd.name, "lanes");
  assert.equal(typeof cmd.description, "string");
  assert.ok(cmd.description.length > 0);
  assert.equal(cmd.acceptsArgs, true);
  assert.equal(cmd.requireAuth, true);
  assert.equal(cmd.handler, handler);
});

const MATRIX_SAMPLE = `mode: single-model
generated_from: scoring.routing
routing:
  matrix:
    planner:
      class: team-write
      primary:
        cell: claude-code:sonnet
        pool: claude-subscription
      fallback:
      - cell: agy:gemini-4-argon-high
        pool: agy-gemini
    explore:
      class: team-read
      primary:
        cell: agy:gemini-3.8-flash-high
        pool: agy-gemini
      fallback:
      - cell: claude-code:haiku
        pool: claude-subscription
      - cell: anthropic-api:claude-haiku-5-5
        pool: anthropic-api
  balancing:
    spill_at: 0.85
`;

test("parseLanesSummary parses mode, primary and the ordered fallbacks of the role matrix", () => {
  const summary = parseLanesSummary(MATRIX_SAMPLE);
  assert.equal(summary.mode, "single-model");
  assert.deepEqual(Object.keys(summary.roles), ["planner", "explore"]);
  assert.equal(summary.roles.planner.klass, "team-write");
  assert.equal(summary.roles.planner.primary, "claude-code:sonnet");
  assert.deepEqual(summary.roles.planner.fallbacks, ["agy:gemini-4-argon-high"]);
  assert.equal(summary.roles.explore.primary, "agy:gemini-3.8-flash-high");
  assert.deepEqual(summary.roles.explore.fallbacks, ["claude-code:haiku", "anthropic-api:claude-haiku-5-5"]);
});

test("parseLanesSummary ignores the old resolved-lanes shape", () => {
  assert.deepEqual(parseLanesSummary("mode: single-model\nresolved:\n  roles:\n    planner:\n      lane: claude-code\n").roles, {});
});

test("formatLanesMarkdown renders a Role | Primary | Fallback table", () => {
  const md = formatLanesMarkdown(parseLanesSummary(MATRIX_SAMPLE), "/fake/lanes.yaml");
  assert.match(md, /### 🚦 ai-resources: Role matrix/);
  assert.match(md, /\*\*Mode:\*\* `single-model`/);
  assert.match(md, /\|\s+\*\*planner\*\*\s+\|\s+`claude-code:sonnet`\s+\|\s+`agy:gemini-4-argon-high`\s+\|/);
  assert.match(md, /\|\s+\*\*explore\*\*\s+\|\s+`agy:gemini-3.8-flash-high`\s+\|\s+`claude-code:haiku`, `anthropic-api:claude-haiku-5-5`\s+\|/);
});

test("makeLanesCommandHandler returns message when lanes file does not exist", async () => {
  const handler = makeLanesCommandHandler({ pluginConfig: { lanesConfigPath: "/nonexistent/lanes.yaml" } });
  const res = await handler({});
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /No configuration found at `/);
});

test("makeLanesCommandHandler reads custom lanesConfigPath if provided", async () => {
  const fsModule = await import("node:fs");
  const tmpPath = path.join(HERE, "fixtures", "tmp-lanes.yaml");
  const yamlContent = MATRIX_SAMPLE.replace("single-model", "multi-model");
  fsModule.writeFileSync(tmpPath, yamlContent, "utf8");
  try {
    const handler = makeLanesCommandHandler({ pluginConfig: { lanesConfigPath: tmpPath } });
    const res = await handler({});
    assert.equal(res.continueAgent, false);
    assert.match(res.text, /Role matrix/);
    assert.match(res.text, /multi-model/);
    assert.match(res.text, /planner/);
  } finally {
    if (fsModule.existsSync(tmpPath)) fsModule.unlinkSync(tmpPath);
  }
});

// --- /workflow cockpit commands (ADR-0012) -----------------------------------------

const REPO_ROOT = path.resolve(HERE, "../../..");

test("resolveKitRoot falls back to repo root when workflows directory exists", () => {
  const root = resolveKitRoot({}, path.resolve(HERE, ".."));
  assert.ok(typeof root === "string");
  assert.ok(root.length > 0);
  assert.ok(root.endsWith("ai-resources"));
});

test("parseWorkflowHeader extracts id, name, description, trigger, and steps", () => {
  const yaml = `
name: test-flow
description: Test workflow for unit testing
trigger: When tests run
steps:
  - id: step-one
    subagent_type: planner
    handoff_to: step-two
    entry_criteria: Start
    exit_criteria: Done
  - id: step-two
    subagent_type: implementer
    handoff_to: null
`;
  const f = path.join(HERE, "fixtures", "tmp-workflow.yaml");
  writeFileSync(f, yaml, "utf8");
  try {
    const parsed = parseWorkflowHeader(f);
    assert.equal(parsed.id, "tmp-workflow");
    assert.equal(parsed.name, "test-flow");
    assert.equal(parsed.description, "Test workflow for unit testing");
    assert.equal(parsed.trigger, "When tests run");
    assert.equal(parsed.steps.length, 2);
    assert.equal(parsed.steps[0].id, "step-one");
    assert.equal(parsed.steps[0].role, "planner");
    assert.equal(parsed.steps[0].to, "step-two");
    assert.equal(parsed.steps[1].id, "step-two");
    assert.equal(parsed.steps[1].role, "implementer");
    assert.equal(parsed.steps[1].to, "null");
  } finally {
    if (existsSync(f)) unlinkSync(f);
  }
});

test("buildWorkflowCommand matches OpenClawPluginCommandDefinition contract", () => {
  const handler = async () => ({ text: "workflow" });
  const cmd = buildWorkflowCommand("workflow", handler);
  assert.equal(cmd.name, "workflow");
  assert.equal(typeof cmd.description, "string");
  assert.ok(cmd.description.length > 0);
  assert.equal(cmd.acceptsArgs, true);
  assert.equal(cmd.requireAuth, true);
  assert.equal(cmd.handler, handler);
});

test("makeWorkflowCommandHandler list returns formatted table of workflows", async () => {
  const handler = makeWorkflowCommandHandler({ pluginConfig: { kitRoot: REPO_ROOT } });
  const res = await handler({ args: "list" });
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /### 📋 ai-resources: Available Workflows/);
  assert.match(res.text, /explore-and-plan/);
  assert.match(res.text, /feature-cross-provider/);
});

test("makeWorkflowCommandHandler info displays detailed pipeline for valid workflow", async () => {
  const handler = makeWorkflowCommandHandler({ pluginConfig: { kitRoot: REPO_ROOT } });
  const res = await handler({ args: "info explore-and-plan" });
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /### 📋 Workflow: `explore-and-plan`/);
  assert.match(res.text, /Pipeline Steps/);
  assert.match(res.text, /explore/);
  assert.match(res.text, /plan/);
});

test("makeWorkflowCommandHandler info returns error for unknown workflow", async () => {
  const handler = makeWorkflowCommandHandler({ pluginConfig: { kitRoot: REPO_ROOT } });
  const res = await handler({ args: "info non-existent-flow" });
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /⚠️ \*\*Workflow not found\*\*/);
});

test("makeWorkflowCommandHandler run delegates to runtime.subagent when available", async () => {
  const dispatched = [];
  const api = {
    pluginConfig: { kitRoot: REPO_ROOT },
    runtime: {
      subagent: {
        async run(opts) {
          dispatched.push(opts);
          return { sessionKey: "agent:claude:subagent:wf-test" };
        },
      },
    },
  };
  const handler = makeWorkflowCommandHandler(api);
  const res = await handler({ args: "run explore-and-plan Refactor authentication tokens" });
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /🚀 \*\*Workflow Started\*\*: `explore-and-plan`/);
  assert.match(res.text, /wf-test/);
  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0].message, /Executing workflow explore-and-plan/);
  assert.match(dispatched[0].message, /Refactor authentication tokens/);
});

// --- /equipo team lifecycle command (ADR-0012) --------------------------------------

test("buildEquipoCommand matches OpenClawPluginCommandDefinition contract", () => {
  const handler = async () => ({ text: "equipo" });
  const cmd = buildEquipoCommand("equipo", handler);
  assert.equal(cmd.name, "equipo");
  assert.equal(typeof cmd.description, "string");
  assert.ok(cmd.description.length > 0);
  assert.equal(cmd.acceptsArgs, true);
  assert.equal(cmd.requireAuth, true);
  assert.equal(cmd.handler, handler);
});

test("makeEquipoCommandHandler formats team protocol and delivers to runtime.subagent", async () => {
  const calls = [];
  const api = {
    runtime: {
      subagent: {
        async run(opts) {
          calls.push(opts);
          return { sessionKey: "agent:claude:subagent:eq-123" };
        },
      },
    },
  };
  const handler = makeEquipoCommandHandler(api);
  const res = await handler({ args: "Build full metrics dashboard" });
  assert.equal(res.continueAgent, false);
  assert.match(res.text, /👥 \*\*Team Lifecycle Dispatched\*\*/);
  assert.match(res.text, /Build full metrics dashboard/);
  assert.match(res.text, /`planner` ➔ `implementer` ➔ `verifier`/);
  assert.equal(calls.length, 1);
  assert.match(calls[0].message, /\[AI-RESOURCES TEAM LIFECYCLE: ADR-0012\]/);
  assert.match(calls[0].message, /1\. PLANNER:/);
  assert.match(calls[0].message, /2\. IMPLEMENTER:.*\(Self-approval is forbidden\)/);
  assert.match(calls[0].message, /3\. VERIFIER:/);
});

test("makeEquipoCommandHandler throws a clear error when runtime.subagent is unavailable", async () => {
  const handler = makeEquipoCommandHandler({});
  await assert.rejects(
    () => handler({ args: "Build something" }),
    /runtime.subagent.run is not available/,
  );
});
