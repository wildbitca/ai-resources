export const meta = {
  name: 'kit-plan',
  description: 'Understand the code, draft plans from three angles, judge them, and write one plan for review',
  whenToUse: 'Before implementing a feature, bugfix or refactor with the ai-resources kit. Stop here for approval, then run /kit-implement.',
  phases: [
    { title: 'Understand', detail: 'classify the size (inline | light | full), then readers: code, specs, tests and risk' },
    { title: 'Draft', detail: 'three independent plans from different angles (light: one)' },
    { title: 'Judge', detail: 'two judges score the drafts (light: skipped)' },
    { title: 'Plan', detail: 'synthesize the winner into one plan file' },
  ],
}

// args: a goal string, or {goal, kind: 'feature'|'bugfix'|'refactor', constraints, size: 'inline'|'light'|'full'}
// A goal may carry --inline, --light or --full; an explicit size skips the classifier. Without one, a single
// cheap call classifies the task (see skills/kit-orchestration "Task size"); a failed classification means full.
// This is the only place that classifies: /kit-implement reads the size from the plan's ## Sizing block or from the
// size passed on, and never classifies again. The run ledger (ai-resources runs) advises, it never decides.
const SIZES = ['inline', 'light', 'full']
const input = typeof args === 'string' ? { goal: args } : (args || {})
const rawGoal = input.goal || input.task || ''
const sizeFlag = /(?:^|\s)--(inline|light|full)(?=\s|$)/.exec(rawGoal)
const goal = rawGoal.replace(/(?:^|\s)--(inline|light|full)(?=\s|$)/g, ' ').trim()
const kind = input.kind || 'feature'
const constraints = input.constraints || ''
const explicitSize = SIZES.includes(input.size) ? input.size : (sizeFlag ? sizeFlag[1] : null)

if (!goal) {
  return { error: 'No goal given. Run /kit-plan with what you want planned.' }
}

const UNDERSTAND_SCHEMA = {
  type: 'object',
  required: ['summary', 'files', 'notes'],
  properties: {
    summary: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
}

const DRAFT_SCHEMA = {
  type: 'object',
  required: ['approach', 'steps', 'files', 'risks'],
  properties: {
    approach: { type: 'string' },
    steps: { type: 'array', items: { type: 'string' } },
    files: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    tradeoffs: { type: 'string' },
  },
}

const JUDGE_SCHEMA = {
  type: 'object',
  required: ['winner', 'rationale', 'scores'],
  properties: {
    winner: { type: 'integer' },
    rationale: { type: 'string' },
    scores: { type: 'array', items: { type: 'number' } },
    best_ideas_from_others: { type: 'array', items: { type: 'string' } },
  },
}

const PLAN_SCHEMA = {
  type: 'object',
  required: ['plan_path', 'summary', 'acceptance_criteria', 'requires_tests', 'security_critical'],
  properties: {
    plan_path: { type: 'string' },
    summary: { type: 'string' },
    acceptance_criteria: { type: 'array', items: { type: 'string' } },
    requires_tests: { type: 'boolean' },
    security_critical: { type: 'boolean' },
    open_questions: { type: 'array', items: { type: 'string' } },
    tasks_path: { type: 'string' },
    task_ids: { type: 'array', items: { type: 'string' } },
    spec_ids: { type: 'array', items: { type: 'string' } },
  },
}

const SIZE_SCHEMA = {
  type: 'object',
  required: ['mode', 'reason'],
  properties: {
    mode: { type: 'string', enum: SIZES },
    reason: { type: 'string' },
    resume_test: { type: 'string', enum: ['pass', 'fail'] },
    risk_items: { type: 'array', items: { type: 'string' } },
    forecast_lines: { type: 'integer' },
    units: { type: 'integer' },
    files: { type: 'integer' },
    started_at: { type: 'integer' },
    suggested_mode: { type: 'string', enum: SIZES },
  },
}

const LENSES = [
  {
    label: 'code',
    prompt: `Map the code this work touches: entry points, the modules involved, existing patterns to follow, and where similar things are already done. Report file:line references, not code dumps.`,
  },
  {
    label: 'specs',
    prompt: `Read the project's context: specs/PROJECT.md, any spec under specs/features/ or specs/architecture/ that covers this area, README and ADRs. Report the requirements, rules (RULE-* ids) and decisions that constrain this work, with paths. Say so plainly if the project has no specs.`,
  },
  {
    label: 'tests-and-risk',
    prompt: `Report how this area is tested (frameworks, where tests live, how they run, current coverage of the target code) and what could break: public API or schema changes, data migrations, auth, concurrency, performance. List the commands that verify this project (test, lint, typecheck, build).`,
  },
]

const ANGLES = [
  'Smallest correct change: the least code that satisfies the goal, reusing what exists.',
  'Risk-first: order the work so the riskiest unknown is proven first, and every step keeps the tests for its area green.',
  'Structure-first: respect and, where cheap, improve the module boundaries and interfaces this touches.',
]

phase('Understand')
log(`Planning (${kind}): ${goal}`)

let size = explicitSize
let startEpoch
let sizing = { reason: explicitSize ? 'set by the caller' : '', risk_items: [] }
const forced = !!explicitSize
if (!size) {
  const sized = await agent(
    `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}\n` +
    `Classify the task size from a bounded read (about 3 tool calls). Do not change any file. Counts of files or tests never decide.\n` +
    `- inline: the change and its place are clear, the resume test passes (if the session stopped now, someone could finish from the request and git diff), one unit.\n` +
    `- light: the resume test fails or a risk item applies, but it is one unit (or units of about 100 changed lines or less).\n` +
    `- full: two or more independent units with disjoint edit surfaces, each over about 100 changed lines, or the user wants separate deliverables.\n` +
    `Risk items: 1 data or irreversible effects; 2 security; 3 contracts others consume (APIs, CLI flags, config, handoff keys, hooks, workflow YAML); ` +
    `4 concurrency; 5 delivery or environment; 6 no test would catch a regression. List the item numbers with a reason in risk_items, or leave it empty.\n` +
    `First run \`date +%s\` and report it as started_at (skip it when the shell is unavailable). Report forecast_lines (changed lines), units and files as your estimate. Then, only when the command exists and the numbers came out of your read, ` +
    `run \`ai-resources runs suggest --kind ${kind} --files N --lines F --units U [--risk 3,5]\` and put its mode in suggested_mode. ` +
    `It is a hint from past runs, never a decision: the resume test and the risk items decide, and you may differ from it (say why in reason). ` +
    `When the command is missing or fails, skip it silently.`,
    { label: 'classify', phase: 'Understand', agentType: 'explore', schema: SIZE_SCHEMA },
  )
  size = sized && SIZES.includes(sized.mode) ? sized.mode : 'full'
  startEpoch = sized && Number.isFinite(sized.started_at) ? sized.started_at : undefined
  sizing = sized ? { ...sized, risk_items: sized.risk_items || [] } : { reason: 'classification failed, defaulting to full', risk_items: [] }
  // A named risk item takes a task out of the inline path; it adds a verifier, not a bigger plan.
  if (size === 'inline' && sizing.risk_items.length) size = 'light'
}
log(`Size: ${size}${sizing.reason ? ` — ${sizing.reason}` : ''}`)

if (size === 'inline') {
  return {
    goal,
    kind,
    size,
    risk_items: sizing.risk_items,
    reason: sizing.reason,
    plan_path: null,
    start: startEpoch,
    estimated: sizing.suggested_mode || size,
    next: `Inline: no plan file and no further subagents. Make the change in this session, run the focused test then the suite once. Re-run with --light or --full to force a plan. ` +
      `At the end, when \`ai-resources runs\` exists, append the run: \`ai-resources runs record --mode ${size} --estimated ${sizing.suggested_mode || size} --outcome pass|gaps|abandoned --agents 0 --kind ${kind}${startEpoch ? ` --start ${startEpoch}` : ''}\` plus --lines, --units, --files and --risk (skip silently when the command is missing).`,
  }
}

// Keep each result paired with its lens: a dead reader must not shift the others' labels.
const readLenses = size === 'light'
  ? [{ label: 'code-specs-tests', prompt: LENSES.map(l => l.prompt).join('\n\nThen: ') }]
  : LENSES
const findings = (await parallel(readLenses.map(lens => () =>
  agent(
    `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}` +
    `${lens.prompt}\n\nRead only what you need. Do not change any file.`,
    { label: `understand:${lens.label}`, phase: 'Understand', agentType: 'explore', schema: UNDERSTAND_SCHEMA },
  ).then(result => (result ? { lens: lens.label, result } : null)),
))).filter(Boolean)

if (!findings.length) {
  return { error: 'Exploration returned nothing; run /kit-plan again or plan in the session.' }
}
if (findings.length < readLenses.length) {
  const missing = readLenses.filter(l => !findings.some(f => f.lens === l.label)).map(l => l.label)
  log(`Missing context from: ${missing.join(', ')} — the plan is drafted without it`)
}

const context = findings.map(({ lens, result: f }) =>
  `### ${lens}\n${f.summary}\n` +
  `Files: ${(f.files || []).join(', ')}\n` +
  `Notes:\n- ${(f.notes || []).join('\n- ')}` +
  ((f.open_questions || []).length ? `\nOpen questions:\n- ${f.open_questions.join('\n- ')}` : ''),
).join('\n\n')

phase('Draft')
const draftAngles = size === 'light' ? ANGLES.slice(0, 1) : ANGLES
const drafts = (await parallel(draftAngles.map((angle, i) => () =>
  agent(
    `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}\n` +
    `What the readers found:\n\n${context}\n\n` +
    `Draft an implementation plan with this bias: ${angle}\n\n` +
    `Rules: keep the external behaviour the goal does not ask to change; make each step independently verifiable; ` +
    `name the files each step touches; say which steps need new tests. Verify anything the readers left open before relying on it. Do not change any file.`,
    { label: `draft:${i + 1}`, phase: 'Draft', agentType: 'planner', schema: DRAFT_SCHEMA },
  ),
))).filter(Boolean)

if (!drafts.length) {
  return { error: 'No plan draft survived; run /kit-plan again.' }
}

const draftText = drafts.map((d, i) =>
  `### Draft ${i + 1}: ${d.approach}\nSteps:\n- ${d.steps.join('\n- ')}\nFiles: ${d.files.join(', ')}\n` +
  `Risks:\n- ${d.risks.join('\n- ')}${d.tradeoffs ? `\nTradeoffs: ${d.tradeoffs}` : ''}`,
).join('\n\n')

phase('Judge')
const judgeCriteria = [
  'correctness and completeness against the goal, and whether each step is actually verifiable',
  'risk: blast radius, reversibility, hidden coupling, and how early the plan proves its riskiest assumption',
]

const verdicts = size === 'light' ? [] : (await parallel(judgeCriteria.map((criterion, i) => () =>
  agent(
    `Goal (${kind}): ${goal}\n\nContext:\n\n${context}\n\nDrafts:\n\n${draftText}\n\n` +
    `Judge these ${drafts.length} drafts on ${criterion}. Score each from 0 to 10 in draft order, pick a winner ` +
    `(1-based index) and list ideas worth grafting from the others. Be specific about what is wrong with the losers.`,
    { label: `judge:${i + 1}`, phase: 'Judge', schema: JUDGE_SCHEMA },
  ),
))).filter(Boolean)

const totals = drafts.map((_, i) =>
  verdicts.reduce((sum, v) => sum + (Array.isArray(v.scores) && typeof v.scores[i] === 'number' ? v.scores[i] : 0), 0))
const judged = verdicts.length > 0 && Math.max(...totals) > 0
const winnerIndex = judged ? totals.indexOf(Math.max(...totals)) : 0
const grafts = verdicts.flatMap(v => v.best_ideas_from_others || [])
log(judged
  ? `Draft ${winnerIndex + 1} wins (scores ${totals.join(' / ')})`
  : size === 'light' ? `Light plan: one draft, no judges`
  : `No usable scores from the judges — falling back to draft 1, unjudged`)

phase('Plan')
const plan = await agent(
  `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}\n` +
  `Context:\n\n${context}\n\nDrafts:\n\n${draftText}\n\n` +
  (judged
    ? `The judges picked draft ${winnerIndex + 1}. Their rationale:\n- ${verdicts.map(v => v.rationale).join('\n- ')}\n`
    : size === 'light' ? `Use the single draft as the base.\n`
    : `The judges returned no usable scores. Use draft 1 as the base and weigh the others yourself.\n`) +
  (grafts.length ? `Graft these ideas from the other drafts where they fit:\n- ${grafts.join('\n- ')}\n` : '') +
  `\nTask size: ${size}${sizing.risk_items.length ? `; risk items: ${sizing.risk_items.join('; ')}` : '; no risk items'}. ` +
  `Start the plan with a "## Sizing" block (mode, resume_test, risk_items, forecast lines and units, route, verification, spawn_budget).\n` +
  `Write the final plan to .agent-output/planner/<slug>-plan.md (create the directory if needed) with:\n` +
  `- Goal and scope, plus what is explicitly out of scope\n` +
  `- Numbered steps, each with the files it touches and how to verify it: the tests for those files, never the full suite per step ` +
  `(the full suite runs once, in the Test phase of /kit-implement)\n` +
  `- Acceptance criteria as Given/When/Then, each mapped to the command that proves it\n` +
  `- Test plan: what needs new tests, which existing suites must stay green\n` +
  `- Risks and rollback\n` +
  `- Open questions for the user, if any\n` +
  `- Number acceptance criteria S1, S2, ... and tasks T1, T2, ... (each task names the S ids it covers) so later steps refer to them by id\n\n` +
  `If the project has a specs/ directory, also write the temporal task ledger to specs/features/<feature>/tasks.md from $AGENT_KIT/templates/tasks.template.md ` +
  `(acceptance criteria in English with a short source reference, not verbatim quotes) and set tasks_path; it is removed when the change closes.\n` +
  `Return task_ids (T1, ...) and spec_ids (S1, ...) so the implementer is handed work by reference, never by paraphrase. ` +
  `When the command exists, run \`ai-resources plan-check\` on the plan file and fix what it reports about the Sizing block.\n` +
  `Then return the path and a short summary. Write only that file; change no source file.`,
  { label: 'write-plan', phase: 'Plan', agentType: 'planner', schema: PLAN_SCHEMA },
)

if (!plan) {
  return { error: 'The plan was not written; run /kit-plan again.' }
}

return {
  goal,
  kind,
  size,
  risk_items: sizing.risk_items,
  start: startEpoch,
  plan_path: plan.plan_path,
  tasks_path: plan.tasks_path || null,
  task_ids: plan.task_ids || [],
  spec_ids: plan.spec_ids || [],
  summary: plan.summary,
  acceptance_criteria: plan.acceptance_criteria,
  requires_tests: plan.requires_tests,
  security_critical: plan.security_critical,
  open_questions: plan.open_questions || [],
  judged: { winner: winnerIndex + 1, scores: totals, scored: judged },
  context_gaps: readLenses.filter(l => !findings.some(f => f.lens === l.label)).map(l => l.label),
  next: `Review the plan, then run /kit-implement with ${JSON.stringify({ plan_path: plan.plan_path, tasks_path: plan.tasks_path || undefined, task_ids: plan.task_ids || [], spec_ids: plan.spec_ids || [], goal, kind, size, risk_items: sizing.risk_items, start: startEpoch, estimated: sizing.suggested_mode || size, forced, forecast_lines: sizing.forecast_lines, units: sizing.units, files: sizing.files })}`,
}
