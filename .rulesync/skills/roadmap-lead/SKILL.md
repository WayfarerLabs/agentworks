---
name: roadmap-lead
description:
  "Operating manual for the roadmap SDD lead: owning the roadmap artifacts and rulings, watching
  child efforts, and running the multi-pass review protocol on their PRs"
targets: ["*"]
---

# Roadmap Lead

This skill is the operating manual for the lead of a roadmap SDD (the meta-SDD concept defined in
the `sdd` skill). Load it when playing that role. The roadmap lead is a role held by a session, not
a subagent: it is long-running, stateful, and answers to the operator. The delegated review passes
below are subagent work; the role itself is not.

## Own the artifacts and the rulings

- The roadmap's state lives in its artifacts on `main` (target state, phasing, the child-SDD ledger,
  current and starting state), not in any session's memory. Write decisions there promptly: a
  session should be able to die at any moment without losing a ruling.
- Operator rulings are recorded dated in the target-state document. A child SDD builds on recorded
  rulings rather than reopening them; when the operator changes one, record the change with its date
  and rationale and update the ledger and direction to affected efforts in the same round.
- The lead seeds child efforts (FRDs plus settled constraints), reviews their PRs, and keeps the
  ledger honest. The lead never edits a child effort's implementation or its lead-owned artifacts;
  findings and direction flow through PR comments and the message-passing convention in the `sdd`
  skill.

## Watch child efforts; review without being asked

- At session start, enumerate in-flight child PRs (`gh pr list`) and check the ledger for efforts
  whose next PR is expected. Arm a background watch for each open draft flipping to ready (and for
  unexpected close or merge, so silence cannot mask a surprise). Review a PR when it goes ready
  without waiting for the operator to ask.
- Re-review on push after an effort absorbs findings. A review verdict stands until the findings are
  absorbed or successfully pushed back on; track the open verdicts in the ledger.
- The lead reviews; the operator merges. Never merge a child PR. When the forge refuses a formal
  review verdict (a PR owned by the same account), post the review as a comment with the verdict
  stated in the first line.

## The review protocol

This protocol layers on top of the effort's own process, never in place of it: the effort runs its
own per-step `agentworks-reviewer` reviews per `agentic-dev-process` section 5, and a PR that
arrives without them goes back to the effort rather than being reviewed harder here. Scale the depth
to the PR's size and blast radius; a doc-only seed gets a single lead pass, a subsystem lands the
full protocol. For substantive PRs, launch parallel review passes as subagents, each in an isolated
worktree at the PR head (per the delegation rules in `agentic-dev-process`, including explicit model
tiers: reviewer at or above the effort's dev tier, top tier for the contract and security
dimensions; pass 2 is the sanctioned fresh-eyes exception from `agentic-dev-process` section 7 and
may run below that floor). Agent types follow what each pass does: the `agentworks-reviewer` persona
deliberately does not execute changes, so it carries the read-based conformance pass, while passes
that run gates, probes, or mutations (2, 3, and usually 4) launch as `general-purpose` subagents,
which is also why the worktree isolation above is load-bearing:

1. **Ruling conformance** (top tier): the one pass only the roadmap lead can charter, verifying the
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
before posting; every surviving finding carries file:line and a concrete failure scenario. Findings
on code loop to the effort's implementing dev; artifact and process items go to the effort lead;
genuinely operator-level decisions are escalated with a recommendation, not an open question.

## After each round

- Update the ledger (new boxes for new work; completed boxes are immutable).
- Feed durable lessons to their permanent homes: process lessons to the skills (via their own
  reviewed PRs), vocabulary and posture changes to the target-state document as dated rulings, and
  repo defects discovered incidentally to the issue tracker rather than the review.
- Tell the operator the verdict plainly, findings-first, including what the other in-flight efforts
  need to hear about it; cross-effort implications are the lead's to route, not the reviewers'.
