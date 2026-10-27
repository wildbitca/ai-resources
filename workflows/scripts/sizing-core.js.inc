// Deterministic mapping from the six 0-3 scores to a mode (skills/kit-orchestration "Scorecard"), shared by /kit-plan
// and /kit-implement. Workflow scripts cannot import, so `ai-resources generate` pastes this file (with the rules from
// skills/kit-orchestration/scorecard.json) between the sizing-core markers of both scripts; edit it here, not there.
// lite/standard/full are inline/light/full in the scripts (SCORECARD.sizes); SCORECARD.size_flags lists every accepted spelling.
const SCORECARD = @SCORECARD_JSON@
const MODE_TO_SIZE = SCORECARD.sizes
const SIZES = Object.values(MODE_TO_SIZE)
const modeOf = size => Object.keys(MODE_TO_SIZE).find(m => MODE_TO_SIZE[m] === size)
const extrasOf = s => Object.entries(SCORECARD.triggers).filter(([r, c]) => Object.entries(c).some(([d, t]) => s[d] >= t) && !SCORECARD.base.standard.includes(r)).map(([r]) => r)
const rosterFor = (mode, extras) => {
  if (mode === 'full') return [...SCORECARD.base.full]
  if (mode === 'lite') return []
  return [...SCORECARD.base.standard, ...extras].sort((a, b) => SCORECARD.base.full.indexOf(a) - SCORECARD.base.full.indexOf(b))
}
// The parts of a card that follow from its mode.
const teamFor = (mode, extras) => {
  const roster = rosterFor(mode, extras)
  return { roster, tiers: Object.fromEntries(roster.map(r => [r, SCORECARD.tiers[r]])), handoff: mode === 'full' || roster.includes('planner') }
}
// Returns null for an invalid card (scores that are not six integers 0-3); resolveSize decides what that means.
const scoreCard = scores => {
  if (!scores || typeof scores !== 'object') return null
  const s = {}
  for (const d of SCORECARD.dimensions) {
    if (!Number.isInteger(scores[d]) || scores[d] < 0 || scores[d] > 3) return null
    s[d] = scores[d]
  }
  const total = SCORECARD.dimensions.reduce((a, d) => a + s[d], 0)
  const extras = extrasOf(s)
  const L = SCORECARD.lite, F = SCORECARD.full
  let mode = 'standard'
  if (total >= F.min_total || s.risk >= F.min_risk || extras.length >= F.min_extras) mode = 'full'
  else if (!extras.length && total <= L.max_total && s.risk <= L.max_risk && s.uncertainty <= L.max_uncertainty && s.resumability <= L.max_resumability && s.verifiability <= (L.max_verifiability ?? 3)) mode = 'lite'
  return { mode, scored_mode: mode, bump_reason: null, escalations: [], size: MODE_TO_SIZE[mode], total, scores: s, ...teamFor(mode, extras) }
}
// The one place that turns a classifier answer (or the plan's Sizing block) into a size:
// valid scores decide; scores given but invalid mean full, even beside a bare mode; with no scores at all a bare mode
// from SIZES is honoured; no answer (a failed classifier) or an unusable mode means light.
const resolveSize = answer => {
  const scorecard = answer ? scoreCard(answer.scores) : null
  if (scorecard) return { scorecard, size: scorecard.size }
  if (answer && answer.scores != null) return { scorecard: null, size: 'full' }
  return { scorecard: null, size: answer && SIZES.includes(answer.mode) ? answer.mode : 'light' }
}
// A bump (a named risk item) or an escalation moves the card to a new size and records why: `mode`, `size`, `roster`
// and `tiers` describe what actually runs, `scored_mode` what the scores said.
const retarget = (card, size, reason, kind) => {
  if (!card) return card
  const mode = modeOf(size)
  const moved = { ...card, mode, size, ...teamFor(mode, mode === 'standard' ? extrasOf(card.scores || {}) : []) }
  if (kind === 'bump') moved.bump_reason = reason
  else moved.escalations = [...(card.escalations || []), { from: card.mode, to: mode, reason }]
  return moved
}
// A named risk item takes a task out of the inline path; it adds a verifier, not a bigger plan.
const bumpForRisk = (size, card, items) => {
  if (size !== 'inline' || !items.length) return { size, card, reason: null }
  const reason = `risk items named: ${items.join('; ')}`
  return { size: 'light', card: retarget(card, 'light', reason, 'bump'), reason }
}
// The one-line form for the handoff front matter key `scorecard` (same as ai_resources.scorecard.compact plus escalations).
const cardLine = card => !card ? '' : [card.mode.toUpperCase(), ...Object.entries(card.scores || {}).map(([d, v]) => `${SCORECARD.abbrev[d]}${v}`),
  ...(card.bump_reason ? [`(scored ${card.scored_mode.toUpperCase()}, bumped: ${card.bump_reason})`] : []),
  ...(card.escalations || []).map(e => `-> ${e.to.toUpperCase()} (${e.reason})`)].join(' ')
// Escalation never lowers the size: tests failing twice or a growing diff move lite -> standard -> full.
const escalateSize = (current, failures, diffLines) => {
  let best = current
  for (const [target, lim] of Object.entries(SCORECARD.escalation[modeOf(current)] || {})) {
    if ((failures >= lim.failures || diffLines >= lim.diff_lines) && SIZES.indexOf(MODE_TO_SIZE[target]) > SIZES.indexOf(best)) best = MODE_TO_SIZE[target]
  }
  return best
}
const SCORES_SCHEMA = {
  type: 'object',
  description: 'Six integer scores 0-3; higher means more ceremony: resumability (3 = a cold session cannot resume from request + git diff), blast_radius (files/modules), risk (auth, secrets, money, data migration, infra, public API), uncertainty (unknown cause or design choices), verifiability (3 = no existing tests cover it), spread (backend + infra + mobile).',
  required: SCORECARD.dimensions,
  properties: Object.fromEntries(SCORECARD.dimensions.map(d => [d, { type: 'integer', minimum: 0, maximum: 3 }])),
}
// Size flags (--inline, --lite, --light, --standard, --full) and `size`: every spelling in SCORECARD.size_flags; an explicit size skips the classifier.
const FLAG_SIZE = SCORECARD.size_flags
const FLAG_RE = new RegExp(`(?:^|\\s)--(${Object.keys(FLAG_SIZE).join('|')})(?=\\s|$)`)
const parseArgs = args => {
  const input = typeof args === 'string' ? { goal: args } : (args || {})
  const rawGoal = input.goal || input.task || ''
  const flag = FLAG_RE.exec(rawGoal)
  const size = (Object.hasOwn(FLAG_SIZE, input.size) ? FLAG_SIZE[input.size] : null) || (flag ? FLAG_SIZE[flag[1]] : null)
  return { input, goal: rawGoal.replace(new RegExp(FLAG_RE.source, 'g'), ' ').trim(), size }
}
