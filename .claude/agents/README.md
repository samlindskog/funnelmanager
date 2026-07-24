# Agent fleet — funnelmanager

This directory defines the subagents that do service work and adversarially
review it. It encodes the same isolation the runtime enforces: **one agent owns
one boundary, and cross-boundary changes are an explicit handoff, never a reach-in**
(the agent-workflow mirror of "exchange, never forward").

## How the global rules reach every agent

Subagents run in a **fresh context that still includes the project `CLAUDE.md`**.
That file already carries the cross-cutting rules — zero-trust / per-hop
audiences, "only leads talks to Apollo", the streaming never-raise invariant, the
naming convention, the Mongo-owns-payloads data model. **We deliberately do not
copy those into each agent** (that would burn tokens and drift). Each agent file
below adds only:

1. the **directory it owns** and the siblings it must not edit,
2. the **load-bearing invariants for that boundary** (a pointer into `CLAUDE.md`, restated tersely), and
3. its **verify command** and the **review handoff**.

If a global rule changes, change `CLAUDE.md` — every agent picks it up.

## The fleet

**Domain agents** (implement / modify — one per service boundary):

| Agent | Owns | Never edits |
|---|---|---|
| `search-agent`   | `search/` | `leads/`, Apollo — go through `LeadsClient` |
| `leads-agent`    | `leads/` (the *only* Apollo holder) | any UI shaping |
| `mail-agent`     | `mail/` | `mailui/` |
| `mailui-agent`   | `mailui/` (standalone React) | `frontend/` — they share no code |
| `frontend-agent` | `frontend/` | `mailui/`, backends |
| `mcp-agent`      | `mcp/` | `leads/` internals |
| `runtime-agent`  | `libs/fm_runtime/` (shared auth/runtime) | service business logic |
| `platform-agent` | `deploy/`, `deploy/keycloak/`, `docker-compose*.yml`, `.github/` | app source |
| `jobs-agent`     | `jobs/` (cross-app job tracker/controller, NEW) | the apps it tracks |
| `agents-agent`   | `agents/` (pydantic-ai runtime-agent backend, NEW) | `agentsui/`, other services' internals |
| `agentsui-agent` | `agentsui/` (standalone agents UI, NEW) | `frontend/`, `mailui/` |

The `jobs` / `agents` / `agentsui` services and their target architecture are
specified in `docs/agent-build-plan.md`. Run a build workstream with the
`service-workstream` workflow (`.claude/workflows/service-workstream.js`): it
dispatches the owning agent at **Opus/high**, then the three adversarial reviewers
on its diff, per the program's "team uses Opus on high effort + adversarial review
per agent" rule.

**Reviewer agents** (read-only, adversarial — run against a domain agent's diff).
These are **global** (`~/.claude/agents/`), reusable across every project — they
learn each repo's invariants at runtime by reading its `CLAUDE.md`. In *this* repo
that means the streaming never-raise rule, the Apollo-key boundary, `@anonymous`
drift, hydration/dedup, etc. are all in play via `CLAUDE.md`.

| Agent | Hunts |
|---|---|
| `bug-hunter`         | correctness: broken invariants, streaming/connection resets, dedup/ordering, async/race bugs |
| `security-reviewer`  | authz/authn gaps, secret leaks, token forward-vs-exchange, allowlist/policy drift |
| `quality-reviewer`   | simplification, reuse, convention drift, altitude (no bug hunting) |

They are also **self-improving (advisory — never auto-edited)** via three global
hooks: `SubagentStop` (`capture-reviewer.sh`) archives each reviewer's transcript;
`SessionEnd` (`review-reviewers.sh`) spawns a read-only headless Claude that
critiques their effectiveness and writes a **proposal** to
`~/.claude/reviewer-runs/pending/`; `SessionStart` (`reviewer-proposals-notice.sh`)
surfaces pending proposals in your next session for **yes/no approval** per
suggestion. Only approved edits touch the global `.md` files; handled proposals
move to `~/.claude/reviewer-runs/applied/`. Run log: `~/.claude/reviewer-runs/improve.log`.

## Token strategy (the balanced part)

- **Domain agents** omit a `model:` field → they **inherit the orchestrator's
  model**, so you choose opus vs sonnet per task at dispatch time.
- **Reviewers are read-only and diff-scoped** (`git diff`, not the whole tree),
  and pinned to **`sonnet`** — review multiplies (N services × 3 reviewers), so
  this is where cheap tiers pay off. Escalate a reviewer to opus only for the
  high-blast-radius boundaries (`leads`, `mail`, `fm_runtime`, `platform`).
- **Tool scoping doubles as a cost guard:** domain agents get edit tools but
  **not** `Agent` (no recursive fan-out); reviewers get read-only tools only.
- Reviewers **report, they do not fix** — the orchestrator (or the owning domain
  agent) applies accepted findings. Keeps the review context small and the fix in
  the hands of the boundary owner.

## The loop (implement → adversarial review)

Dispatch pattern, run by the main thread or a `Workflow` script:

1. Route the task to the one domain agent that owns the affected directory.
2. When it finishes (or, for long tasks, on its interim diff), fan out all three
   reviewers **in parallel** against that agent's `git diff`.
3. Each reviewer is prompted to **refute** ("assume this is wrong; find how").
   Collect findings; the orchestrator triages and hands accepted ones back to the
   domain agent to fix. Re-review only the fix diff.

A ready-to-run `Workflow` sketch for this lives at the bottom of this file.

Existing skills already cover the ends of the lifecycle: `deploy-dev` (ship to the
dev cluster) and `deploy-funnelmanager` (prod GitOps). Reviewers can also invoke
the repo's own `/code-review` and `/security-review` skills as a second opinion.

### Workflow sketch

```js
// .claude/workflows/service-change.js — run: Workflow({ scriptPath })
export const meta = {
  name: 'service-change',
  description: 'One domain agent implements; three reviewers adversarially verify the diff',
  phases: [{ title: 'Implement' }, { title: 'Review' }],
}
const { agentType, task } = args   // e.g. { agentType: 'leads-agent', task: '...' }

phase('Implement')
await agent(task, { agentType, label: agentType, phase: 'Implement' })

phase('Review')
const LENSES = ['bug-hunter', 'security-reviewer', 'quality-reviewer']
const reviews = await parallel(LENSES.map(a => () =>
  agent(`Adversarially review the current \`git diff\` — assume it is wrong and prove it. Report findings only; do not edit.`,
        { agentType: a, label: a, phase: 'Review' })))
return { reviews }
```
