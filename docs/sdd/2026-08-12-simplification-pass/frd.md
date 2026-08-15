# Simplification Pass: Functional Requirements

Effort start: 2026-08-12. Parent: the `2026-08-04-next-steps` saga (adopted as a child by operator
ruling, 2026-08-13; its `phasing.md` carries the ordering against the grammar rewrite). The saga
lead currently owns these artifacts, pending handoff to an implementing effort lead. Evidence base:
[findings.md](findings.md), the consolidated inventory from a seven-lane review of the
2026-08-06..12 merge window.

## Background

Six days of high-velocity agentic development merged 68 PRs (~130k added lines). A structured study
found the core models sound but identified a recurring scaffolding tax around them: adversarial
validation of first-party content, tests that police form rather than behavior, per-effort
duplication, speculative generality, and prose register drift.

The first draft of this SDD planned a 35-item coordinated program to fix all of it. Review feedback
(2026-08-13) correctly identified that shape as the disease recurring in the cure, and the operator
ruled the effort down to three steps: bare minimum rule adjustment, deletion waves, then reassess.
This FRD is that shape. Everything that is not subtraction is out of scope here and recorded as a
candidate for later, separately justified work.

## Operator rulings

Settled inputs, recorded for traceability:

1. **2026-08-12**: External plugins are a real, near-term goal. Checks that defend a genuine dynamic
   boundary are kept; the rest is not preserved for a future that can design against the real
   channel when it exists.
2. **2026-08-12**: 0.14 is a large breaking release; contract-truth fixes ride break windows rather
   than accumulating compat. (The 0.14 items themselves are out of scope here; see below.)
3. **2026-08-12**: Migration guidance for breaking changes flows through commit footers, release
   notes, and the self-documenting guide features, not long-lived compat code
   ([migration-strategy.md](migration-strategy.md)).
4. **2026-08-13**: The pass runs as: minimal guidance amendment first, then deletion waves, then a
   stop-and-reassess gate. No redesigns, no new production contracts, no new process machinery ride
   the waves.
5. **2026-08-14 (materiality bar)**: findings are weighed by whether leaving them would change what
   someone builds, concludes, or does next; only material findings gate disposition, re-review, or
   merge. Canonical home: `agentic-dev-process` section 5, with the reviewer, tester, and saga-lead
   surfaces pointing at it. Landed with wave 0 (PR #515).
6. **2026-08-14 (Copilot delivery fallback, R1.0 branch b)**: rulesync v7.14.0 couples Copilot's
   `applyTo` to `globs`, so eager Claude delivery and path-wide Copilot delivery cannot both come
   from one key. Accepted fallback: Copilot moves to reference-based delivery via the always-applied
   `.github/copilot-instructions.md` pointer, the same trust level Codex already has through
   `AGENTS.md` references.
7. **2026-08-14 (wave 2 ownership)**: the implementing effort lead owns both waves. Running them as
   parallel worktree sessions or serializing them is the lead's call. R3.3's file-disjointness still
   binds: wave 2 stays in `.rulesync/` and the generated rule and skill trees, wave 1 stays in
   `cli/` and `website/`.
8. **2026-08-14 (website deferral)**: wave 1's website-touching work waits for PR #486
   (continuous-lander) to merge. That PR is actively rewriting `test_pages_workflows.py`,
   `test_lander_404.py`, `lander-model.test.mjs`, and `site_validation.py`, which are exactly the
   files W1, W4, W5, W6, W8 and the sweep's website rows would edit. This is the coordination the
   plan's wave 1 preamble called for, resolved by ordering rather than by negotiation.
9. **2026-08-14 (source-guard split)**: source-scanning guards do not delete as one shape. What
   decides a guard is what its assertion protects, not that it inspects source: a guard enforcing a
   boundary the type system cannot express stays, and a guard pinning how our code is written goes.
   [hla.md](hla.md) doctrine 2 carries the split. The tiebreaker for a guard that reads as both, an
   observational twin displacing it where one exists or is cheap, is the lead's, derived from the PR
   #523 precedent rather than ruled here.
10. **2026-08-14 (`match=` taxonomy)**: `match=` is decided per site by a three-case taxonomy rather
    than one verdict, since deleting wholesale drops real branch coverage while preserving it by
    adding a production discriminator is what R2.2 forbids. Discriminate structurally where the code
    offers a handle; where only wording we author discriminates, the assertion goes and the branch
    coverage goes with it (R2.4). No case matches our own wording. [hla.md](hla.md) doctrine 2
    carries the cases.
11. **2026-08-14 (caller-supplied arguments)**: an argument arriving from a caller our type checker
    does not check is a trust boundary, not interior state, ruled after PR #523 deleted an
    interior-looking enum check that four review lenses cleared and a live `interaction="refuse"`
    call then resolved a real secret. R1.1 carries it into `development-principles`;
    [hla.md](hla.md) doctrine 1 carries the enumeration and the provenance test it turns on.

## Requirements

### R1 (Wave 0): rule delivery, then the amendments

- R1.0: Establish that always-on rules actually reach the agents they bind, before adding criteria
  to those rules (operator direction, 2026-08-13; issue #511 is the tracking home). The expected
  resolution is removing the path filter: `globs`/`paths:` frontmatter is what forces lazy loading,
  and the frontmatter-free `always-consider-*` rules already load eagerly. Probe delivery across the
  configured targets (Claude, Codex, Copilot), confirm what Rulesync emits for a rule without
  `globs`, then drop the filter from the twelve broad rules; `cli-conventions.md` keeps its narrow
  globs on purpose. If the probe surfaces a reason this simple shape cannot work, escalate to the
  operator rather than building a delivery mechanism.
- R1.1: The trust-boundary doctrine (the five boundaries, interior trust, and the
  validator-names-its-boundary convention; hla.md doctrine 1) is folded into
  `development-principles` as a compact amendment. In the same amendment, principle 3 gains a
  test-quality counterweight (operator direction, 2026-08-13): a test earns its place by guarding an
  invariant that can actually regress; a test that can only fail when someone edits the thing it
  restates is cost, not coverage; assert behavior at a boundary, not the shape of the
  implementation, and deleting a worthless test is the same virtue as writing a worthy one. No new
  rule file.
- R1.2: `no-prose-policing-tests` gains a short generalization: the rule's target is every authored
  artifact (prose, config files, workflow files, CSS tokens, the spelling of our own source), not
  prose alone; when two artifacts must agree, derive one from the other and test the derivation. No
  new rule file.
- R1.3: Wave 0 merges before any wave 1 PR, and wave 1 delegation charters cite both amendments, so
  the deletion criteria are on main before deletions are judged against them. Citation is a
  supplement, never delivery: R1.0 completes only on one of two measurable branches, (a) the twelve
  rules verified delivered unconditionally per target, or (b) a recorded operator disposition whose
  fallback places the full criteria text into every affected lane (for example, carried verbatim in
  each charter). Until one branch holds, wave 1 does not start.

### R2 (Wave 1): deletion

The charter, applied per PR and judged locally: keep behavioral, structural, and security tests;
delete form-policing and prose-policing tests; delete dead surface and speculative generality with
no shipped consumer; delete validation whose input is clearly interior. A replacement is added only
where a real, regressable invariant would otherwise go unguarded, and a test is never sufficient
justification for a new production contract.

- R2.1: Before any validator is deleted, its input's provenance is classified against the boundary
  list in [hla.md](hla.md) doctrine 1, and a validator guarding any of those boundaries stays. The
  list lives there and nowhere else: this requirement carried a partial copy through two revisions,
  and both times the copy was missing the boundary that mattered.
- R2.2: No wave 1 PR introduces a new production type, changes a shipped contract, or requires an
  LLD. Work that turns out to need any of those is set aside for the reassessment instead.
- R2.3: Real coverage gaps found beneath deleted ceremony are closed in the same PR where the
  invariant is real (the persisted-enum parity gap, G11, is the standing example).
- R2.4: The prose-test-purge effort remains absorbed (its seed FRD is superseded in place; see its
  `locked.md`). Its estate is handled under this wave's charter: sentence-only observables are
  decided case by case, mostly by deletion, never by mandated new observables.

### R3 (Wave 2): process and rule subtraction

- R3.1: Duplicated rules and testing guidance consolidate under a strict net-deletion constraint:
  the three principle-absorbed rules delete, the five collateral-sync rules become one, the
  published-review-authority statement keeps one canonical home, and the testing trio's diverged or
  contradictory copies get one authoritative home while deliberate cross-perspective reinforcement
  stays (operator caution, 2026-08-12).
- R3.2: A journey-and-register trim removes narration and definitional prose from operating
  instructions. Always-on rule bytes are reported before and after and must go down.
- R3.3: No persona changes and no new delivery mechanisms ride this wave. The rule-delivery gap is
  wave 0's to resolve (R1.0); this wave's subtraction builds on whatever delivery shape wave 0
  landed. Wave 2 runs in parallel with wave 1 on its own session (operator, 2026-08-13),
  file-disjoint from it; the R4 reassessment waits for both waves.

### R4: stop and reassess

- R4.1: After waves 1 and 2 and after the CLI grammar rewrite lands (the saga's `phasing.md` places
  the rewrite between wave 1 and this reassessment), the lead writes a reassessment: what measurably
  got simpler (fewer concepts, paths, and contracts is the test; line and test counts are
  retrospective evidence, not targets), what findings survive, and which are worth promoting.
- R4.2: Surviving subsystem findings are proposed as separate, bounded, per-subsystem efforts on
  their own merits. Nothing is pre-authorized by this SDD.

## Out of scope, recorded as candidates

- **The 0.14 contract-truth items** (S5 backends-to-sources, C3 token-union collapse, C4 compat
  flag, C7 compat expiry, and the migration-notes convention): out of scope for this pass. Routed
  (operator, 2026-08-13): they run as their own dispatched task, briefed on the
  `refactor/breaking-truth-0-14` branch, in parallel with the deletion waves. That task owns
  `env/entry.py` and the token union; this pass owns the inert descriptor fields (C1, C5).
  [migration-strategy.md](migration-strategy.md) is the task's authoritative strategy, read from
  this directory.
- **Subsystem redesigns** (D1/D2 database classification and sentinels, S3/S4/S6 secrets protocol
  and resolve paths, G1/G3 guide boundary surgery, C8 `ResolvedSessionTemplate`, G5 JSON/human
  traversal, W7/W9/W10 website philosophy and lander scope, P1/P2 platform extractions, P4 error
  taxonomy): deferred to the R4 reassessment, proposed individually if still warranted.
- **External-plugin trust design**: not designed or promoted in this pass. The checks kept by the
  wave 1 charter (constructibility, call-shape compatibility at registration) are the seam; findings
  and the boundary sketch go into the reassessment as seed material for the loader effort.
- **New rule-delivery machinery**: wave 0 resolves the delivery gap by removing the path filter
  (R1.0), never by building a delivery mechanism; anything beyond that shape escalates.
- No new user-facing features, no harness changes, no SDD-directory cleanup beyond the directed
  prose-test-purge supersession.

## Acceptance

- Wave 0 is on main before the first wave 1 PR merges.
- Every wave PR is green on the full suite and reviewed; wave 2 lands net-negative on process bytes.
- The reassessment exists, states what became simpler in concept-and-contract terms, reports the
  retrospective numbers (lines, test counts, suite wall time, always-on bytes), and carries the
  candidate list with the saga flags delivered.
