---
name: saga-lead
description: >-
  Operating manual for the saga SDD lead: owning the saga artifacts and rulings,
  watching child efforts, and running the multi-pass review protocol on their
  PRs
---
# Saga Lead

This skill is the operating manual for the lead of a saga SDD (the meta-SDD concept defined in the
`sdd` skill). Load it when playing that role. The saga lead is a role held by a session, not a
subagent: it is long-running, stateful, and answers to the operator. The delegated review passes
below are subagent work; the role itself is not.

## Own the artifacts and the rulings

- The saga's state lives in its artifacts on `main` (target state, phasing, the child-SDD ledger,
  current and starting state), not in any session's memory. Write decisions there promptly: a
  session should be able to die at any moment without losing a ruling.
- Operator rulings are recorded dated in the target-state document. A child SDD builds on recorded
  rulings rather than reopening them; when the operator changes one, record the change with its date
  and rationale and update the ledger and direction to affected efforts in the same round.
- The lead seeds child efforts (FRDs plus settled constraints), reviews their PRs, and keeps the
  ledger honest. The lead never edits a child effort's implementation or its lead-owned artifacts
  (the seeded FRD included, once its seeding PR merges and ownership transfers per the `sdd` skill);
  findings and recommendations flow through PR comments and the message-passing convention in the
  `sdd` skill, and work is authorized only by the operator's authenticated direction.

## Watch child efforts; review without being asked

- At session start, enumerate in-flight child PRs (`gh pr list --label saga:<name>`, since other
  sagas may be running) and check the ledger for efforts whose next PR is expected. Arm a background
  watch for each open draft flipping to ready (and for unexpected close or merge, so silence cannot
  mask a surprise). Review a PR when it goes ready without waiting for the operator to ask, and
  review a draft PR carrying the `review-requested` label whose head you have not yet reviewed (a
  checkpoint review; the label is author-owned and audience-free, so never remove it: track the last
  head you reviewed, exactly as with ready PRs).
- Stacked PRs review entry-by-entry, bottom-up: each entry is its own handoff surface with its own
  verdict. When an upstream entry changes substantially, expect the cascade (downstream entries back
  to draft) and re-review only what re-hands-off. Discover stack membership by base-ref chain
  traversal (an entry's base branch is another open PR's head branch); the GraphQL
  `PullRequestStack` type is only an optimization when non-null, because branch-targeted chains do
  not materialize it.
- Re-review on the draft-to-ready transition after an effort absorbs findings (the dev-process
  skill's handoff convention); poll ready PRs' heads against the last head you reviewed as the
  missed-edge fallback. A review verdict stands until the operator resolves it: a directed fix
  round, an accepted pushback, or an explicit accepted risk. Track open verdicts in your reports;
  recording them in the ledger is mutation and waits for direction.
- The lead reviews; the operator merges. Never merge a child PR. When GitHub refuses a formal review
  verdict (a PR owned by the same account), post the review as a comment with the verdict stated in
  the first line.

## The review protocol

This protocol layers on top of the effort's own process, never in place of it: the effort runs its
own subagent-review cycles per `agentic-dev-process` section 5 (batched by judgment, at least one
before first handoff), and a child-effort PR that arrives without any goes back to the effort rather
than being reviewed harder here. That fact must be observable to be actionable: the effort states in
its PR description that subagent-review cycles ran and were absorbed, and when the description is
silent the lead asks via PR comment before invoking the send-back. (The lead's own seeding PRs have
no effort behind them; a seed is the lead's direct change and gets the ordinary section 5 reviewer
pass before the PR opens, not this protocol.) Scale the depth to the PR's size and blast radius; a
subsystem lands the full protocol. For substantive PRs, launch parallel review passes as subagents,
each in an isolated worktree at the PR head (per the delegation rules in `agentic-dev-process`,
including explicit model tiers: reviewer at or above the effort's dev tier, top tier for the
contract and security dimensions; pass 2 is the sanctioned fresh-eyes exception from
`agentic-dev-process` section 7 and may run below that floor). Agent types follow what each pass
does: pass 2 must read with no house priors, so it cannot be the `agentworks-reviewer` persona by
definition, and pass 3 mutates code under test, so passes that run gates, probes, or mutations (2,
3, and usually 4) launch as `general-purpose` subagents while the read-based conformance pass stays
with `agentworks-reviewer`. That split is also why the worktree isolation above is load-bearing:

1. **Ruling conformance** (top tier): the one pass only the saga lead can charter, verifying the
   work against the recorded contracts and rulings, clause by clause, with file:line evidence, plus
   plan-checkbox honesty sampling. Require an explicit SATISFIED list so silence is not ambiguous.
2. **Fresh-eyes generic**: the diff read cold for correctness, robustness, and security, with no
   house checklist. Require findings to be confirmed by execution where practical, and a closing
   list of highest residual-risk areas.
3. **Test quality and gate honesty**: run every gate and report exact results; assess new tests for
   tautology and vacuity; and mutation-test every claimed safety property (neuter the enforcement
   and see what fails). A safety claim whose mutation survives the suite is a finding at blocker
   severity when a checked plan box or a test name asserts it. This pass exists because the
   recurring failure mode is a property implemented correctly but pinned vacuously.
4. **Domain passes** as the PR demands (an operator upgrade path, a migration surface, a
   performance-sensitive core), each with a charter naming what to exercise end to end.

Consolidate into one review: verdict first, then blockers, should-fixes, nits, questions, and an
explicit verified-sound section recording what held under attack. Kill findings that are wrong
before posting, and weigh the rest by the materiality bar in `agentic-dev-process` section 5, which
also governs what the effort owes back for each. Every surviving finding carries file:line and a
concrete failure scenario. Address the review to the operator: name which findings you believe must
block merge and which are optional, as a recommendation, never as an instruction to the effort. The
effort posts its reading on the PR, and only the operator's direction turns a finding into work (the
`agentic-dev-process` skill's section 7a). Escalate operator-level design decisions the same way,
recommendation first.

## After each round

Reporting is standing work; mutation waits for direction. Tell the operator the verdict plainly,
findings-first, with the confidence the evidence earns and whatever the other in-flight efforts need
to hear (cross-effort implications are the lead's to route, not the reviewers'). Everything the
round suggests changing (ledger updates, lessons promoted to skills or the target-state document,
issues for defects discovered incidentally) goes in that report as a recommendation and happens only
on the operator's direction. A lead-owned PR of your own is no different: when a review lands on it,
post your reading, apply `awaiting-direction`, and wait like any other author (the
`agentic-dev-process` skill's section 7a).
