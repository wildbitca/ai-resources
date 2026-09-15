export const meta = {
  name: 'kit-implement',
  description: 'Implement an approved plan with tests, then review it from three lenses, verify each finding and fix what survives',
  whenToUse: 'After /kit-plan (or with your own plan file). One writer makes the changes; reviewers only read.',
  phases: [
    { title: 'Implement', detail: 'one writer applies the plan with tests' },
    { title: 'Test', detail: 'full suite as a gate, with a bounded fix loop' },
    { title: 'Review', detail: 'correctness, security and test-integrity lenses in parallel' },
    { title: 'Verify findings', detail: 'a skeptic tries to refute each finding' },
    { title: 'Fix', detail: 'the writer applies the confirmed findings' },
    { title: 'Sign off', detail: 'acceptance criteria checked against evidence' },
  ],
}

// args: {plan_path, goal, kind, acceptance_criteria?, security_critical?} or a plain goal string
const input = typeof args === 'string' ? { goal: args } : (args || {})
const goal = input.goal || input.task || ''
const kind = input.kind || 'feature'
const planPath = input.plan_path || input.plan || ''
const criteria = input.acceptance_criteria || []
const securityCritical = input.security_critical !== false
const MAX_TEST_ROUNDS = 2

if (!goal && !planPath) {
  return { error: 'Give a plan path or a goal: /kit-implement with {"plan_path": "..."}.' }
}

const planRef = planPath
  ? `Read the approved plan at ${planPath} and follow it. Do not widen its scope.`
  : `There is no plan file. Keep the change minimal and focused on the goal.`

const IMPL_SCHEMA = {
  type: 'object',
  required: ['summary', 'files_changed', 'tests_added', 'commands_run'],
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    commands_run: { type: 'array', items: { type: 'string' } },
    deviations: { type: 'array', items: { type: 'string' } },
    blocked: { type: 'boolean' },
    block_reason: { type: 'string' },
  },
}

const TEST_SCHEMA = {
  type: 'object',
  required: ['passed', 'command', 'failures'],
  properties: {
    passed: { type: 'boolean' },
    command: { type: 'string' },
    failures: { type: 'array', items: { type: 'string' } },
    pre_existing: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'detail', 'severity'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'integer' },
          detail: { type: 'string' },
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['refuted', 'reason'],
  properties: {
    refuted: { type: 'boolean' },
    reason: { type: 'string' },
    suggested_fix: { type: 'string' },
  },
}

const SIGNOFF_SCHEMA = {
  type: 'object',
  required: ['met', 'gaps', 'evidence'],
  properties: {
    met: { type: 'boolean' },
    gaps: { type: 'array', items: { type: 'string' } },
    evidence: { type: 'array', items: { type: 'string' } },
    manual_checks: { type: 'array', items: { type: 'string' } },
  },
}

phase('Implement')
log(`Implementing (${kind}): ${goal || planPath}`)

const impl = await agent(
  `Goal (${kind}): ${goal}\n${planRef}\n\n` +
  `You are the only writer in this run. Work step by step:\n` +
  `- Write or update tests for the behaviour you are changing, and watch them fail before you make them pass.\n` +
  `- Keep each step small; run the project's lint/typecheck and the tests for the area as you go.\n` +
  `- Never weaken or delete an existing assertion to make a suite pass. If a test is wrong, say so in deviations.\n` +
  `- If the plan turns out to be wrong, follow the goal, record what you changed in deviations, and continue.\n` +
  `- Stop and set blocked only if the work cannot proceed without a decision.\n\n` +
  `Report the files you changed, the tests you added, and the exact commands you ran.`,
  { label: 'implement', phase: 'Implement', agentType: 'implementer', schema: IMPL_SCHEMA },
)

if (!impl) return { error: 'The implementation agent returned nothing.' }
if (impl.blocked) {
  return { status: 'blocked', reason: impl.block_reason || 'implementation blocked', files_changed: impl.files_changed }
}

phase('Test')
let test = await agent(
  `Run this project's full test suite and its lint/typecheck, exactly as the project documents them ` +
  `(package.json scripts, Makefile, CI config, or the kit's _domain-commands.yaml).\n` +
  `Report the command you used, whether everything passed, which failures come from the change just made ` +
  `(files: ${(impl.files_changed || []).join(', ') || 'unknown'}) and which were already failing before it.\n` +
  `Do not modify any file.`,
  { label: 'test-gate', phase: 'Test', agentType: 'tester', schema: TEST_SCHEMA },
)

let testRounds = 0
while (test && !test.passed && (test.failures || []).length && testRounds < MAX_TEST_ROUNDS) {
  testRounds += 1
  log(`Test failures, fix round ${testRounds}/${MAX_TEST_ROUNDS}`)
  const fix = await agent(
    `The suite fails after your change. Command: ${test.command}\nFailures:\n- ${test.failures.join('\n- ')}\n\n` +
    `Fix the cause, not the symptom: do not delete, skip or weaken assertions to get green. ` +
    `If a failure is unrelated to this change, leave it and say so in deviations.`,
    { label: `fix-tests:${testRounds}`, phase: 'Test', agentType: 'implementer', schema: IMPL_SCHEMA },
  )
  if (!fix) break
  test = await agent(
    `Re-run the full suite and lint/typecheck (${test.command}) and report the result. Do not modify any file.`,
    { label: `test-gate:${testRounds + 1}`, phase: 'Test', agentType: 'tester', schema: TEST_SCHEMA },
  )
}

const testsGreen = !!(test && test.passed)
if (!testsGreen) log('Suite is not green — reviewing anyway and reporting it')

phase('Review')
const changed = (impl.files_changed || []).join(', ') || 'the working tree changes'
const REVIEW_LENSES = [
  {
    key: 'correctness',
    agentType: 'code-reviewer',
    prompt: `Review the change for correctness against the goal and the plan: logic errors, unhandled cases, ` +
      `broken contracts, regressions in behaviour the goal did not ask to change. Report only defects you can point at in the diff.`,
  },
  {
    key: 'security',
    agentType: 'security-auditor',
    prompt: `Review the change for security defects: input validation at boundaries, authz checks, injection, ` +
      `secrets or tokens in code or logs, unsafe deserialization, permissions and dependency risk. Report only what this diff introduces or leaves exposed.`,
  },
  {
    key: 'test-integrity',
    agentType: 'code-reviewer',
    prompt: `Review only the test changes in this diff. Report: assertions deleted or weakened, tests skipped or marked ` +
      `pending, tests that assert on implementation detail instead of behaviour, cases in the plan's acceptance criteria with no test, ` +
      `and any test written so it cannot fail.`,
  },
]

const reviews = (await parallel(REVIEW_LENSES.map(lens => () =>
  agent(
    `Goal (${kind}): ${goal}\n${planPath ? `Plan: ${planPath}\n` : ''}Files changed: ${changed}\n` +
    `Read the diff (git diff, plus git diff --staged) and the files it touches.\n\n${lens.prompt}\n\n` +
    `Do not modify any file. Return an empty list when you find nothing — do not invent findings to look thorough.`,
    { label: `review:${lens.key}`, phase: 'Review', agentType: lens.agentType, schema: FINDINGS_SCHEMA },
  ).then(r => ({ lens: lens.key, findings: (r && r.findings) || [] })),
))).filter(Boolean)

const allFound = reviews.flatMap(r => (r.findings || []).map(f => ({ ...f, lens: r.lens })))
const SEVERITY_ORDER = { blocker: 0, major: 1, minor: 2 }
allFound.sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3))
// Verification runs one agent per finding; cap the batch so a chatty reviewer cannot blow past the
// runtime's concurrency limit and kill the run after the writer has already changed the tree.
const VERIFY_CAP = 16
const found = allFound.slice(0, VERIFY_CAP)
const unverified = allFound.slice(VERIFY_CAP)
const unverifiedBlockers = unverified.filter(f => f.severity !== 'minor')
const dropped = unverified.length
log(`${allFound.length} finding(s) from ${reviews.length} lenses` +
  (dropped ? ` — verifying the ${VERIFY_CAP} most severe, ${dropped} not verified (reported as unverified)` : ''))

phase('Verify findings')
const verified = found.length
  ? (await parallel(found.map((f, i) => () =>
      agent(
        `A reviewer (${f.lens} lens) claims this about the current change:\n\n` +
        `${f.title}\nFile: ${f.file}${f.line ? `:${f.line}` : ''}\nSeverity: ${f.severity}\n${f.detail}\n\n` +
        `Try to refute it. Read the code and the diff. It is refuted if the code already handles the case, ` +
        `the claim misreads the code, the path is unreachable, or it is a style preference rather than a defect. ` +
        `If you cannot confirm it from the code, treat it as refuted.`,
        { label: `verify:${i + 1}`, phase: 'Verify findings', schema: VERDICT_SCHEMA },
      ).then(v => (v && !v.refuted ? { ...f, fix: v.suggested_fix || '', why: v.reason } : null)),
    ))).filter(Boolean)
  : []

const blockers = verified.filter(f => f.severity !== 'minor')
log(`${verified.length} finding(s) survived (${blockers.length} blocking)`)

phase('Fix')
let fixResult = null
if (blockers.length) {
  fixResult = await agent(
    `These review findings survived independent verification. Fix them:\n\n` +
    blockers.map((f, i) =>
      `${i + 1}. [${f.severity}/${f.lens}] ${f.title} — ${f.file}${f.line ? `:${f.line}` : ''}\n   ${f.detail}` +
      (f.fix ? `\n   Suggested: ${f.fix}` : ''),
    ).join('\n') +
    `\n\nFix the cause of each one, keep the tests green (re-run the suite at the end), and do not weaken assertions. ` +
    `If you disagree with a finding, leave the code as is and explain why in deviations.`,
    { label: 'apply-fixes', phase: 'Fix', agentType: 'implementer', schema: IMPL_SCHEMA },
  )
}

phase('Sign off')
const signoff = await agent(
  `Goal (${kind}): ${goal}\n${planPath ? `Plan: ${planPath} — read its acceptance criteria.\n` : ''}` +
  (criteria.length ? `Acceptance criteria:\n- ${criteria.join('\n- ')}\n` : '') +
  `\nCheck the work against those criteria using evidence you can produce yourself: run the project's tests and ` +
  `lint, read the diff, run the commands the plan names. Report each criterion as met or not, with the evidence ` +
  `(command and result, or file:line). List anything only a human can check as manual_checks. ` +
  `Be skeptical: report a gap rather than assuming. Do not modify any file.`,
  { label: 'sign-off', phase: 'Sign off', agentType: 'verifier', schema: SIGNOFF_SCHEMA },
)

return {
  goal,
  kind,
  plan_path: planPath || null,
  // A finding that was never verified cannot count as cleared: a blocker past the cap keeps the run open.
  status: signoff && signoff.met && testsGreen && !blockers.length && !unverifiedBlockers.length
    ? 'done'
    : 'needs-attention',
  implementation: {
    summary: impl.summary,
    files_changed: impl.files_changed,
    tests_added: impl.tests_added,
    deviations: impl.deviations || [],
  },
  tests: test ? { passed: test.passed, command: test.command, failures: test.failures, pre_existing: test.pre_existing || [] } : null,
  review: {
    raw_findings: allFound.length,
    unverified: unverified.map(f => ({ severity: f.severity, lens: f.lens, title: f.title, file: f.file })),
    confirmed: verified.map(f => ({ severity: f.severity, lens: f.lens, title: f.title, file: f.file, why: f.why })),
    fixed: fixResult ? fixResult.files_changed : [],
    security_reviewed: securityCritical,
  },
  sign_off: signoff || null,
  next: unverifiedBlockers.length
    ? `Review the ${unverifiedBlockers.length} finding(s) listed under review.unverified — they were never checked — then commit or merge.`
    : 'Read the plan, the diff and the sign-off gaps; then commit or merge, and run /knowledge-audit if the project keeps specs.',
}
