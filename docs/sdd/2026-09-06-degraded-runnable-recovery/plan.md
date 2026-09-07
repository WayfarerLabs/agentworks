# Implementation Plan: Degraded Runnable Recovery

- Status: Implementation
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [degraded-inventory-lld.md](./degraded-inventory-lld.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `b22cc49c9827cb38fb7d8fc77522b436b318ac03`
- Delivery: one design-and-implementation PR based on `main`

## Delivery rules

- Keep the PR draft during design review and implementation. Mark ready only when implementation,
  documentation, reviews, exact-head CI, and operator-gated live validation are complete.
- Run up to three authorized design feedback/fix rounds. Proceed to implementation on the same PR
  only after design converges.
- Run up to three additional authorized feedback/fix rounds on the final implementation.
- Wait the standard one-hour published feedback window in each round unless the operator changes it.
- Do not merge the PR; the operator owns merge.

## Phase 1: Discovery and design

- [x] Rebase the effort on current `main` after PR #756.
- [x] Reproduce the three structural failure boundaries from source and tests.
- [x] Confirm filter, names-only, JSON, describe, lifecycle, and migration-safe-open behavior.
- [x] Research SQLite foreign-key diagnostics, Python exception semantics, and JSON null.
- [x] Write the FRD, HLA, LLD, research record, and implementation plan.
- [x] Run file formatting, Markdown, spell, locked-SDD, and diff consistency checks.
- [x] Run private project-values and Muntz design reviews and resolve material findings.
- [x] Publish the draft design PR and complete three design feedback/fix rounds.

## Phase 2: Test-first implementation

- [x] Add current-schema fixtures for missing session workspace, missing session VM, and missing
      console VM alongside healthy peers.
- [x] Add behavioral coverage for plain human inventory, internal nullable VM projection, strict
      JSON v1 behavior, stable ordering, filters, names-only behavior, and no external calls or
      database writes.
- [x] Add behavioral coverage proving session and console status observe healthy peers, leave
      orphans unknown, and construct no orphan transport.
- [x] Preserve shared observer strictness and focused typed failures with explicit tests.
- [x] Add stale-schema migration coverage for typed direct and safe-open refusal, unchanged version
      checkpoint, and backup recovery behavior.
- [x] Add restore-source coverage for foreign-key violation reporting, default pre-copy refusal,
      narrow forced acceptance, unchanged validation of every other source property, and final
      snapshot identity across source commits or path replacement.
- [x] Add CLI coverage that `--force` and `--yes` remain independent and that a forced inconsistent
      restore warns before confirmation and after successful replacement.
- [x] Implement the list-only session VM projection and human-list nullable VM facts.
- [x] Implement per-row session and console status partitioning in the list services while retaining
      strict shared observers.
- [x] Translate migration foreign-key violations at the database boundary.
- [x] Add a prepared-restore boundary that pins one inspected SQLite snapshot through confirmation
      and copy while preserving the public validator and restore return contracts.
- [x] Extend restore-source validation with a narrow `allow_foreign_key_violations` policy; map CLI
      `--force` to it without weakening other checks.

## Phase 3: Permanent collateral

- [x] Update `cli/command-reference.md` for recovery inventory, status isolation, filter
      reachability, the JSON v1 recovery limit, and the warned restore bypass.
- [x] Update `docs/guides/runnable-status.md` with recovery and migration behavior.
- [x] Add a dated correction to the locked runnable-status SDD.
- [x] Update any README, completion, sample, or guide topic made stale by the implementation.

## Phase 4: Verification and review

- [x] Run focused session, console, machine-output, migration, CLI, filter, renderer, and safety
      suites.
- [x] Run Ruff check and format, strict mypy, file lint, locked-SDD, Rulesync drift, diff, release
      residual, and the full non-integration Python suite.
- [ ] Build an isolated wheel and validate shipped CLI human and JSON behavior against healthy and
      synthetic degraded state without residue.
- [ ] Run private project-values, Muntz, cold correctness/security, and integration-test review
      lanes appropriate to the final delta; resolve material findings.
- [ ] Push the exact checkpoint, obtain hosted CI success, and complete operator-gated live
      validation or record the operator's explicit disposition.
- [ ] Complete up to three authorized final feedback/fix rounds.

## Phase 5: Closeout

- [ ] Reconcile every requirement and definition of done against exact HEAD.
- [ ] Update this plan with truthful completed work and create `locked.md` summarizing final
      reality.
- [ ] Ensure no load-bearing maintenance guidance exists only under `docs/sdd/`.
- [ ] Mark ready with merge intent and a signed exact-head handoff after all gates converge.

## Definition of done

- [ ] Plain session and console inventory returns every selected stored row in degraded
      current-schema state without external work or writes.
- [ ] Requested status isolates incomplete structure per row and retains healthy peer results.
- [ ] Focused and mutating operations stay strict.
- [ ] Unsafe migration refuses through typed Agentworks errors without advancing its checkpoint.
- [ ] Restore rejects foreign-key violations by default; `--force` accepts only those violations,
      remains independent from `--yes`, and warns before and after replacement.
- [ ] Human, strict JSON v1, filter, names-only, docs, and locked design records agree.
- [ ] Local gates, private reviews, hosted CI, shipped-CLI validation, published feedback, and
      operator disposition are complete at the ready commit.
