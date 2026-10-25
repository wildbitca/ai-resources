// Runs a workflow script (workflows/scripts/*.js) against a scripted agent() and prints the calls and the result.
// usage: node kit_script_harness.mjs <script.js> <config.json>
// config: {"args": ..., "responses": {"<label prefix>": <value>}}; a label without a response yields null.
import fs from 'node:fs'

const [, , script, cfgPath] = process.argv
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'))
const src = fs.readFileSync(script, 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const calls = []
const agent = async (prompt, opts = {}) => {
  calls.push({ label: opts.label, agentType: opts.agentType || null })
  const key = Object.keys(cfg.responses || {}).find(k => (opts.label || '').startsWith(k))
  return key ? cfg.responses[key] : null
}
const parallel = async fns => Promise.all(fns.map(f => f()))
const run = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', src)
const result = await run(cfg.args, agent, parallel, () => {}, () => {})
console.log(JSON.stringify({ calls, result }))
