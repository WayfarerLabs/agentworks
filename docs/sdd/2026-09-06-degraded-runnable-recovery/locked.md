# Degraded runnable recovery: locked

**Locked:** 2026-09-09

This effort is complete in PR #764. The lock takes effect when that PR lands on `main`; until then,
this file records the final reviewed and operator-accepted implementation state.

## What shipped

- Plain session and console inventory preserves every selected current-schema row when a workspace
  or VM relationship is missing. Human session inventory renders an unknown VM as `-`; strict JSON
  v1 still rejects a session whose required VM name cannot be represented.
- Requested session and console status isolates incomplete rows as `unknown`, skips transport work
  for them, and continues observing complete peers. Named describe and lifecycle operations retain
  strict relationship validation.
- Migration foreign-key failures surface as typed Agentworks state errors without advancing the
  schema checkpoint. Production safe-open recovery guidance remains intact.
- Database restore validates one pinned source snapshot, rejects foreign-key violations by default,
  and accepts only that validation class with `--force`. Forced restore remains independent from
  `--yes` and warns before confirmation and after installation.
- Restore pins the destination through consent and installs a private staged copy while holding a
  canonical-path, SQLite-backed database-use lock. Writable Agentworks databases hold the shared
  side of that lock for their lifetime; replacement holds the exclusive side across installation.
  Existing destinations checkpoint WAL state, enter delete-journal mode, and acquire SQLite writer
  exclusion before atomic replacement. Absent destinations use no-overwrite installation.

## Safety and implementation boundaries

Inventory recovery is deliberately list-only. It does not repair state, widen the machine schema,
infer runtime absence from missing structure, or relax focused operations. The shared session and
console observers remain strict for lifecycle and describe consumers; list services partition rows
before calling those observers.

Restore's `--force` is not a general validation bypass. Malformed SQLite, failed integrity checks,
unsupported schema versions, unexpected schema shapes, altered foreign-key declarations, endpoint
aliasing, and destination replacement remain unconditional refusals. The coordination lock protects
against cooperating Agentworks writers; arbitrary local processes that replace path names remain
outside the supported adversary boundary.

## Verification and review

The feature code checkpoint before current-main reconciliation was `a3aba69a4`. It passed 8,625
non-integration Python tests with 3 platform-specific skips, Ruff check and format, strict mypy,
repository file lint, locked-SDD and Rulesync checks, website suites and deterministic builds, plus
an isolated wheel build and install. All hosted Python, Windows Python 3.13, CodeQL, documentation,
and aggregate CI gates were green on that checkpoint.

Independent project-values, Muntz, cold correctness/security, and integration-test lanes converged.
The final feedback iteration removed redundant destination checks while retaining stage identity at
each installation boundary; it also consolidated SQLite-backed lock acquisition and documented the
read that materializes a deferred shared lock. Real contention coverage proves an open writable
database blocks replacement until close. The focused restore, CLI, and migration safety corpus
passed after that change.

The operator validated the Windows recovery script, including `-ExerciseLiveRestore`: it created a
recovery backup, exercised synthetic recovery and lock behavior, restored the live database, and
confirmed session and console inventory afterward. The final integration lane could not reach the
Windows workstation because no execution connector was exposed, so it made no workstation change;
exact-head hosted Windows coverage ran the three new lock and stage-identity regressions, while the
lane's local exact-head recovery corpus passed 169 tests.

The current-main reconciliation code checkpoint is `3d681d401`. It composes main's session user and
compact-uptime projections with degraded-row isolation, using one shared observation timestamp and
deriving uptime only for observed running sessions. The conflict resolution passed a focused
242-test session and database corpus and the full 8,642-test suite with 3 platform-specific skips,
plus Ruff, strict mypy, repository, locked-SDD, Rulesync, website, and deterministic-build gates.
Exact-head project-values, Muntz, and cold correctness reviews found the composition clean. Hosted
Python, Windows Python 3.13, static-analysis, and repository gates passed. The first hosted website
attempt failed because Chromium did not publish its DevTools endpoint; both website suites and
deterministic builds passed locally, and the final documentation-only head received a clean hosted
replacement run.

## Permanent homes and accepted limits

Operator behavior lives in `cli/command-reference.md`, `docs/guides/runnable-status.md`, and the
locked runnable-status correction. The database backup module and its behavioral tests carry the
restore and coordination contract. Nothing in this SDD directory is required to operate or maintain
the feature.

The database-use lock is cooperative rather than a process-containment or hostile-filesystem
boundary. Strong process containment remains a separate systemd/cgroup concern. The operator owns
merging PR #764; the effort lead does not merge it.
