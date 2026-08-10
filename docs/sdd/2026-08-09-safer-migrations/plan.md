# Plan: Safer Database Migrations

<!-- cspell:ignore sdds -->

- Status: Active
- Builds on: [frd.md](./frd.md), [hla.md](./hla.md),
  [migration-strategy.md](./migration-strategy.md)
- Saga: `docs/sdd/2026-08-04-next-steps/`

This plan keeps the effort to one SQLite safety service, one CLI noun group, one setting, and the
existing migration ladder. Completed checkboxes are immutable. The effort lead alone updates them
after the corresponding implementation and independent review are complete. Every implementation
phase receives project-specific review; the completed change receives a separate fresh-eyes review.
Commits remain independently green.

Full gate:

```console
cd cli
uv run ruff check agentworks/ tests/
uv run ruff format --check agentworks/ tests/
uv run mypy agentworks/ tests/
uv run pytest tests/ -v -m 'not integration'
cd ..
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
```

No live VM is needed: the shipped boundary is local SQLite, filesystem, config, completion, and CLI
behavior. Live validation uses a temporary isolated home and no operator state.

## Phase 0: SDD review

- [x] Independently review and approve `frd.md` requirements and non-goals.
- [x] Independently review and approve `hla.md`, its primary-source research, and the decision not
      to add a separate LLD.
- [x] Independently review and approve this plan and `migration-strategy.md`; incorporate every
      valid finding before implementation.
- [x] Independently review the artifact amendments from the design-test findings: structural restore
      validation, bounded backup waits, restrictive creation, serialized migration recheck, and
      explicit completion short-circuit. Record the review evidence in a signed PR comment.
- [x] Independently review the saga-conditional amendments: version-appropriate restore schema,
      first-observation lock qualification, suppression-surviving notices, and legacy installed
      completion safety.
- [x] Obtain the saga lead's phased artifact ruling and confirm the singular `database` CLI home.
- [x] Delete the branch-seeded task brief before taking the artifact PR out of draft.
- DoD: the functional contract, architecture, compatibility path, implementation sequence, and scope
  guard are reviewed; the artifact PR is merge-ready; no implementation has begun under an unsettled
  requirement.

## Phase 1: Direct backup and restore

- [ ] Add the focused `agentworks.db` online-copy service with adjacent backup directory creation,
      separate manual and automatic names, collision safety, source validation, restore direction,
      incomplete-destination cleanup, restrictive file creation, a fixed busy deadline, and
      automatic-only retention of five. Put the version-appropriate schema-sentinel map beside the
      migration ladder with its future-maintenance comment.
- [ ] Add the singular `agw database` group with `backup` and `restore BACKUP_PATH [--yes/-y]`.
      Neither command may call `get_db()` or construct the migrating `Database` facade.
- [ ] Keep backup status and restore confirmation on stderr, keep the successful backup path as the
      only stdout value, and refuse non-interactive restore without `--yes`.
- [ ] Add native restore-file completion in bash, zsh, PowerShell, and completion specs; update the
      CLI README and command reference with the now-shipped command behavior.
- [ ] Test WAL-visible copy, validation-before-destination-open, source/destination direction,
      generic SQLite rejection, the version-appropriate map across every historical schema version,
      current-version common-sentinel lookalike rejection, future-version restore refusal before
      destination open, identical-path refusal, held-lock deadline, restrictive POSIX modes,
      collision handling, cleanup, mixed-version retention ordering, manual retention immunity,
      confirmation without an implicit pre-restore backup, stdout purity, and all generated
      completion surfaces.
- DoD: on-demand backup and confirmed restore work without migration side effects; targeted tests,
  the complete gate, project-specific review, and mutation of the copy direction all pass.

## Phase 2: Safe automatic migration flow

- [ ] Add WAL-aware non-migrating schema inspection and safe writable-open sequencing to the same
      database safety service. Serialize stale opens with one persistent dedicated SQLite lock,
      qualify the first stale observation under that lock, recheck after interaction, refuse an
      overlapped or changed-but-stale observation, refuse malformed and future schema, complete any
      selected backup before `Database` construction, close construction failures, and raise a
      kind-based `StateError` with the exact platform-aware recovery command or the explicit
      no-backup fact.
- [ ] Add the strict default-true `DatabaseConfig` and focused `[database]` loader. Full config
      includes the section; only stale non-interactive opens use the focused projection.
- [ ] Update `get_db()` to own notice, interactive default-yes prompt, and non-interactive setting
      selection while delegating safety ordering to the service. Catch a selected backup's existing
      `BackupError` here and add the mode-specific explicit-decline or config-opt-out retry hint.
      Add suppression-surviving `output.notice()` for the mandatory versioned notice and automatic
      backup status. Prompt only when stdin and the stderr prompt stream are terminals.
- [ ] Add the hidden completion-probe marker. When sidecars exist, return no candidates; otherwise
      use an immutable read-only connection. Unavailable probe state raises a clean error before a
      database caller runs; generated shell code discards its stderr and consumes empty stdout,
      including when config warned or failed earlier. Recognize the pre-0.14 marker-free completion
      argv/TTY shape in the root callback from the shared database-backed command-path set and
      refuse it before prompt or migration. Completion must never prompt, migrate, create a
      database, or create or change SQLite sidecars.
- [ ] Make `Database` refuse future schema and close on migration failure. Keep the existing
      migration ladder and per-version commit behavior unchanged. Make doctor use the shared
      non-migrating, WAL-aware inspection path and align its wording.
- [ ] Update `sample-config.toml`, config reference material, the 0.14 upgrade guide, and
      `concept-migration` with backup location, fixed retention, opt-out behavior, failure recovery,
      restore-before-downgrade order, and the required `agw completion install` refresh.
- [ ] Test fresh/current/stale/future/malformed matrices; interactive accept and decline;
      non-interactive default, opt-out, and invalid focused config; backup-before-first-statement;
      backup failure prevention and retry guidance; partial failure recovery; POSIX and PowerShell
      command rendering; two-process serialized recheck with one backup; staggered late-inspector
      partial-migration refusal; interactive JSON and names-only stdout purity with the required
      stderr notice; doctor; the single writable-construction-site inventory; and byte-identical
      zero-output completion probes, including shell-wrapped warning, invalid-config, and pre-0.14
      marker-free cases.
- DoD: every writable production open follows the reviewed safety flow, every completion probe is
  non-mutating, all permanent teaching matches behavior, and the complete gate and project-specific
  review have no unresolved valid finding.

## Phase 3: Integrated validation and closeout

- [ ] As the invoking session, fetch the PR's real head and base; prove the checkout matches the
      remote head; and check freshness and merge conflicts with current `main`. Save and preserve an
      `agw-state` snapshot first only if the planned run can touch operator state.
- [ ] Run four mutation checks: force migration before backup, bypass initial stale qualification,
      remove the post-interaction schema recheck, and force completion through a writable open. Each
      must fail a focused test; restore production behavior afterward.
- [ ] Run the full gate from a clean tree and record the exact commands and results in the PR.
- [ ] Have an independent fresh-eyes reviewer inspect the complete implementation, tests, docs, and
      SDD traceability; return every valid finding to the implementing agent and re-review fixes.
- [ ] As the invoking session, load `integration-testing` and `agw-test-env`, select the local-only
      inventory and a bounded resource budget, and inject the relevant environment, safety,
      freshness, cleanup, and disposition charter into an `agentworks-tester`. Have it exercise the
      installed real CLI with an isolated temporary home: manual backup/restore, automatic
      stale-schema backup, opt-out, migration failure remediation, JSON/names-only purity, and
      non-mutating completion. The tester returns evidence only; the invoking session independently
      verifies cleanup, decides the operator-gated disposition, and posts the signed PR comment.
- [ ] Obtain the saga lead's final implementation ruling after code, review, gate, and live-test
      findings converge.
- [ ] Classify residual migration claims and completion call sites, confirm the brief remains
      absent, update all plan checkboxes truthfully, and create `locked.md` containing final state
      and evidence.
- DoD: full gates, mutation checks, fresh-eyes review, and isolated CLI validation are clear; the PR
  has no unresolved valid finding, carries signed evidence, is out of draft, and is ready for the
  operator to merge.

## Traceability

- R1: Phase 2.
- R2: Phase 2.
- R3: Phase 2.
- R4: Phase 1.
- R5: Phase 1.
- R6: Phase 1.
- R7: Phase 1.
- R8: Phase 2.
- R9: Phase 2.
- R10: Phases 1 and 2.
- R11: Phase 2.
- R12: Phases 1 and 2.
- R13: Phase 2.
- R14: Phase 2.
- AC1: Phase 2 and Phase 3 isolated CLI validation.
- AC2: Phase 2 and Phase 3 isolated CLI validation.
- AC3: Phase 2 partial-failure test and Phase 3 mutation and CLI validation.
- AC4: Phase 1 WAL-visible service test and Phase 3 CLI validation.
- AC5: Phase 1 retention and restrictive-creation tests.
- AC6: Phase 1 restore-validation tests and Phase 3 CLI validation.
- AC7: Phase 2 doctor matrix and Phase 3 full gate.
- AC8: Phases 1 and 2 focused tests and Phase 3 full pipeline.
- AC9: Phase 2 completion and stdout-purity tests and Phase 3 CLI validation.
- AC10: Phases 1 and 2 future-schema tests and Phase 3 CLI validation.
- AC11: Phase 2 backup-failure ordering tests and Phase 3 mutation validation.
- AC12: Phase 2 two-process serialization test and Phase 3 mutation validation.
- AC13: Phase 1 held-lock deadline test and Phase 3 CLI validation.
