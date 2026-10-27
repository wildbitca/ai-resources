import { execFile as nodeExecFile } from "node:child_process";
import path from "node:path";

// Native team orchestrator, plugin side (ADR-0015 D3).
//
// The engine is `ai-resources orchestrate <verb> --json` (Python): a pure, resumable state machine that NEVER dispatches.
// This module is the thin runtime around it:
//
//   - `start` / `next` / `report` / `approve` / `cancel` / `resume` / `status` are run through `execFile` with an argv ARRAY
//     (no shell, no string concatenation of user text into a command line);
//   - a `dispatch` action becomes `api.runtime.subagent.run({ sessionKey, message, deliver: true })` on the
//     `kit-role-<role>` agent the action names; ONE blocking `waitForRun` per attempt (never a poll loop) races a per-role
//     timer; the outcome goes back with `orchestrate report`, which returns the next actions;
//   - an `ask` (plan approval) and a `notice` (queued behind maxTeams) are posted to the requester; the operator answers
//     with `/workflow approve <run>`;
//   - on plugin load `resume` hands out again the steps a gateway restart interrupted.
//
// The plugin never restarts anything and never touches the gateway config.

export const RUN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
export const WORKFLOW_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
export const ENGINE_TIMEOUT_MS = 120_000;
const MAX_REPLY_CHARS = 200_000;

/** The command that runs the engine: an explicit `aiResourcesBin`, else `python3 <kitRoot>/scripts/kit.py`. */
export function engineCommand({ aiResourcesBin = "", python3 = "python3", kitRoot = "" } = {}) {
  if (aiResourcesBin) return { file: aiResourcesBin, prefix: [] };
  if (kitRoot) return { file: python3, prefix: [path.join(kitRoot, "scripts", "kit.py")] };
  return { file: "ai-resources", prefix: [] };
}

/** Plain-object clamp of the plugin config values the engine receives. */
export function engineOptions(pluginConfig = {}) {
  const maxTeams = Number.isInteger(pluginConfig.maxTeams) ? Math.min(Math.max(pluginConfig.maxTeams, 1), 16) : 2;
  const timeouts = {};
  for (const [role, secs] of Object.entries(pluginConfig.roleTimeouts || {})) {
    if (Number.isInteger(secs) && secs >= 30 && secs <= 86400) timeouts[role] = secs;
  }
  return { maxTeams, roleTimeouts: timeouts, runsDir: typeof pluginConfig.runsDir === "string" ? pluginConfig.runsDir : "" };
}

function safeKeyPart(text) {
  return String(text).replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 60);
}

/** Session key of one attempt of one step: a fresh subagent session on the role agent. */
export function stepSessionKey(action) {
  return `agent:${safeKeyPart(action.agent)}:subagent:${safeKeyPart(action.run)}-${safeKeyPart(action.step)}-a${Number(action.attempt) || 1}`;
}

/** The text of the last assistant message of a session-messages result (several shapes are tolerated). */
export function lastAssistantText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i] || {};
    const msg = m.message && typeof m.message === "object" ? m.message : m;
    if (msg.role && msg.role !== "assistant") continue;
    const content = msg.content;
    if (typeof content === "string" && content.trim()) return content;
    if (Array.isArray(content)) {
      const text = content.filter((c) => c && c.type === "text" && typeof c.text === "string").map((c) => c.text).join("\n");
      if (text.trim()) return text;
    }
    if (typeof msg.text === "string" && msg.text.trim()) return msg.text;
  }
  return "";
}

export function formatActions(result) {
  const lines = [];
  if (result.run) lines.push(`Run \`${result.run}\` is **${result.status}**.`);
  for (const a of result.actions || []) {
    if (a.type === "notice" || a.type === "ask" || a.type === "done" || a.type === "failed" || a.type === "cancelled") {
      lines.push(a.text);
    } else if (a.type === "dispatch") {
      lines.push(`- \`${a.step}\` → \`${a.agent}\` (attempt ${a.attempt}, up to ${Math.round(a.timeout_s / 60)} min)`);
    }
  }
  if ((result.promoted || []).length) lines.push(`Started from the queue: ${result.promoted.map((r) => `\`${r}\``).join(", ")}.`);
  return lines.join("\n");
}

export function formatStatus(doc) {
  if (doc.runs) {
    const rows = doc.runs.map((r) => `| \`${r.id}\` | ${r.workflow} | ${r.status} | ${r.step || "—"} (${r.done}/${r.total}) |`);
    return [`**Teams** (${doc.active} active of ${doc.max_teams}; queued: ${doc.queued.length})`, "",
      "| Run | Workflow | Status | Step |", "| :--- | :--- | :--- | :--- |", ...rows].join("\n");
  }
  const steps = Object.entries(doc.steps || {}).map(([id, s]) => `| ${id} | ${s.role} | ${s.state} | ${s.attempt} | ${s.result || ""} |`);
  const extra = doc.approval && doc.approval.state === "pending" && doc.status === "awaiting_approval"
    ? `\nWaiting for you: \`/workflow approve ${doc.id}\` or \`/workflow cancel ${doc.id}\`.` : "";
  return [`**Run** \`${doc.id}\` — ${doc.workflow} — **${doc.status}**${doc.failure ? ` (${doc.failure})` : ""}`, "",
    "| Step | Role | State | Attempt | Result |", "| :--- | :--- | :--- | :--- | :--- |", ...steps, extra].join("\n");
}

/**
 * @param {object} api       the register(api) object
 * @param {object} options   { pluginConfig, kitRoot, python3 } plus injectable deps for tests:
 *                           execFile, setTimeout, clearTimeout, now, notify, log
 */
export function createOrchestrator(api, options = {}) {
  const pluginConfig = options.pluginConfig || (api && api.pluginConfig) || {};
  const execFile = options.execFile || nodeExecFile;
  const setTimer = options.setTimeout || setTimeout;
  const clearTimer = options.clearTimeout || clearTimeout;
  const log = options.log || (api && api.logger) || { info() {}, warn() {} };
  const opts = engineOptions(pluginConfig);
  const cmd = engineCommand({ aiResourcesBin: pluginConfig.aiResourcesBin || "", python3: options.python3 || "python3", kitRoot: options.kitRoot || pluginConfig.kitRoot || "" });
  const contexts = new Map();       // run id -> { sessionKey, agentId } of the requester, for later notices
  const inflight = new Set();       // "<run>/<step>/<attempt>" currently being awaited by this process
  const notices = [];

  /** Run one engine verb; resolves to the parsed JSON object. Rejects with an Error carrying `.code`. */
  function engine(verb, args = [], { stdin = "" } = {}) {
    const argv = [...cmd.prefix, "orchestrate", verb, ...args, "--json"];
    if (opts.runsDir) argv.push(`--runs-dir=${opts.runsDir}`);
    return new Promise((resolve, reject) => {
      const child = execFile(cmd.file, argv, { timeout: ENGINE_TIMEOUT_MS, maxBuffer: 8 * 1024 * 1024, encoding: "utf8" },
        (err, stdout) => {
          let doc = null;
          try { doc = JSON.parse(String(stdout || "").trim() || "null"); } catch { doc = null; }
          if (doc && doc.error) {
            const e = new Error(doc.error);
            e.code = doc.code || "engine";
            return reject(e);
          }
          if (err && !doc) {
            const e = new Error(`ai-resources orchestrate ${verb} failed: ${err.message}`);
            e.code = "engine-unavailable";
            return reject(e);
          }
          return resolve(doc || {});
        });
      if (stdin && child && child.stdin) child.stdin.end(stdin);
      else if (child && child.stdin && typeof child.stdin.end === "function") child.stdin.end();
    });
  }

  function notify(runId, text) {
    notices.push({ run: runId, text });
    if (typeof options.notify === "function") return options.notify(runId, text, contexts.get(runId));
    const ctx = contexts.get(runId);
    const system = api && api.runtime && api.runtime.system;
    try {
      if (ctx && system && typeof system.enqueueSystemEvent === "function") {
        system.enqueueSystemEvent(text, { sessionKey: ctx.sessionKey, agentId: ctx.agentId });
        return undefined;
      }
    } catch (e) { log.warn(`ai-resources orchestrator: could not post a notice (${e.message})`); }
    log.info(`ai-resources orchestrator [${runId}]: ${text}`);
    return undefined;
  }

  /** Dispatch what an engine result asks for, and post the questions and notices. Never throws. */
  async function handle(result) {
    const out = [];
    for (const a of result.actions || []) {
      if (a.type === "dispatch") {
        runStep(a).catch((e) => log.warn(`ai-resources orchestrator: step ${a.run}/${a.step} failed (${e.message})`));
        out.push(a);
      } else if (a.type === "ask" || a.type === "notice" || a.type === "failed" || a.type === "done" || a.type === "cancelled") {
        if (a.type !== "notice" || result.status === "queued") notify(a.run || result.run, a.text);
      }
    }
    for (const r of result.promoted || []) {
      try { await handle(await engine("next", [r])); } catch (e) { log.warn(`ai-resources orchestrator: could not start ${r} (${e.message})`); }
    }
    return out;
  }

  /** Dispatch one step, wait with ONE blocking call raced by the role timer, report, and continue with what comes back. */
  async function runStep(action) {
    const key = `${action.run}/${action.step}/${action.attempt}`;
    if (inflight.has(key)) return;
    inflight.add(key);
    try {
      const sub = api && api.runtime && api.runtime.subagent;
      if (!sub || typeof sub.run !== "function") {
        return handle(await engine("report", [action.run, action.step, "--error=runtime.subagent.run is not available on this gateway"]));
      }
      const sessionKey = stepSessionKey(action);
      let started;
      try {
        started = await sub.run.call(sub, { sessionKey, message: action.prompt, deliver: true });
      } catch (e) {
        return handle(await engine("report", [action.run, action.step, `--error=${String(e.message || e).slice(0, 200)}`]));
      }
      const runId = started && started.runId;
      const realKey = (started && started.sessionKey) || sessionKey;
      const outcome = await awaitRun(sub, runId, realKey, action.timeout_s * 1000);
      if (outcome.timeout) {
        try { if (typeof sub.deleteSession === "function") await sub.deleteSession.call(sub, { sessionKey: realKey }); } catch { /* best effort */ }
        return handle(await engine("report", [action.run, action.step, "--timeout", `--session=${realKey}`, `--agent=${action.agent}`]));
      }
      if (outcome.error) {
        return handle(await engine("report", [action.run, action.step, `--error=${String(outcome.error).slice(0, 200)}`, `--session=${realKey}`]));
      }
      const reply = String(outcome.reply || "").slice(-MAX_REPLY_CHARS);
      return handle(await engine("report", [action.run, action.step, `--session=${realKey}`, `--agent=${action.agent}`, "--reply-file=-"], { stdin: reply }));
    } finally {
      inflight.delete(key);
    }
  }

  async function awaitRun(sub, runId, sessionKey, timeoutMs) {
    let timer;
    let timedOut = false;
    const deadline = new Promise((resolve) => { timer = setTimer(() => { timedOut = true; resolve({ timeout: true }); }, timeoutMs); });
    // The gateway keeps the process alive; a pending role timer must not (it would also keep a test run from ending).
    if (timer && typeof timer.unref === "function") timer.unref();
    const finished = (async () => {
      for (let guard = 0; guard < 500 && !timedOut; guard += 1) {
        const waited = await sub.waitForRun.call(sub, { runId, timeoutMs });
        if (timedOut) return { timeout: true };
        const status = waited && waited.status;
        if (status === "pending") continue;
        if (status === "timeout") return { timeout: true };
        if (status === "error") return { error: waited.error || "the run ended with an error" };
        let reply = typeof waited.terminalReply === "string" ? waited.terminalReply : "";
        if (!reply && typeof sub.getSessionMessages === "function") {
          const got = await sub.getSessionMessages.call(sub, { sessionKey, limit: 6 });
          reply = lastAssistantText(got && got.messages);
        }
        return { reply };
      }
      return { timeout: true };
    })().catch((e) => ({ error: String(e.message || e) }));
    try {
      return await Promise.race([finished, deadline]);
    } finally {
      clearTimer(timer);
    }
  }

  return {
    engine, notices, inflight, contexts, handle, runStep, options: opts,

    async start(workflow, goal, ctx = {}, extra = {}) {
      if (!WORKFLOW_RE.test(workflow)) throw new Error(`invalid workflow id ${JSON.stringify(workflow)}`);
      const args = [`--workflow=${workflow}`, `--goal=${goal}`, `--max-teams=${opts.maxTeams}`];
      if (extra.workspace) args.push(`--workspace=${extra.workspace}`);
      if (extra.domain) args.push(`--domain=${extra.domain}`);
      if (Object.keys(opts.roleTimeouts).length) args.push(`--role-timeouts=${JSON.stringify(opts.roleTimeouts)}`);
      const result = await engine("start", args);
      if (result.run) contexts.set(result.run, { sessionKey: ctx.sessionKey || ctx.sessionId, agentId: ctx.agentId });
      await handle(result);
      return result;
    },
    async approve(runId) { this.check(runId); const r = await engine("approve", [runId]); await handle(r); return r; },
    async cancel(runId) { this.check(runId); const r = await engine("cancel", [runId]); await handle(r); return r; },
    async status(runId) { if (runId) this.check(runId); return engine("status", runId ? [runId] : []); },
    async cost(runId) { this.check(runId); return engine("cost", [runId]); },
    async resume(runId) {
      if (runId) this.check(runId);
      const r = await engine("resume", runId ? [runId] : ["--all"]);
      for (const one of r.runs || [r]) await handle(one);
      return r;
    },
    /** On plugin load: hand out again every step a restart interrupted. Never throws. */
    async resumeAll() {
      try { return await this.resume(""); } catch (e) { log.warn(`ai-resources orchestrator: resume skipped (${e.message})`); return null; }
    },
    check(runId) { if (!RUN_ID_RE.test(String(runId || ""))) throw new Error(`invalid run id ${JSON.stringify(runId)}`); },
  };
}
