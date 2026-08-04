# Plan: Session Resume Rename

<!-- cspell:ignore sdds -->

- Status: Draft
- Builds on: `frd.md`, `hla.md`, `migration-strategy.md`

This plan lands the canonical `resume` vocabulary and the 0.13.0 compatibility shims in one feature
PR based on the harness-integration rename branch. The 0.14.0 removals are tracked as a later phase;
the SDD remains open until those removals land.

Completed checkboxes are immutable. Every implementation step receives project-specific review.
Code-heavy steps also receive a fresh-eyes correctness review. Commits remain independently green.

Full gate:

```console
cd cli
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest -q
cd ..
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
```

## Phase 0: SDD review

- [x] Review and approve `frd.md` requirements and non-goals.
- [x] Review and approve `hla.md` boundaries and `migration-strategy.md` compatibility matrices.
- [x] Revise this plan if review changes the contract. No revisions were required.
- DoD: the operator approves the functional contract, architecture, migration policy, removal
  release, and implementation plan before code changes begin.

## Phase 1: Canonical integration and manager APIs

- [ ] Rename `HarnessIntegration.restart(ctx)` to `resume(ctx)` in the abstract base, shell, Claude
      Code, and Codex implementations. Rename operation-specific locals and documentation without
      changing algorithms.
- [ ] Rename `restart_session` and `restart_all_sessions` to `resume_session` and
      `resume_all_sessions`, including manager exports and every live caller. Rename
      operation-specific contexts, log operation names, output, hints, errors, fixtures, and tests.
- [ ] Preserve mechanical terminology for actual process, tmux, VM, and service restarts.
- [ ] Run the integration, session lifecycle, transport, filters, secret-resolution, and
      orchestrated test subsets; then run the Python quality gates.
- DoD: live internal APIs expose only `resume`; the pre-change lifecycle scenarios pass with no
  behavioral assertion weakened; project-specific and fresh-eyes reviews have no unresolved valid
  findings.

## Phase 2: Canonical CLI and deprecated alias

- [ ] Add `agw session resume` with the existing single and batch interface. Extract one private
      execution helper shared by canonical and deprecated command callbacks.
- [ ] Convert `agw session restart` into a one-release wrapper that emits the exact warning in
      `migration-strategy.md`, respects `--no-deprecations`, and delegates without duplicating
      validation or lifecycle logic.
- [ ] Update dynamic completion mappings for `session.resume`; retain parity mappings for
      `session.restart` during 0.13.0 and mark the latter deprecated in help.
- [ ] Add parity tests for successful single and batch execution, validation errors, lifecycle
      errors, exactly-one warning behavior, suppression, and help/completion discovery.
- DoD: `resume` is canonical; both spellings have identical functional behavior in 0.13.0; only the
  old spelling warns; CLI, completion, and targeted lifecycle tests pass; reviews are clear.

## Phase 3: Shell config rename and compatibility

- [ ] Make `resume_command` canonical in shell validation, merge, and runtime selection, falling
      back to `command` as before.
- [ ] Normalize old `restart_command` inputs before inheritance, aggregate a suppressible
      deprecation warning, and reject every local or inherited mixed-spelling case in the migration
      matrix.
- [ ] Teach `agw resource migrate` to emit `resume_command` and to fail atomically on conflicts.
- [ ] Update deprecated-field metadata, sample resources, package validation, and relevant fixtures.
- [ ] Cover every row in `migration-strategy.md` section 4, including YAML, deprecated TOML,
      inheritance, suppression, migration output, and rollback on migration failure.
- DoD: canonical input is warning-free; every previously valid old-only input works with guidance;
  ambiguous input fails; emitted config is canonical; config, manifest, migration, and sample tests
  pass; reviews are clear.

## Phase 4: Current documentation and terminology sweep

- [ ] Update current operator docs, CLI README tables and examples, resource guide, sample manifest,
      capability README, ADRs, code comments, and user-visible hints to use `resume`.
- [ ] Update the active `docs/sdd/2026-08-03-harness-integration` artifacts where they specify the
      current integration method or session command. Do not rewrite completed checkbox text; append
      corrective notes or new work items where the SDD mutability rule requires it.
- [ ] Leave historical changelog entries and locked historical SDDs unchanged.
- [ ] Run the classified residual searches from `migration-strategy.md`; record or eliminate every
      live match.
- [ ] Run the documentation linter and full gate.
- DoD: no current canonical surface teaches the old name; remaining matches are compatibility,
  migration, history, or real mechanical restarts; reviews and all gates are clear.

## Phase 5: 0.13.0 release readiness

- [ ] Add release notes describing `session resume`, the one-release command alias,
      `resume_command`, the old config-field warning, downgrade considerations, and the 0.14.0
      removal.
- [ ] Verify the installed CLI command tree and generated completions, not only unit-level specs.
- [ ] Exercise representative real or orchestrated Claude Code, Codex, and shell sessions through
      `session resume`; exercise the deprecated command once with and once without suppression.
- DoD: the full gate passes, release-facing docs are complete, observed command behavior matches the
  FRD, and no valid review finding remains.

## Phase 6: 0.14.0 removal

This phase is blocked until 0.13.0 has shipped.

- [ ] Delete the `session restart` command wrapper, warning, completion mappings, and compatibility
      tests; assert the old command is unknown.
- [ ] Delete `restart_command` normalization, warning, migrator rewrite, and old-input fixtures;
      assert the old key is rejected.
- [ ] Remove compatibility-only exceptions from the residual inventory and add release notes.
- DoD: only canonical `resume` surfaces remain in live code and current docs; old inputs fail
  clearly; the full gate and reviews pass.

## Phase 7: Closeout

- [ ] Confirm all load-bearing behavior and plugin-author guidance lives in permanent docs.
- [ ] Create `locked.md` with the final state and release timeline.
- DoD: every applicable plan item is complete, permanent docs reflect HEAD, and the locked SDD is
  safe to remove without losing current-system knowledge.

## Traceability

- R1: Phases 1 and 2.
- R2: Phases 2 and 6.
- R3: Phases 1, 2, and 5.
- R4: Phases 1 and 4.
- R5: Phase 1.
- R6: Phases 3 and 6.
- R7: Phases 2, 4, and 5.
- R8: Phase 2.
