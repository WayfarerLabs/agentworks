# Implementation Plan: Degraded Runnable Recovery

- Status: Complete; locked on merge of PR #764
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
- [x] Harden prepared restore after cold review by pinning the destination through confirmation,
      delaying absent-destination creation until apply, refusing destination identity changes,
      closing interrupted validation, and validating expected foreign-key declarations independently
      from violating rows. Require regular endpoint files, probe them in non-blocking mode, and
      compare source and destination identities observed around their SQLite opens. Copy into a
      private stage and verify or no-overwrite-install the live path only after that copy completes.
      Before replacing existing state, require a clean WAL checkpoint, delete journal mode, and an
      exclusive writer lock within the bounded wait. Hold a cross-platform database-use lock through
      installation and make writable Agentworks database connections share it so they cannot reopen
      the replacement gap. Resolve the database path before both lock derivation and SQLite open so
      aliases cannot split that coordination.

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
- [x] Build an isolated wheel and validate shipped CLI human and JSON behavior against healthy and
      synthetic degraded state without residue.
- [x] Run private project-values, Muntz, cold correctness/security, and integration-test review
      lanes appropriate to the final delta; resolve material findings.
- [x] Push the exact checkpoint, obtain hosted CI success, and complete operator-gated live
      validation or record the operator's explicit disposition.
- [x] Complete up to three authorized final feedback/fix rounds.

## Phase 5: Closeout

- [x] Reconcile every requirement and definition of done against exact HEAD.
- [x] Update this plan with truthful completed work and create `locked.md` summarizing final
      reality.
- [x] Ensure no load-bearing maintenance guidance exists only under `docs/sdd/`.
- [x] Mark ready with merge intent and a signed exact-head handoff after all gates converge.

## Phase 6: Current-main reconciliation

- [x] Merge `main` at `3cdd3c0f6` and resolve the session-query and command-reference conflicts.
- [x] Preserve degraded-row status isolation while composing main's session user and compact-uptime
      projections with one shared observation timestamp.
- [x] Run the 242-test focused session and database corpus and the full 8,645-test suite with 3
      platform-specific skips on the reconciliation code checkpoint.
- [x] Run Ruff check and format, strict mypy, repository file lint, locked-SDD, Rulesync drift,
      website suites, and deterministic website builds on the reconciled implementation.
- [x] Obtain clean exact-head project-values, Muntz, and cold correctness reviews of the conflict
      resolution.
- [x] Push the reconciliation checkpoint and obtain hosted Python, Windows, static-analysis, and
      repository-gate success. Classify the first website attempt's missing Chromium DevTools
      endpoint as infrastructure after its suites passed locally, then obtain a clean replacement
      run on the final documentation-only head.
- [x] Re-hand off the conflict-free branch as ready after the final exact-head gates converge.

## Definition of done

- [x] Plain session and console inventory returns every selected stored row in degraded
      current-schema state without external work or writes.
- [x] Requested status isolates incomplete structure per row and retains healthy peer results.
- [x] Focused and mutating operations stay strict.
- [x] Unsafe migration refuses through typed Agentworks errors without advancing its checkpoint.
- [x] Restore rejects foreign-key violations by default; `--force` accepts only those violations,
      remains independent from `--yes`, and warns before and after replacement.
- [x] Human, strict JSON v1, filter, names-only, docs, and locked design records agree.
- [x] Local gates, private reviews, hosted CI, shipped-CLI validation, published feedback, and
      operator disposition are complete at the ready commit.
