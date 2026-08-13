# Simplification Pass: Functional Requirements

Effort start: 2026-08-12. Evidence base: [findings.md](findings.md), the consolidated inventory from
a seven-lane review of the 2026-08-06..12 merge window.

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

## Requirements

### R1 (Wave 0): bare minimum rule adjustment

- R1.1: The trust-boundary doctrine (the four boundaries, interior trust, and the
  validator-names-its-boundary convention; hla.md doctrine 1) is folded into
  `development-principles` as a compact amendment. No new rule file.
- R1.2: `no-prose-policing-tests` gains a short generalization: the rule's target is every authored
  artifact (prose, config files, workflow files, CSS tokens, the spelling of our own source), not
  prose alone; when two artifacts must agree, derive one from the other and test the derivation. No
  new rule file.
- R1.3: Wave 0 merges before any wave 1 PR, and wave 1 delegation charters cite both amendments, so
  the deletion criteria are on main before deletions are judged against them.

### R2 (Wave 1): deletion

The charter, applied per PR and judged locally: keep behavioral, structural, and security tests;
delete form-policing and prose-policing tests; delete dead surface and speculative generality with
no shipped consumer; delete validation whose input is clearly interior. A replacement is added only
where a real, regressable invariant would otherwise go unguarded, and a test is never sufficient
justification for a new production contract.

- R2.1: Before any validator is deleted, its input's provenance is classified against the R1.1
  boundary list; validators guarding operator input, external processes, or persisted
  cross-execution state stay.
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
- R3.3: No persona changes, no new delivery mechanisms, no probes ride this wave. The rule-delivery
  gap (findings, rule-delivery section) is filed as its own tracked issue and pursued separately.

### R4: stop and reassess

- R4.1: After waves 1 and 2, the lead writes a reassessment: what measurably got simpler (fewer
  concepts, paths, and contracts is the test; line and test counts are retrospective evidence, not
  targets), what findings survive, and which are worth promoting.
- R4.2: Surviving subsystem findings are proposed as separate, bounded, per-subsystem efforts on
  their own merits. Nothing is pre-authorized by this SDD.

## Out of scope, recorded as candidates

- **The 0.14 contract-truth items** (S5 backends-to-sources, C3 token-union collapse, C4 compat
  flag, C7 compat expiry, and the migration-notes convention): out of scope for this pass, and
  recorded as candidates for the broader saga, with [migration-strategy.md](migration-strategy.md)
  as their seed. The saga lead routes them; flagged through the operator.
- **Subsystem redesigns** (D1/D2 database classification and sentinels, S3/S4/S6 secrets protocol
  and resolve paths, G1/G3 guide boundary surgery, C8 `ResolvedSessionTemplate`, G5 JSON/human
  traversal, W7/W9/W10 website philosophy and lander scope, P1/P2 platform extractions, P4 error
  taxonomy): deferred to the R4 reassessment, proposed individually if still warranted.
- **External-plugin trust design**: not designed or promoted in this pass. The checks kept by the
  wave 1 charter (constructibility, call-shape compatibility at registration) are the seam; findings
  and the boundary sketch go into the reassessment as seed material for the loader effort.
- **The rule-delivery bug**: filed as its own issue (R3.3), not fixed by in-repo machinery here.
- No new user-facing features, no harness changes, no SDD-directory cleanup beyond the directed
  prose-test-purge supersession.

## Acceptance

- Wave 0 is on main before the first wave 1 PR merges.
- Every wave PR is green on the full suite and reviewed; wave 2 lands net-negative on process bytes.
- The reassessment exists, states what became simpler in concept-and-contract terms, reports the
  retrospective numbers (lines, test counts, suite wall time, always-on bytes), and carries the
  candidate list with the saga flags delivered.
