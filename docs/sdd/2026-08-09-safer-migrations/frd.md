# FRD: Safer Database Migrations

- Status: Approved for implementation
- Date: 2026-08-09
- Saga: `docs/sdd/2026-08-04-next-steps/`
- Delivery: operator-dispatched SDD effort tracked by the saga's pre-0.14 release gate; the effort
  lead owns these artifacts and implementation, the saga lead reviews, and the operator merges.

## Purpose

Agentworks upgrades its SQLite state database automatically when an ordinary command first opens an
older schema. That convenience currently gives the operator no notice and no recovery artifact. The
upgrade to 0.14 changes every existing database, so migration safety becomes ordinary product UX:
announce the migration before it starts, make a WAL-safe backup by default when automation cannot
answer a prompt, and provide direct backup and restore commands.

The primary value is recovery from a migration that completes but produces an unwanted result, and
version rollback when an operator returns from 0.14 to 0.13. The backup also provides a recovery
point if one migration fails after partially changing the database. Agentworks' migration runner
checkpoints each completed version and does not wrap a whole multi-statement version in an explicit
transaction, so the product must not promise that every failure rolls itself back.

## Requirements

- R1. Before an ordinary CLI command migrates an existing database whose schema is below the current
  version, it prints the current and target schema versions and says that migration is about to
  begin. A new database is initialized without a migration notice or backup offer. A database newer
  than the running Agentworks version is refused before backup or migration so an older release
  cannot operate against a schema it does not understand.
- R2. In an interactive terminal, the notice asks whether to create a backup before continuing and
  defaults to yes. Declining skips the backup and continues the migration. In a non-interactive
  invocation (an explicit `--non-interactive` or stdin without a terminal), Agentworks creates the
  backup automatically unless the operator has disabled that behavior in config. It never tries to
  prompt on a non-interactive invocation.
- R3. The non-interactive default is controlled by one boolean database setting whose default is
  true. The setting changes only automatic pre-migration backup behavior; it does not suppress the
  interactive offer or affect explicit backup and restore commands.
- R4. `agw database backup` creates an on-demand backup of the current state database and prints the
  resulting path. It snapshots the database at its present schema without invoking automatic
  migration, including when that schema is newer than the running release. An absent database is a
  clean not-found error and a malformed database is a clean state error; neither case creates an
  empty source database or a backup. On-demand backups are never deleted by automatic retention.
- R5. `agw database restore BACKUP_PATH` restores the selected backup into the state database. It
  shows both paths and asks for confirmation by default; `--yes` accepts the replacement for
  automation. A non-interactive invocation without `--yes` refuses cleanly. Refusal or an invalid
  backup leaves both paths untouched. Validation requires a structurally sound SQLite database whose
  complete non-SQLite table and column shape exactly matches the claimed Agentworks version, not
  merely caller-supplied common sentinels. A backup newer than the running release is preserved but
  refused for restore until a release that understands its schema performs the operation. Restore
  does not open the restored database through the automatic migration path during the same command.
- R6. Backups use SQLite's online backup API rather than filesystem copying. A completed backup is a
  consistent SQLite snapshot even when the source uses WAL or another process has it open. Backup
  and restore use one fixed bounded busy deadline; exhausting it returns a clean retryable error
  rather than waiting indefinitely.
- R7. Database backups live in a dedicated directory beside the state database and use timestamped,
  collision-resistant names that distinguish automatic pre-migration backups from on-demand backups.
  After a successful automatic backup, Agentworks retains the five newest automatic backups and
  removes older automatic backups. It does not prune on-demand backups or files it does not
  recognize as its own automatic backups. Newly created backup files and an absent live destination
  restored on POSIX are user-only; native Windows relies on the existing user-profile directory ACL.
- R8. When migration fails after a backup was created, the clean CLI error includes the exact
  `agw database restore <path>` invocation for that backup. If the operator declined or disabled
  backup, the error says no pre-migration backup was created and does not invent a restore path.
- R9. `agw doctor` remains non-migrating. Its pending-schema result teaches the same model as the
  pre-migration notice: the next ordinary state-opening command will announce the migration and
  offer or automatically create a backup first.
- R10. Operator teaching lands with the behavior: `sample-config.toml`, the CLI reference and state
  documentation, `docs/guides/upgrading-to-0.14.md`, and `agw guide concept-migration` describe the
  exact command names, configuration default, backup location and retention, restore flow, and
  downgrade use. The generated completion tree includes the new command group and arguments. The
  0.14 upgrade guide requires rerunning `agw completion install` so scripts installed by an older
  release gain the hidden non-migrating probe marker.
- R11. Shell-completion probes are non-prompting and non-mutating. If dynamic completion encounters
  a stale database, it returns no database-derived candidates and leaves migration to an
  operator-invoked command. The hidden child invocation may fail cleanly with empty stdout;
  generated shell code discards its diagnostic stderr and treats that as no candidates, without
  spreading an optional database through ordinary callers. Configuration warnings or errors before
  the database gate are equally hidden from the interactive shell. A stale script installed by an
  older release also cannot prompt or migrate: an exact known database-backed names-only command,
  interactive stdin, and redirected non-terminal stderr are classified before command execution as a
  legacy completion probe and refused. Generating or installing completion scripts does not open
  state.
- R12. Migration notices, backup status, prompts, and failure remediation use the presentation and
  error channels without writing to command stdout. JSON commands still emit exactly one JSON
  document, and names-only commands still emit only candidate names, after migration completes. The
  pre-migration notice is mandatory on stderr even while ordinary presentation is suppressed.
- R13. If a selected automatic or interactive pre-migration backup fails, Agentworks stops before
  the first migration statement and reports that migration did not start. It never silently falls
  back to migrating without the selected safety artifact. The clean error tells an interactive
  operator that a retry may explicitly decline the offer and tells automation that the documented
  config opt-out is the deliberate escape hatch.
- R14. Concurrent commands that observe the same stale database serialize at one narrow migration
  boundary. An initial stale observation is first qualified under that lock; if its version or
  schema cookie changed but remains stale before qualification, the service refuses it. The service
  rechecks again after interaction when it reacquires the lock. Exactly one command backs up and
  migrates that version transition; a waiter that finds current state continues without a second
  backup or stale failure remediation. A caller whose first stale inspection overlapped an in-flight
  migration, or whose recorded state changed but remains non-current, refuses without another backup
  or migration so it cannot attach misleading recovery guidance to another process's partial result.

## Acceptance

- AC1. Given an existing below-latest database in an interactive terminal, an ordinary command
  announces the version change before any migration statement, defaults the backup offer to yes, and
  either creates a usable snapshot or honors a decline before migrating.
- AC2. Given the same database without an interactive terminal, the default configuration creates a
  usable snapshot before migration with no prompt. Setting the documented boolean to false skips
  that automatic backup and still migrates.
- AC3. A migration-failure test that leaves a version partially applied proves that restoring the
  reported snapshot returns the original schema version and representative rows exactly.
- AC4. An on-demand backup taken from a WAL-mode database remains readable and contains committed
  rows, including rows not yet written into the main database file.
- AC5. Six automatic backups cause the oldest automatic backup to be removed and the newest five to
  remain. On-demand backups and unrelated files in the same directory remain untouched. New backup
  files and a newly restored live database have restrictive POSIX modes.
- AC6. Restore refusal changes neither source nor destination. Confirmed restore replaces the state
  database with the selected snapshot; the next ordinary command applies any required forward
  migrations through the normal notice and backup flow. A structurally valid current-version
  lookalike containing only common Agentworks sentinels is rejected before the destination opens. A
  version-1 database containing a committed version-2 column is likewise rejected as partially
  migrated before the destination opens.
- AC7. Doctor does not create, migrate, back up, or restore state, and its human and JSON schema
  facts remain accurate for absent, current, stale, malformed, and newer databases.
- AC8. Focused tests, the complete repository gates, and an isolated-home drive of the shipped CLI
  demonstrate the interactive, non-interactive, opt-out, backup, restore, retention, failure-hint,
  doctor, docs, and completion contracts without touching the operator's real state.
- AC9. Invoking each dynamic completion path against stale state emits no candidates, prompts, or
  notices and leaves the database and backup directory byte-for-byte unchanged. An explicit JSON or
  names-only command that migrates includes the required versioned migration notice on stderr and
  preserves its stdout contract. A pre-0.14 installed completion invocation with redirected stderr
  also returns no candidates without prompting, backing up, or migrating.
- AC10. An ordinary writable open of a newer schema fails before any database or backup-directory
  change. On-demand backup can still preserve that newer database, and restore can replace it with a
  selected older snapshot.
- AC11. A forced online-backup or destination failure after the operator or config selected backup
  leaves the schema and representative data unchanged, creates no completed backup, and explains how
  to retry with an explicit decline or opt-out rather than continuing automatically.
- AC12. Two processes opening the same stale database converge on one successful backup and
  migration; the waiter rechecks under serialization, exits successfully, and does not create a
  second automatic backup. A staggered case starts the waiter after the first process's initial DDL
  and makes it refuse without a backup after the first process fails. Another staggered case records
  stale state, lets a competing process change stale state and release the lock, then proves the
  caller refuses on its first acquisition after the competing process released the lock. Removing
  initial lock qualification, only the preliminary-to-qualified token comparison, or the later
  locked recheck makes these tests fail.
- AC13. A held SQLite lock makes backup or restore return the documented retryable error within the
  fixed deadline and leaves no completed partial backup or newly created live destination.

## Constraints and non-goals

- The feature covers only the Agentworks SQLite state database. VM backups, config and resource
  manifests, workspace files, secrets, and remote state are separate concerns.
- Do not add hostile-filesystem defenses, ownership or provenance machinery, backup encryption,
  remote targets, atomicity proofs, a general snapshot framework, or a general migration
  orchestration subsystem. One dedicated SQLite lock file is the complete concurrency mechanism.
- Do not change the migration ladder or make all historical migrations transactional as part of this
  effort. The backup is a safety net around the migration behavior that exists.
- Do not reuse the configurable VM-backup path for database backups. Database backups follow the
  database so an operator can find the recovery artifact beside the state it protects.
- Do not add a retention setting until an operator need demonstrates that five automatic snapshots
  is the wrong fixed policy.
- Restore does not automatically snapshot the database it is about to replace. The operator already
  selected a recovery artifact and confirms the replacement; adding recursively layered safety
  copies would blur automatic-retention semantics. An operator who wants that extra artifact can run
  `agw database backup` before restore.
- The command group is `database`, not `state`: it acts on one SQLite database and must not imply it
  captures every form of Agentworks state.

## Settled decisions

- The operator approved the corrected partial-failure framing, the `agw database backup` and
  `agw database restore` command home, fixed retention of five automatic backups, preservation of
  on-demand backups, and confirmation before restore on 2026-08-09.
- The effort lead settled the saga lead's restore question on 2026-08-09: confirmed restore does not
  create an automatic pre-restore backup; the explicit on-demand command already covers that choice
  without adding a second retention category.

## Open questions

None. Any implementation discovery that requires broadening these requirements or non-goals is an
operator escalation, not an implicit design change.
