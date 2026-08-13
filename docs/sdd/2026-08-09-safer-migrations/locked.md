# Safer database migrations: locked

**Locked:** 2026-08-10

This effort is complete. The lock takes effect when implementation PR #478 lands on `main`; until
then, this file records closeout intent on the merge-intent branch. The design artifacts are staged
separately in prerequisite PR #472 so the saga could review the contract before implementation.

## What shipped

- `agw database backup` and confirmed `agw database restore PATH`, implemented as bounded SQLite
  online copies that preserve WAL-visible data without constructing the migrating database facade.
- Adjacent user-only database backups with collision-safe manual and pre-migration names, five-file
  automatic-only retention, source validation, cleanup, and restrictive POSIX creation.
- Exact historical Agentworks table-and-column shape validation for every schema version. Restore
  and first-lock migration qualification share the same canonical comparator, so partially applied
  DDL from the next version cannot masquerade as a completed older schema.
- One persistent SQLite migration lock around stale writable opens. The preliminary observation is
  trigger-only; locked canonical qualification, version/cookie comparison, and the post-interaction
  recheck prevent duplicate migration and misleading recovery guidance across both late-inspector
  windows.
- Mandatory versioned stderr notices, an interactive backup choice that defaults to yes, a strict
  non-interactive config setting that defaults to true, halt-before-migration backup failures, and
  exact restore or no-backup recovery guidance for migration failures and interruptions.
- Non-mutating completion probes: explicit marker support, conservative pre-0.14 script refusal,
  sidecar gating, immutable current-schema reads, stderr suppression in generated shells, and shared
  inventory/parity checks across Bash, Zsh, and PowerShell.
- Permanent operator teaching in the CLI README, command reference, sample config, 0.14 upgrade
  guide, and migration guide topic, including restore-before-downgrade and completion refresh.

## Verification

- The final full local gate passed with 7,717 non-integration tests and 3 tests deselected, Ruff,
  formatting, strict mypy across 667 files, Prettier, markdownlint, cspell, locked-SDD, Rulesync,
  and diff checks.
- Six design safety mutations and the additional exact-schema and interruption-cleanup branch
  mutations all failed focused tests before production behavior was restored.
- Per-phase reviews, a fresh-eyes whole-diff review, focused fix reviews, and the saga-lead final
  implementation review converged with no blocking, important, or minor findings.
- Isolated shipped-CLI validation passed manual backup/restore, automatic stale migration, opt-out,
  real migration-failure recovery, stdout purity, completion immutability, restore-shape refusal,
  canonical stale qualification, genuine historical controls, and version-zero initialization.
- No VM or cloud backend was needed. Every CLI validation used a temporary isolated home, so no
  operator-state snapshot was required; all reported scratch roots were removed and independently
  verified absent.
- Exact head/base freshness was verified against origin, and a synthetic merge with current `main`
  was conflict-free at closeout.

## Permanent homes and known behavior

The load-bearing operator contract lives in `cli/README.md`, `cli/command-reference.md`,
`cli/sample-config.toml`, the 0.14 upgrade guide, and the `concept-migration` guide topic. The
maintainer contract lives in `agentworks.db.backup`, the migration-shape map beside the ladder, and
their focused tests; no current code or permanent documentation depends on this SDD directory.

WAL-aware read-only validation may leave restrictive `-shm` and zero-byte `-wal` coordination files
beside a selected backup. The backup database itself remains byte-identical, valid, and retryable.
Immutable reads were deliberately not used for backup/restore validation because they can ignore
committed WAL content; deleting coordination files would add ownership and race hazards.

## Supersession (2026-08-12)

Bug report 502 found that two pieces of the completion mechanism recorded above under "Non-mutating
completion probes", sidecar gating and immutable current-schema reads, were the wrong design for the
property they were trying to protect. The state database runs in WAL mode, so `-wal`/`-shm`
coordination files are its normal steady state under any live connection, not evidence of
unavailable or damaged state; vetoing on their presence made every database-backed completer return
no candidates whenever any other `agw` process held the database open, and the immutable read that
remained after clearing the veto would have ignored the WAL and served stale, pre-write rows. The
fixing pull request, number 503, replaced both with an ordinary, WAL-aware read-only completion open
(no sidecar veto, no `immutable=1`), bounded by a short connection timeout so a database locked by a
concurrent writer still fails a completion probe quickly instead of freezing it. Every other item
recorded under "What shipped" and "Verification" still stands; this note narrows only the
sidecar-gating and immutable-read clauses of the completion-probe bullet under "What shipped" and
the "completion immutability" item in the shipped-CLI validation list under "Verification".

## Supersession (2026-08-13)

The follow-on to the above, pull request 504, extended this effort's classifier rather than only
using it, so it is recorded here for the same reason. A database held by a concurrent writer was
classified `MALFORMED` and reported to the operator as damaged state, with remediation advice to
restore from a backup, when nothing was wrong beyond another process holding the file. The
classifier gains a `BUSY` member and a matching `BusyStateError`, and every database-open seam that
translates a SQLite error now distinguishes busy from corrupt. The exact-shape validation, the
migration lock protocol, and the backup and restore contracts are untouched: this note narrows only
the classification vocabulary recorded under "What shipped". Issue 505 tracks the database-open
seams that still classify inconsistently.
