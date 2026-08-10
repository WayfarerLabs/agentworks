# Prior art: SQLite backup and migration recovery

- Date: 2026-08-09
- Scope: the SQLite and Python contracts needed by the safer-migrations design

## Executive summary

SQLite's online backup API is the right primitive for both directions of this feature. It copies a
consistent logical database snapshot from one connection into another while allowing a live source
to continue serving other clients. Python exposes that direction directly as
`source.backup(destination)`, so restore is the same operation with the selected backup as source
and the Agentworks database as destination.

The current Agentworks runner does not place a whole migration version inside an explicit
transaction. Under Python's default legacy transaction control, DDL alone does not cause Python to
open one. A failed version can therefore retain earlier DDL while losing later work. The backup is a
real failure-recovery artifact as well as protection from successful-but-wrong migrations and a
version-downgrade path.

## Findings and design consequences

### Online backup produces a consistent live snapshot

The SQLite backup API can copy in incremental page batches with short source read locks and holds
the destination write transaction for the copy. If another connection changes the source during an
incremental copy, SQLite restarts as needed; a completed backup represents one consistent source
snapshot. Python's `Connection.backup` works while other clients, or the same source connection,
access the database. This design uses small finite page batches so the callback can enforce a fixed
deadline and the source read locks stay bounded.

Design consequences:

- Use `source.backup(destination)` for every backup.
- Do not copy the database, WAL, or shared-memory files with filesystem utilities.
- Use fresh source and destination connections and close both deterministically.
- Treat restore as an exclusive operator action. Do not add locking or process-coordination
  machinery to restore; the separate stale-migration race is addressed below.

Sources: [SQLite backup overview](https://sqlite.org/backup.html),
[SQLite backup API](https://sqlite.org/c3ref/backup_finish.html),
[Python `sqlite3.Connection.backup`](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup)

### The Python backup retry loop needs an outer deadline

CPython calls the progress callback after each backup step, including `SQLITE_BUSY` and
`SQLITE_LOCKED`, and otherwise sleeps and retries those statuses without a total deadline. A
callback exception exits the loop and SQLite finishes the backup handle, rolling back an unfinished
destination transaction.

Design consequences:

- Use finite page batches, short connection busy timeouts, and a short sleep interval.
- Check a fixed monotonic deadline in the progress callback and translate expiry to `BackupError`.
- Close both connections and remove a newly created incomplete destination after timeout.
- Test the bound with another connection holding a destination write lock.

Sources:
[CPython `_sqlite` backup implementation](https://github.com/python/cpython/blob/3.14/Modules/_sqlite/connection.c#L2011-L2090),
[SQLite backup finish contract](https://sqlite.org/c3ref/backup_finish.html)

### WAL requires no special copy path

SQLite readers see committed pages through the WAL when those pages have not yet moved back into the
main database file. The backup API reads through SQLite rather than copying one file's bytes, so its
snapshot includes committed WAL content without a manual checkpoint and without separate `-wal` or
`-shm` artifacts.

Design consequences:

- A focused test must commit a row in WAL mode before checkpoint and prove the backup contains it.
- Backup location and retention operate on one completed database file.
- No WAL-specific branch belongs in production backup code.

Sources: [SQLite WAL](https://sqlite.org/wal.html),
[SQLite backup overview](https://sqlite.org/backup.html)

### Completion inspection must not participate in WAL coordination

SQLite can open a WAL database read-only when existing WAL and shared-memory files are readable,
when the containing directory permits creating them, or when the database is opened as immutable.
The `immutable=1` URI parameter tells SQLite the file will not change and avoids locking and change
detection, but it also ignores WAL contents. A schema change committed only in WAL is therefore
invisible to an immutable connection. Immutable mode is safe for completion only when no WAL or
journal sidecar is present; ordinary inspection must remain WAL-aware.

Design consequences:

- Use `mode=ro` for ordinary schema inspection.
- For completion, return no candidates when a `-wal`, `-shm`, or `-journal` sidecar exists;
  otherwise use `mode=ro&immutable=1` for inspection and the read-only query connection.
- Treat concurrent sidecar creation during that short check as outside the single-operator CLI
  contract; immutable mode can produce a stale completion view but cannot mutate state.
- Test that a completion probe leaves the database directory byte-for-byte unchanged.

Sources: [SQLite WAL read-only behavior](https://sqlite.org/wal.html),
[SQLite URI filename parameters](https://sqlite.org/uri.html)

### Restore is the same directional copy in reverse

The online backup API replaces the destination database with the source database's contents. Restore
therefore opens the chosen backup read-only as source, validates it before touching live state,
opens the live database as destination, and invokes the same backup operation. It must not construct
the Agentworks `Database` facade, because that facade migrates during construction.

Design consequences:

- Keep backup and restore below the `Database` facade in one path-level service.
- Validate the selected source as a readable Agentworks SQLite database before opening the live
  destination.
- Let restore create an absent destination, but reject identical source and destination paths.
- End the command after restore. The next ordinary command owns any forward migration.

Sources: [SQLite backup API](https://sqlite.org/c3ref/backup_finish.html),
[Python `sqlite3.Connection.backup`](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup)

### Restore validation must match the claimed Agentworks version

Migration 1 creates `vms(name)` and `workspaces(name)`, and later rebuilds preserve them. They
reject a generic SQLite file that has only `schema_version`, but they cannot distinguish a
current-version lookalike that copies those three common sentinels while omitting current tables
such as `settings` and `sessions`. Restore therefore needs a cumulative table-and-critical-column
sentinel map keyed by the claimed completed version.

Design consequences:

- Restore validation accepts only versions this binary understands and checks that version's
  declarative sentinel set before opening the live destination.
- A test builds every real migration version and proves its corresponding sentinel set, while a
  current-version common-sentinel lookalike proves the map is semantically meaningful.
- A migration-ladder comment makes sentinel-map review part of future table addition, rebuild, and
  removal work.
- This is not hostile-input authentication; a deliberately forged lookalike remains outside scope.

Source: current `agentworks.db.migrations` history and
[SQLite integrity check](https://sqlite.org/pragma.html#pragma_integrity_check)

### Transactional DDL requires a transaction the runner does not start

With Python's legacy transaction control, implicit transactions open for DML statements such as
`INSERT`, `UPDATE`, `DELETE`, and `REPLACE`, not for DDL. SQLite wraps a standalone statement in its
own implicit transaction. Agentworks explicitly commits before the migration run, executes each
statement separately, and commits after recording each completed version. Depending on statement
order, an earlier DDL statement can therefore survive a later failure inside the same version.

Design consequences:

- Never promise automatic rollback of a failed migration.
- Create the safety snapshot before the first migration statement.
- Preserve the existing per-version checkpoint behavior; redesigning migration transactions is a
  separate effort.
- Test the recovery claim with an intentionally partial failing migration, then restore its backup.

Sources:
[Python transaction control](https://docs.python.org/3.14/library/sqlite3.html#transaction-control-via-the-isolation-level-attribute),
[SQLite transactions](https://sqlite.org/lang_transaction.html)

### A separate SQLite write transaction can serialize the existing runner

Locking the state database cannot protect the whole current migration ladder because the runner
commits before the run and after every completed version. SQLite permits only one simultaneous write
transaction per database, so a transaction on a separate dedicated SQLite file can act as a narrow,
portable mutex without changing those migration transactions.

Design consequences:

- Acquire bounded `BEGIN IMMEDIATE` on one persistent adjacent lock database only for stale opens.
- Qualify the first stale observation under the lock. If acquisition initially reports busy, a
  still-stale result after waiting is not a trustworthy baseline and must be refused.
- After interaction, reacquire and compare both application version and SQLite schema cookie; then
  hold the lock across backup and the full existing migration ladder.
- Never unlink the lock file; unlinking can let a waiter and a new caller lock different files.
- Do not add owner records, leases, stale-lock recovery, or platform-specific file-lock branches.

Source:
[SQLite immediate transactions](https://sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions)

## Refuted or not adopted

- **Filesystem copy of the main database file:** invalid for a live WAL database because committed
  pages may live outside that file, and interruption can leave the copy corrupt.
- **Mandatory WAL checkpoint before backup:** unnecessary; it duplicates work below SQLite's logical
  backup interface and can interfere with concurrent clients.
- **`VACUUM INTO`:** capable of producing a live copy, but backup already exposes the direct Python
  API, has the desired source-to-destination semantics, and does not add vacuum work.
- **Whole-run migration transaction redesign:** potentially valuable, but it changes the migration
  contract rather than supplying the requested safety UX and is explicitly outside this effort.
- **Cross-process lock manager:** restore is documented as an exclusive operation. Hostile or
  uncooperative concurrent writers are outside scope.

## Open research questions

None for this effort. The fixed backup deadline and finite page batches are intentionally not
operator-configurable; observed database size or workload would be needed before adding a tuning
surface.

## Source quality

| Source                           | Quality               | Angle used                                    |
| -------------------------------- | --------------------- | --------------------------------------------- |
| SQLite backup documentation      | Primary project docs  | Snapshot semantics, locking, direction        |
| SQLite WAL documentation         | Primary project docs  | WAL visibility and read-only behavior         |
| SQLite URI documentation         | Primary project docs  | Immutable read-only inspection                |
| SQLite transaction documentation | Primary project docs  | Implicit and explicit transaction boundaries  |
| Python `sqlite3` documentation   | Primary language docs | Backup wrapper and legacy transaction control |
| CPython `_sqlite` implementation | Primary language code | Busy retry and progress callback behavior     |
