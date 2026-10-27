import { test } from "node:test";
import assert from "node:assert/strict";

import plugin, {
  makeWorkflowCommandHandler,
  makeEquipoCommandHandler,
  orchestratorFor,
  splitRepoFlag,
  EQUIPO_WORKFLOW,
} from "../index.js";
import {
  createOrchestrator,
  engineCommand,
  engineOptions,
  stepSessionKey,
  lastAssistantText,
  formatActions,
  formatStatus,
  RUN_ID_RE,
} from "../orchestrator.js";

// --- helpers: a scripted engine behind a stub execFile, a stub runtime and manual timers -------------------------------

function dispatch(over = {}) {
  return {
    type: "dispatch", run: "r1", step: "plan", role: "planner", agent: "kit-role-planner", attempt: 1,
    cwd: "/wt", timeout_s: 1200, parallel: false, handoff_file: "/h", prompt: "PROMPT plan", ...over,
  };
}

function makeEngine(handlers) {
  const calls = [];
  function execFile(file, args, opts, cb) {
    const call = { file, args: [...args], opts, stdin: undefined };
    calls.push(call);
    const verb = args[args.indexOf("orchestrate") + 1];
    const child = { stdin: { end(data) { call.stdin = data; } } };
    queueMicrotask(() => {
      try {
        const reply = handlers[verb](call);
        cb(null, JSON.stringify(reply), "");
      } catch (e) {
        cb(e, "", "");
      }
    });
    return child;
  }
  return { execFile, calls, verbs: () => calls.map((c) => c.args[c.args.indexOf("orchestrate") + 1]) };
}

function makeTimers() {
  const timers = [];
  let nextId = 1;
  return {
    timers,
    setTimeout(fn, ms) { const t = { id: nextId++, fn, ms, cleared: false }; timers.push(t); return t.id; },
    clearTimeout(id) { const t = timers.find((x) => x.id === id); if (t) t.cleared = true; },
  };
}

function makeApi({ pluginConfig = {}, runs = [], waits = [], messages = [] } = {}) {
  const started = [];
  const deleted = [];
  const waitCalls = [];
  const notices = [];
  const api = {
    pluginConfig: { enableOrchestrator: true, kitRoot: "/kit", python3Bin: "/py/python3", ...pluginConfig },
    logger: { info() {}, warn() {} },
    registered: [],
    runtime: {
      subagent: {
        async run(req) {
          started.push(req);
          const next = runs.shift();
          if (next instanceof Error) throw next;
          return next || { runId: `run-${started.length}`, sessionKey: req.sessionKey };
        },
        async waitForRun(req) {
          waitCalls.push(req);
          const w = waits.shift();
          return typeof w === "function" ? w(req) : (w || { status: "ok", terminalReply: "Status: success\nBlocked: false\n" });
        },
        async getSessionMessages() { return { messages }; },
        async deleteSession(req) { deleted.push(req.sessionKey); },
      },
    },
    registerCliBackend() {},
    registerCommand(c) { api.registered.push(c); },
  };
  return { api, started, deleted, waitCalls, notices };
}

async function settle() {
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r));
}

// --- pure helpers ------------------------------------------------------------------------------------------------------

test("engineCommand prefers the explicit binary, then python + kitRoot, then the bare name", () => {
  assert.deepEqual(engineCommand({ aiResourcesBin: "/opt/ai-resources" }), { file: "/opt/ai-resources", prefix: [] });
  assert.deepEqual(engineCommand({ python3: "/p/python3", kitRoot: "/kit" }), { file: "/p/python3", prefix: ["/kit/scripts/kit.py"] });
  assert.deepEqual(engineCommand({}), { file: "ai-resources", prefix: [] });
});

test("engineOptions clamps maxTeams and drops invalid role timeouts", () => {
  assert.equal(engineOptions({}).maxTeams, 16);
  assert.equal(engineOptions({ maxTeams: 99 }).maxTeams, 16);
  assert.equal(engineOptions({ maxTeams: 0 }).maxTeams, 1);
  assert.deepEqual(engineOptions({ roleTimeouts: { planner: 600, bad: 5, worse: "x", huge: 10 ** 9 } }).roleTimeouts, { planner: 600 });
});

test("session keys are per attempt and contain only safe characters", () => {
  assert.equal(stepSessionKey(dispatch()), "agent:kit-role-planner:subagent:r1-plan-a1");
  assert.match(stepSessionKey(dispatch({ run: "r 1/../x", step: "a b", attempt: 3 })), /^agent:kit-role-planner:subagent:[A-Za-z0-9_-]+-[A-Za-z0-9_-]+-a3$/);
});

test("lastAssistantText reads the last assistant message in several shapes", () => {
  assert.equal(lastAssistantText([{ role: "user", content: "hi" }, { role: "assistant", content: "done" }]), "done");
  assert.equal(lastAssistantText([{ message: { role: "assistant", content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] } }]), "a\nb");
  assert.equal(lastAssistantText([{ role: "assistant", content: "x" }, { role: "user", content: "later" }]), "x");
  assert.equal(lastAssistantText(null), "");
  assert.equal(lastAssistantText([]), "");
});

test("formatActions and formatStatus render runs, steps, queue notices and the approval hint", () => {
  assert.match(formatActions({ run: "r1", status: "running", actions: [dispatch()], promoted: ["r2"] }), /`plan` → `kit-role-planner`.*Started from the queue: `r2`/s);
  assert.match(formatActions({ run: "r1", status: "queued", actions: [{ type: "notice", text: "queued: 2 team(s) are working" }] }), /queued: 2 team/);
  const doc = { id: "r1", workflow: "w", status: "awaiting_approval", approval: { state: "pending" }, steps: { plan: { role: "planner", state: "done", attempt: 1, result: "ok" } } };
  assert.match(formatStatus(doc), /\/workflow approve r1/);
  assert.match(formatStatus({ runs: [{ id: "r1", workflow: "w", status: "running", step: "plan", done: 0, total: 3 }], active: 1, max_teams: 2, queued: [] }), /1 active of 2/);
  assert.ok(RUN_ID_RE.test("20261023-120000-feature-ab12") && !RUN_ID_RE.test("../x") && !RUN_ID_RE.test("a b"));
});

test("splitRepoFlag removes --repo from the goal", () => {
  assert.deepEqual(splitRepoFlag("add a thing --repo /srv/app now"), { goal: "add a thing now", workspace: "/srv/app" });
  assert.deepEqual(splitRepoFlag("just a goal"), { goal: "just a goal", workspace: "" });
});

// --- the dispatch cycle -------------------------------------------------------------------------------------------------

test("start runs the engine through execFile with an argv array and dispatches the first step to its role agent", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch()] }),
    report: () => ({ run: "r1", status: "running", actions: [{ type: "wait", step: "plan" }] }),
  });
  const timers = makeTimers();
  const { api, started, waitCalls } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout };
  const orch = orchestratorFor(api);
  const goal = "add a thing; rm -rf / && echo $(whoami) `id` -- --weird";
  const result = await orch.start("feature-implementation", goal, { sessionKey: "agent:main:telegram:1" }, { workspace: "/srv/app" });
  await settle();

  const startCall = eng.calls[0];
  assert.equal(startCall.file, "/py/python3");
  assert.deepEqual(startCall.args.slice(0, 3), ["/kit/scripts/kit.py", "orchestrate", "start"]);
  assert.ok(startCall.args.includes(`--goal=${goal}`), "the goal is ONE argv element, never a shell string");
  assert.ok(startCall.args.includes("--workflow=feature-implementation") && startCall.args.includes("--workspace=/srv/app"));
  assert.ok(startCall.args.includes("--max-teams=16") && startCall.args.at(-1) === "--json");
  assert.equal(result.run, "r1");

  assert.equal(started.length, 1);
  assert.deepEqual(started[0], { sessionKey: "agent:kit-role-planner:subagent:r1-plan-a1", message: "PROMPT plan", deliver: true });
  assert.equal(waitCalls.length, 1, "ONE blocking wait per attempt, never a poll loop");
  assert.equal(waitCalls[0].timeoutMs, 1200 * 1000);

  const report = eng.calls.find((c) => c.args.includes("report"));
  assert.deepEqual(report.args.slice(2, 5), ["report", "r1", "plan"]);
  assert.ok(report.args.includes("--session=agent:kit-role-planner:subagent:r1-plan-a1"));
  assert.ok(report.args.includes("--agent=kit-role-planner") && report.args.includes("--reply-file=-"));
  assert.match(report.stdin, /Status: success/);
  assert.equal(timers.timers.length, 1);
  assert.equal(timers.timers[0].cleared, true, "the role timer is cleared when the step finishes");
});

test("a checker's per-run model reaches subagent.run as the model override; a role without one gets none", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [
      dispatch({ step: "verify", role: "verifier", agent: "kit-role-verifier", model: "anthropic/claude-opus-5-5", parallel: true }),
      dispatch({ step: "plan", parallel: true })] }),
    report: () => ({ run: "r1", status: "running", actions: [{ type: "wait", step: "plan" }] }),
  });
  const { api, started } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  await orchestratorFor(api).start("w", "g", {});
  await settle();
  const verify = started.find((s) => s.sessionKey.includes("kit-role-verifier"));
  const plan = started.find((s) => s.sessionKey.includes("kit-role-planner"));
  assert.equal(verify.model, "anthropic/claude-opus-5-5");
  assert.equal("model" in plan, false, "OpenClaw's own primary and model.fallbacks apply to a role with a fixed primary");
});

test("a finished step reports and the next dispatch of the returned actions is started", async () => {
  let reports = 0;
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch()] }),
    report: () => {
      reports += 1;
      return reports === 1
        ? { run: "r1", status: "running", actions: [dispatch({ step: "implement", role: "implementer", agent: "kit-role-implementer", prompt: "PROMPT impl", timeout_s: 9000 })] }
        : { run: "r1", status: "completed", actions: [{ type: "done", run: "r1", text: "every step is done" }] };
    },
  });
  const { api, started, waitCalls } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const orch = orchestratorFor(api);
  const noticesBefore = orch.notices.length;
  await orch.start("w", "g", {});
  await settle();
  assert.deepEqual(started.map((s) => s.sessionKey), ["agent:kit-role-planner:subagent:r1-plan-a1", "agent:kit-role-implementer:subagent:r1-implement-a1"]);
  assert.equal(waitCalls[1].timeoutMs, 9000 * 1000);
  assert.equal(orch.notices.length - noticesBefore, 1);
  assert.match(orch.notices.at(-1).text, /every step is done/);
});

test("parallel members are all dispatched before any of them is awaited", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch({ step: "review", role: "code-reviewer", agent: "kit-role-reviewer", parallel: true }), dispatch({ step: "security", role: "security-auditor", agent: "kit-role-security", parallel: true })] }),
    report: () => ({ run: "r1", status: "running", actions: [{ type: "wait" }] }),
  });
  const { api, started } = makeApi({ waits: [() => new Promise(() => {}), () => new Promise(() => {})] });
  api.orchestratorDeps = { execFile: eng.execFile };
  await orchestratorFor(api).start("w", "g", {});
  await settle();
  assert.equal(started.length, 2);
  assert.equal(eng.verbs().includes("report"), false, "nothing reported yet: both are still running");
});

test("the role timer fires: the child session is ended and the engine hears --timeout", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch({ timeout_s: 600 })] }),
    report: () => ({ run: "r1", status: "running", actions: [{ type: "wait" }] }),
  });
  const timers = makeTimers();
  const { api, deleted } = makeApi({ waits: [() => new Promise(() => {})] });
  api.orchestratorDeps = { execFile: eng.execFile, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout };
  await orchestratorFor(api).start("w", "g", {});
  await settle();
  assert.equal(timers.timers.length, 1);
  assert.equal(timers.timers[0].ms, 600 * 1000);
  timers.timers[0].fn();                                   // the fake clock reaches the deadline
  await settle();
  const report = eng.calls.find((c) => c.args.includes("report"));
  assert.ok(report.args.includes("--timeout") && report.args.includes("--agent=kit-role-planner"));
  assert.deepEqual(deleted, ["agent:kit-role-planner:subagent:r1-plan-a1"]);
});

test("a failing run() or an errored run reports --error and the engine decides what follows", async () => {
  let n = 0;
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch()] }),
    report: () => {
      n += 1;
      return n === 1 ? { run: "r1", status: "running", actions: [dispatch({ attempt: 2 })] } : { run: "r1", status: "failed", actions: [{ type: "failed", run: "r1", text: "retry cap" }] };
    },
  });
  const { api } = makeApi({ runs: [new Error("agent kit-role-planner is not allowed")], waits: [{ status: "error", error: "model overloaded" }] });
  api.orchestratorDeps = { execFile: eng.execFile };
  const orch = orchestratorFor(api);
  await orch.start("w", "g", {});
  await settle();
  const reports = eng.calls.filter((c) => c.args.includes("report"));
  assert.ok(reports[0].args.includes("--error=agent kit-role-planner is not allowed"));
  assert.ok(reports[1].args.includes("--error=model overloaded"));
  assert.match(orch.notices.at(-1).text, /retry cap/);
});

test("the reply comes from getSessionMessages when the wait result carries no terminal reply", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch()] }),
    report: () => ({ run: "r1", status: "running", actions: [] }),
  });
  const { api } = makeApi({ waits: [{ status: "ok" }], messages: [{ role: "assistant", content: "Blocked: false\nVerdict: PASS" }] });
  api.orchestratorDeps = { execFile: eng.execFile };
  await orchestratorFor(api).start("w", "g", {});
  await settle();
  assert.match(eng.calls.find((c) => c.args.includes("report")).stdin, /Verdict: PASS/);
});

// --- plan approval, the queue, resume -----------------------------------------------------------------------------------

test("the plan approval question is posted and /workflow approve resumes the run", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r1", status: "running", actions: [dispatch()] }),
    report: () => ({ run: "r1", status: "awaiting_approval", actions: [{ type: "ask", kind: "approval", run: "r1", step: "implement", text: "The plan is ready. Approve it to let implement start writing.", options: ["approve", "cancel"] }] }),
    approve: () => ({ run: "r1", status: "running", actions: [dispatch({ step: "implement", role: "implementer", agent: "kit-role-implementer", prompt: "PROMPT impl", timeout_s: 9000 })] }),
  });
  const { api, started } = makeApi({ waits: [{ status: "ok", terminalReply: "Status: success" }, () => new Promise(() => {})] });
  api.orchestratorDeps = { execFile: eng.execFile };
  const orch = orchestratorFor(api);
  const handler = makeWorkflowCommandHandler(api);
  await handler({ args: "run feature-implementation add a thing", sessionKey: "s" });
  await settle();
  assert.match(orch.notices.at(-1).text, /Approve it/);
  assert.equal(started.length, 1);
  const res = await handler({ args: "approve r1" });
  await settle();
  assert.match(res.text, /`implement` → `kit-role-implementer`/);
  assert.equal(started.length, 2);
});

test("a queued team gets an in-thread notice, and a promoted one is started when a slot frees", async () => {
  const eng = makeEngine({
    start: () => ({ run: "r3", status: "queued", actions: [{ type: "notice", kind: "queued", run: "r3", text: "queued: 2 team(s) are working; this one starts when a slot frees (0 run(s) ahead of it in the queue)" }] }),
    cancel: () => ({ run: "r1", status: "cancelled", actions: [], promoted: ["r3"] }),
    next: (call) => ({ run: call.args[call.args.indexOf("next") + 1], status: "running", actions: [dispatch({ run: "r3" })] }),
    report: () => ({ run: "r3", status: "running", actions: [] }),
  });
  const { api, started } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const orch = orchestratorFor(api);
  const handler = makeWorkflowCommandHandler(api);
  const res = await handler({ args: "run feature-implementation third goal" });
  assert.match(res.text, /queued: 2 team\(s\) are working/);
  assert.match(orch.notices.at(-1).text, /queued/);
  assert.equal(started.length, 0);
  const cancelled = await handler({ args: "cancel r1" });
  await settle();
  assert.match(cancelled.text, /Started from the queue: `r3`/);
  assert.equal(started.length, 1);
  assert.match(started[0].sessionKey, /r3-plan-a1/);
});

test("register() resumes interrupted runs once, and not at all when the orchestrator is off", async () => {
  const eng = makeEngine({
    resume: () => ({ runs: [{ run: "r1", status: "running", resumed: ["plan"], actions: [dispatch()] }] }),
    report: () => ({ run: "r1", status: "running", actions: [] }),
  });
  const on = makeApi();
  on.api.orchestratorDeps = { execFile: eng.execFile };
  plugin.register(on.api);
  await settle();
  assert.deepEqual(eng.verbs().filter((v) => v === "resume"), ["resume"]);
  assert.ok(eng.calls[0].args.includes("--all"));
  assert.equal(on.started.length, 1);

  const off = makeApi({ pluginConfig: { enableOrchestrator: false } });
  const eng2 = makeEngine({});
  off.api.orchestratorDeps = { execFile: eng2.execFile };
  plugin.register(off.api);
  await settle();
  assert.equal(eng2.calls.length, 0);
  assert.equal(orchestratorFor(off.api), null);
});

test("resumeAll never throws when the engine is unavailable", async () => {
  const api = makeApi().api;
  api.orchestratorDeps = { execFile: (f, a, o, cb) => { queueMicrotask(() => cb(new Error("ENOENT"), "", "")); return { stdin: { end() {} } }; } };
  const orch = orchestratorFor(api);
  assert.equal(await orch.resumeAll(), null);
});

// --- command surface ----------------------------------------------------------------------------------------------------

test("/equipo uses the feature workflow through the orchestrator and /workflow status renders the table", async () => {
  const eng = makeEngine({
    start: (call) => ({ run: "r1", status: "running", actions: [dispatch()], _args: call.args }),
    report: () => ({ run: "r1", status: "running", actions: [] }),
    status: () => ({ id: "r1", workflow: "feature-implementation", status: "running", steps: { plan: { role: "planner", state: "running", attempt: 1, result: "" } }, approval: { state: "pending" } }),
  });
  const { api } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const equipo = makeEquipoCommandHandler(api);
  const res = await equipo({ args: "add rate limiting --repo /srv/app", sessionKey: "s" });
  assert.match(res.text, /Team started/);
  const start = eng.calls[0].args;
  assert.ok(start.includes(`--workflow=${EQUIPO_WORKFLOW}`) && start.includes("--goal=add rate limiting") && start.includes("--workspace=/srv/app"));
  const status = await makeWorkflowCommandHandler(api)({ args: "status r1" });
  assert.match(status.text, /\*\*Run\*\* `r1`/);
});

test("invalid run ids never reach the engine and usage is explained", async () => {
  const eng = makeEngine({});
  const { api } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const handler = makeWorkflowCommandHandler(api);
  for (const args of ["approve", "approve ../x", "cancel a/b", "cost -x"]) {
    const res = await handler({ args });
    assert.match(res.text, /Usage/);
  }
  assert.equal(eng.calls.length, 0);
  const bad = await handler({ args: "run bad/workflow goal" });
  assert.match(bad.text, /invalid workflow id/);
});

test("an engine failure is reported to the operator as text, never thrown", async () => {
  const eng = makeEngine({ approve: () => { throw new Error("spawn ENOENT"); } });
  const { api } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const res = await makeWorkflowCommandHandler(api)({ args: "approve r1" });
  assert.match(res.text, /Orchestrator/);
});

test("an engine refusal keeps its code and message", async () => {
  const eng = makeEngine({ approve: () => ({ error: "this run has no plan waiting for approval", code: "no-approval" }) });
  const { api } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const orch = orchestratorFor(api);
  await assert.rejects(() => orch.approve("r1"), (e) => e.code === "no-approval" && /no plan waiting/.test(e.message));
});

test("with the orchestrator off /workflow run and /equipo keep their previous behaviour and call no engine", async () => {
  const eng = makeEngine({});
  const { api, started } = makeApi({ pluginConfig: { enableOrchestrator: false } });
  api.orchestratorDeps = { execFile: eng.execFile };
  const res = await makeEquipoCommandHandler(api)({ args: "do it", sessionKey: "s" });
  assert.match(res.text, /Team Lifecycle Dispatched/);
  assert.equal(started.length, 1);
  assert.equal(eng.calls.length, 0);
});

test("cost renders the per-role table from the engine", async () => {
  const eng = makeEngine({ cost: () => ({ label: "USD API-equivalent estimate", total: { calls: 3, input: 30, output: 9, usd: 0.12 }, by_role: { planner: { calls: 3, input: 30, output: 9, usd: 0.12 } } }) });
  const { api } = makeApi();
  api.orchestratorDeps = { execFile: eng.execFile };
  const res = await makeWorkflowCommandHandler(api)({ args: "cost r1" });
  assert.match(res.text, /\| planner \| 3 \| 30 \| 9 \| 0.12 \|/);
  assert.match(res.text, /API-equivalent/);
});

test("the plugin manifest declares the orchestrator options and stays strict", async () => {
  const { readFileSync } = await import("node:fs");
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  const props = manifest.configSchema.properties;
  for (const key of ["enableOrchestrator", "maxTeams", "roleTimeouts", "runsDir", "aiResourcesBin"]) assert.ok(props[key], key);
  assert.equal(manifest.configSchema.additionalProperties, false);
  assert.equal(props.maxTeams.maximum, 16);
  assert.ok(!("spendCapUsd" in props), "there is no spend cap (ADR-0015)");
});
