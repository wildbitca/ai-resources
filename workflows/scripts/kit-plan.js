export const meta = {
  name: 'kit-plan',
  description: 'Understand the code, draft plans from three angles, judge them, and write one plan for review',
  whenToUse: 'Before implementing a feature, bugfix or refactor with the ai-resources kit. Stop here for approval, then run /kit-implement.',
  phases: [
    { title: 'Understand', detail: 'parallel readers: code, specs, tests and risk' },
    { title: 'Draft', detail: 'three independent plans from different angles' },
    { title: 'Judge', detail: 'two judges score the drafts' },
    { title: 'Plan', detail: 'synthesize the winner into one plan file' },
  ],
}

// args: a goal string, or {goal, kind: 'feature'|'bugfix'|'refactor', constraints}
const input = typeof args === 'string' ? { goal: args } : (args || {})
const goal = input.goal || input.task || ''
const kind = input.kind || 'feature'
const constraints = input.constraints || ''

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
  'Risk-first: order the work so the riskiest unknown is proven first, and every step keeps the suite green.',
  'Structure-first: respect and, where cheap, improve the module boundaries and interfaces this touches.',
]

phase('Understand')
log(`Planning (${kind}): ${goal}`)

// Keep each result paired with its lens: a dead reader must not shift the others' labels.
const findings = (await parallel(LENSES.map(lens => () =>
  agent(
    `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}` +
    `${lens.prompt}\n\nRead only what you need. Do not change any file.`,
    { label: `understand:${lens.label}`, phase: 'Understand', agentType: 'explore', schema: UNDERSTAND_SCHEMA },
  ).then(result => (result ? { lens: lens.label, result } : null)),
))).filter(Boolean)

if (!findings.length) {
  return { error: 'Exploration returned nothing; run /kit-plan again or plan in the session.' }
}
if (findings.length < LENSES.length) {
  const missing = LENSES.filter(l => !findings.some(f => f.lens === l.label)).map(l => l.label)
  log(`Missing context from: ${missing.join(', ')} — the plan is drafted without it`)
}

const context = findings.map(({ lens, result: f }) =>
  `### ${lens}\n${f.summary}\n` +
  `Files: ${(f.files || []).join(', ')}\n` +
  `Notes:\n- ${(f.notes || []).join('\n- ')}` +
  ((f.open_questions || []).length ? `\nOpen questions:\n- ${f.open_questions.join('\n- ')}` : ''),
).join('\n\n')

phase('Draft')
const drafts = (await parallel(ANGLES.map((angle, i) => () =>
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

const verdicts = (await parallel(judgeCriteria.map((criterion, i) => () =>
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
  : `No usable scores from the judges — falling back to draft 1, unjudged`)

phase('Plan')
const plan = await agent(
  `Goal (${kind}): ${goal}\n${constraints ? `Constraints: ${constraints}\n` : ''}\n` +
  `Context:\n\n${context}\n\nDrafts:\n\n${draftText}\n\n` +
  (judged
    ? `The judges picked draft ${winnerIndex + 1}. Their rationale:\n- ${verdicts.map(v => v.rationale).join('\n- ')}\n`
    : `The judges returned no usable scores. Use draft 1 as the base and weigh the others yourself.\n`) +
  (grafts.length ? `Graft these ideas from the other drafts where they fit:\n- ${grafts.join('\n- ')}\n` : '') +
  `\nWrite the final plan to .agent-output/planner/<slug>-plan.md (create the directory if needed) with:\n` +
  `- Goal and scope, plus what is explicitly out of scope\n` +
  `- Numbered steps, each with the files it touches and how to verify it\n` +
  `- Acceptance criteria as Given/When/Then, each mapped to the command that proves it\n` +
  `- Test plan: what needs new tests, which existing suites must stay green\n` +
  `- Risks and rollback\n` +
  `- Open questions for the user, if any\n\n` +
  `Then return the path and a short summary. Write only that file; change no source file.`,
  { label: 'write-plan', phase: 'Plan', agentType: 'planner', schema: PLAN_SCHEMA },
)

if (!plan) {
  return { error: 'The plan was not written; run /kit-plan again.' }
}

return {
  goal,
  kind,
  plan_path: plan.plan_path,
  summary: plan.summary,
  acceptance_criteria: plan.acceptance_criteria,
  requires_tests: plan.requires_tests,
  security_critical: plan.security_critical,
  open_questions: plan.open_questions || [],
  judged: { winner: winnerIndex + 1, scores: totals, scored: judged },
  context_gaps: LENSES.filter(l => !findings.some(f => f.lens === l.label)).map(l => l.label),
  next: `Review the plan, then run /kit-implement with {"plan_path": "${plan.plan_path}", "goal": "${goal}", "kind": "${kind}"}`,
}
