export const meta = {
  name: 'service-workstream',
  description: 'Run build workstreams: each owning agent implements at Opus/high, then bug/security/quality reviewers adversarially verify its diff at Opus/high',
  phases: [
    { title: 'Implement', detail: 'owning service agent implements the workstream', model: 'opus' },
    { title: 'Review', detail: 'bug-hunter + security-reviewer + quality-reviewer on the diff', model: 'opus' },
  ],
}

// args: { workstreams: [{ agentType, task, label? }] }
// Runs workstreams SEQUENTIALLY (they share one working tree) — each is
// implement -> parallel adversarial review. Opus/high is forced on every agent()
// call per the program's "team uses Opus on high effort" rule. The capture +
// self-improvement hooks fire automatically for both the domain agent and the reviewers.
// args may arrive as an object or as a JSON-encoded string — tolerate both.
let _args = args
if (typeof _args === 'string') { try { _args = JSON.parse(_args) } catch (e) { _args = {} } }
const workstreams = (_args && _args.workstreams) || []
if (!workstreams.length) { log('no workstreams provided in args.workstreams'); return { results: [] } }

const REVIEWERS = ['bug-hunter', 'security-reviewer', 'quality-reviewer']
const results = []

for (let i = 0; i < workstreams.length; i++) {
  const ws = workstreams[i]
  const label = ws.label || ws.agentType || `ws-${i}`

  phase('Implement')
  log(`workstream ${i + 1}/${workstreams.length}: ${label}`)
  const impl = await agent(ws.task, {
    agentType: ws.agentType,
    label: `impl:${label}`,
    phase: 'Implement',
    model: 'opus',
    effort: 'high',
  })

  phase('Review')
  const reviews = await parallel(REVIEWERS.map(r => () =>
    agent(
      `Adversarially review the current \`git diff\` for the workstream "${label}". ` +
      `Assume the change is wrong and prove how. Read docs/agent-build-plan.md and CLAUDE.md ` +
      `for the intended architecture. Report findings only — do not edit.`,
      { agentType: r, label: `review:${label}:${r}`, phase: 'Review', model: 'opus', effort: 'high' },
    ).then(v => ({ reviewer: r, findings: v }))
  ))

  results.push({ workstream: label, impl, reviews: reviews.filter(Boolean) })
}

return { results }
