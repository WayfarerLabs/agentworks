# HLA: Safer Database Migrations

- Status: Approved for implementation
- FRD: [frd.md](./frd.md)
- Prior art: [prior-art-research.md](./prior-art-research.md)
- Saga: `docs/sdd/2026-08-04-next-steps/`

## Architectural summary

The feature adds one small database-safety service that owns schema inspection, backup sequencing,
safe writable opening, and migration-failure association. The existing CLI database-open choke point
owns only interaction and focused config choice. The low-level `Database` facade remains responsible
for schema execution, not prompting, config, or backup. Direct backup and restore commands bypass
that facade so they cannot migrate as a side effect.

```text
ordinary CLI command
        |
        v
      get_db ------------------------ completion probe
        |                                   |
        v                                   v
interaction policy                 sidecar absence gate
  notice + prompt/default            + immutable open: candidates
        |                            + sidecars/other state: none
        v
database safety service
  qualify + lock/recheck + optional snapshot + safe open
        |
        +--> timestamped SQLite snapshot
        |
        v
 writable Database --> existing migration ladder
        |
        +--> failure --> StateError with exact recovery hint

database backup/restore commands --> database backup service
                                     (never writable Database)
```

No general snapshot interface, migration coordinator, lock manager, or storage provider is added.
One dedicated SQLite lock file serializes only the already-identified migration choke point.

## Components and boundaries

### Path-level backup service

A focused module under `agentworks.db` owns SQLite file-to-file backup and restore. It accepts
explicit `Path` values so tests and commands can isolate state without mutating module globals. Its
public operations are shaped around the two real activities rather than a configurable engine:

- create an on-demand snapshot;
- create a pre-migration snapshot and apply automatic-only retention;
- restore one validated snapshot into the live database.

Both snapshot operations share one private online-copy helper. Source connections open by SQLite URI
in read-only mode; destinations use fresh writable connections. The helper calls
`source.backup(destination, pages=256, sleep=0.05, progress=...)` with short connection busy
timeouts. Its progress callback checks one fixed five-second monotonic deadline, including
`SQLITE_BUSY` and `SQLITE_LOCKED` callbacks, and raises `BackupError` when exhausted. This bounds
automatic backup and restore without a new setting. Both connections close on every outcome, and an
incomplete newly created destination is removed best-effort. The service translates SQLite and
filesystem failures into existing domain errors without rendering output or importing CLI code.

A new backup directory is requested with mode `0700`. Each new backup file is atomically reserved
with create-exclusive mode `0600` before SQLite opens it. Restoring to an absent live destination
uses the same reservation; restoring over an existing destination preserves its mode. POSIX tests
pin these invariants. Native Windows relies on the ACL of the existing user-profile config directory
because POSIX mode bits are not portable. The service never changes permissions on a pre-existing
operator directory or database.

Backups live in `<database-parent>/database-backups/`. Names have two disjoint prefixes:

```text
agentworks-pre-migration-<UTC timestamp with microseconds>-v<CURRENT>.db
agentworks-manual-<UTC timestamp with microseconds>.db
```

If a generated path already exists, a numeric suffix selects a fresh path. Successful automatic
backup prunes only names matching the pre-migration prefix, by parsed timestamp oldest first, until
five remain. The timestamp-first form keeps ordering independent of schema version; a mixed-version
test pins that behavior. Manual names and unrelated files are never considered. A retention cleanup
failure is returned as a non-fatal cleanup fact so the CLI can warn without discarding a completed
recovery artifact or allowing cleanup complexity to mask backup success.

Before restore opens the live destination, it rejects an identical source path and validates the
selected source read-only: `PRAGMA quick_check` reports `ok`; `schema_version` is a table whose
maximum is an integer from 1 through `LATEST_VERSION`; and a declarative version-shape map exactly
matches every non-`sqlite_%` table and every column in those tables to the claimed completed
version. The map lives beside the migration ladder and is cumulative by introduction/removal
milestone. A history test builds every real completed version and proves its corresponding exact
shape. Focused tests reject both a common-sentinel lookalike and a version-1 database with a
committed version-2 column, because the latter is a partial migration that would fail on the next
ordinary open. A comment on the migration ladder requires future table additions, rebuilds, and
removals to update the map. This is a narrow semantic format check, not provenance, foreign-key
validation, or hostile-filesystem verification. A future-schema backup remains preservable by
`database backup` but must be restored with a release that understands that version.

After validation, restore copies the selected source into a fresh raw connection to the live path
and closes without constructing `Database` or creating a pre-restore backup. Operators who want a
snapshot of the live destination use the on-demand backup command before restore.

### Focused database setting

Configuration adds exactly one strict setting:

```toml
[database]
auto_backup_before_migration = true
```

`DatabaseConfig` is a frozen settings record. Its loader accepts only a TOML boolean and rejects
unknown keys or a non-table `[database]`. Full `load_config()` includes it like every other settings
section.

The migration decision cannot depend on full config validity because list and completion commands
may open state before loading operator, plugin, or resource settings. A focused
`load_database_config()` reads the same config path and validates only the `[database]` projection.
An absent config file returns the safe default. Unreadable or malformed TOML, or an invalid
`[database]` value, fails before migration because Agentworks cannot safely honor a possible
opt-out. Other unrelated settings do not gate the database safety decision. The focused read happens
only for a stale, non-interactive, operator-invoked open.

### Database safety service

The same focused module exposes operations above the raw copy primitives for WAL-aware schema
inspection, side-effect-free completion opening, and safe writable opening. Ordinary inspection uses
SQLite URI `mode=ro`, returning absent, stale, current, future, or malformed state plus SQLite's
schema cookie without migration. It remains WAL-aware; like any SQLite read, it may participate in
existing WAL sidecar coordination.

Completion opening has a stricter contract. It first checks for adjacent `-wal`, `-shm`, or
`-journal` files and returns no database when any exists. Otherwise it inspects and opens the
current database with `mode=ro&immutable=1`, via a narrowly extended read-only `Database`
constructor. Immutable SQLite opens ignore WAL content, so they are never used after a sidecar is
observed and never used for ordinary policy or doctor checks. This conservative gate may temporarily
suppress candidates after an interrupted or concurrent database session; it never creates or changes
a sidecar merely to complete a shell token. Concurrent creation between the sidecar check and
immutable open is outside the single-operator CLI boundary and at worst produces a stale completion
view. Tests compare the containing directory byte-for-byte before and after completion probes and
pin the no-candidates behavior for WAL and journal sidecars.

For a stale database, the safety service uses one persistent dedicated SQLite file beside the state
database as a portable cross-process mutex. It reserves that file with mode `0600`, opens a separate
connection, and acquires `BEGIN IMMEDIATE` with one fixed 30-second busy timeout. Failure to acquire
returns a clean retryable `StateError`. The file is never unlinked, which avoids a waiter and a new
caller locking different files; no table, owner record, lease, or stale-lock cleanup exists.

A preliminary WAL-aware read can identify absent, current, future, or malformed state without the
lock. When it identifies stale state, the service qualifies that first observation under the lock
before the CLI announces or prompts. It first tries non-blocking acquisition. If the lock was
already held, it waits for the bounded acquisition and rechecks: current state returns as current,
while still-stale state is refused because the observation overlapped another migration attempt. If
the lock was free, it rechecks under lock and compares the version plus schema cookie with the
preliminary observation. Current state converges normally; changed-but-still-stale state refuses;
only unchanged stale state becomes the interaction baseline. The service then releases the lock
while the operator answers. This closes the late-inspector window where a caller could otherwise
record another process's partial DDL as a legitimate stale baseline, including when that process
releases the lock before the first caller acquires it.

After interaction, safe writable open reacquires the lock and rechecks the state database against
that qualified Agentworks version and schema cookie. If another process completed migration, the
waiter skips backup and opens current state. If both tokens are unchanged and state remains stale,
the service creates the selected snapshot and constructs `Database` while retaining the separate
lock through the entire migration ladder. If state changed but remains non-current, it refuses
without a new backup or migration rather than attaching recovery guidance to another process's
partial result. Future and malformed state are also refused. If construction fails during migration,
the failed connection closes and the service raises the existing kind-based `StateError` with a
recovery hint derived from the snapshot path or from the fact that no snapshot was selected. The
lock transaction rolls back and closes in `finally`.

Absent databases are excluded because they contain no prior operator state to protect or back up;
their existing initialization path has no interaction gap. Current databases do not migrate. This
keeps serialization limited to the demonstrated stale-transition race, without changing migration
transactions or adding an activity-specific error class.

### Interaction policy at the CLI choke point

Every production writable open already reaches `cli._helpers.get_db()`. That helper becomes the
interaction boundary:

1. Ask the database safety service to inspect schema without migration and qualify any stale
   observation under the migration lock.
2. For an absent database, request a safe writable open with no notice or backup.
3. For a current database, request a safe writable open with no backup.
4. For a future database, raise `StateError` before a writable open, with an on-demand backup and
   older-version recovery hint.
5. Before stale-state interaction, honor the root callback's legacy completion flag described below.
   Raise the ordinary completion `StateError` before notice, prompt, backup, or migration.
6. For a stale database, render the current and target versions through a new semantic
   `output.notice()` role. It always targets stderr and deliberately survives
   `suppress_presentation()`, unlike ordinary optional presentation; automatic-backup completion
   uses the same role. Tests prohibit using raw `typer.echo` at this boundary.
7. Interactive means both stdin and the prompt's stderr stream are terminals and `--non-interactive`
   is absent. Inside `output.suppress_presentation()`, ask
   `Back up the state database before migrating?`, default yes. This routes the prompt through the
   existing stderr presentation path even when the command selected JSON or names-only output.
8. Otherwise, load the focused database setting and back up when true without prompting.
9. Pass that Boolean to the service's serialized safe writable open. If a selected backup raises the
   existing `BackupError`, catch it at this interaction boundary and preserve it with a
   mode-specific hint: an interactive retry may explicitly decline, while automation may
   deliberately set the documented config opt-out. The service has not constructed `Database`, so
   migration cannot have started.
10. Let the service's clean `StateError` propagate. When a snapshot exists, its hint includes the
    platform-specific exact restore invocation; otherwise it states that no pre-migration backup was
    created.

Migration notices, backup status, prompts, and remediation go to stderr. The command's stdout stays
owned by its normal renderer, including JSON envelopes and names-only rows.

`Database.__init__` gains two correctness guards needed by the boundary: it rejects a schema newer
than `LATEST_VERSION`, and it closes its connection when migration raises. It does not learn about
backup, interaction, or config. `Database.check_schema()` delegates to the ordinary WAL-aware
inspection operation so doctor and other callers share the non-migrating implementation.

### Completion probe mode

Generated dynamic completion commands add one hidden root option before their ordinary read command:

```text
agw --completion-probe vm list --names-only
```

The global callback records that mode in the CLI layer. In completion mode, `get_db()` uses the
safety service's sidecar gate and immutable current-schema open. Existing sidecars, absent, stale,
malformed, and future state raise the existing `StateError` before an ordinary caller can
dereference a database. The hidden child process exits nonzero with empty stdout and a clean
internal stderr diagnostic; every generated shell snippet already discards that stderr and consumes
only stdout, so the interactive shell receives an empty candidate set without spreading
`Database | None` through command code. Configuration warnings or errors that occur before
`get_db()` follow the same shell-level contract: stderr is discarded, stdout stays empty, and no
prompt is allowed. Explicit user `--names-only` and JSON commands do not carry the hidden option and
use the ordinary migration policy.

Completion scripts are installed copies, so a script generated by 0.13 does not gain the marker when
the binary upgrades. Before subcommand code runs, the root callback classifies a legacy probe only
when all of these hold: no explicit marker; argv matches one of the exact known database-backed
names-only source commands (`vm`, `workspace`, `session`, `agent`, `console`, or `resource` `list`,
including its existing filter options); stdin is a terminal; and stderr is not. The small
command-path set is shared with completion metadata, and a parity test extracts every old generated
bash, zsh, and PowerShell database invocation and proves classification, so a new completer cannot
silently miss the fallback. The callback records the same completion-probe flag before config or
`get_db()` work; `get_db()` then refuses stale state before prompt or migration.

This conservative fallback may also suppress migration for an explicit interactive names-only list
whose stderr the operator redirected, which is safe and retryable without that redirection. It does
not affect non-interactive automation because stdin is not a terminal, or an explicit interactive
names-only command whose stderr remains a terminal. The 0.14 upgrade guide nevertheless requires
rerunning `agw completion install`; the marker is the durable contract, and the fallback only
prevents an old installed script from hanging or migrating during the upgrade window.

Adding the hidden marker to the shared dynamic snippets covers every current database-derived
completion without teaching individual list commands about completion. Completion generation and
installation still inspect only the Typer command tree and never open state.

The restore positional uses a `files` dynamic-completion identifier whose bash, zsh, and PowerShell
implementations delegate to each shell's native filesystem completion. No database-specific path
scanner is added.

### Direct CLI commands

A singular top-level `database` Typer group sits beside the other persisted-state nouns:

- `agw database backup` calls the on-demand snapshot service and prints its path through
  `format_host_path`.
- `agw database restore BACKUP_PATH [--yes/-y]` validates the source, shows source and destination,
  and prompts before copying. Without `--yes`, a non-interactive invocation refuses before opening
  the destination.

Neither command calls `get_db()` or constructs `Database`. The restore path receives ordinary file
completion. There are no aliases, location flags, retention flags, or backup-format options.

The migration-failure hint and backup success output share path rendering. A small platform-aware
renderer produces the exact recovery command: POSIX quoting on Linux, macOS, and WSL, and native
single-quote escaping for PowerShell on Windows. Home-relative paths remain executable rather than
printing a quoted, non-expanding tilde: `$HOME/...` on POSIX and `(Join-Path $HOME 'relative/path')`
in PowerShell. Tests pin spaces, apostrophes, and shell-special characters for both platform
families.

## Doctor and teaching

Doctor continues to call `Database.check_schema()` and opens only `Database(read_only=True)` for a
current schema. Its stale-schema text changes from immediate automatic migration to the truthful
notice-and-backup flow. Doctor does not inspect the backup directory or validate recovery files.

Permanent teaching changes in the same implementation phase that makes each claim true:

- `sample-config.toml` and config reference material document the one default-true setting;
- the CLI README and command reference document backup, restore, location, retention, confirmation,
  and non-migrating command behavior;
- the 0.14 upgrade guide explains the pre-migration snapshot, restore-before-downgrade order, and
  required `agw completion install` refresh;
- `concept-migration` distinguishes resource declaration rewrites from SQLite schema migration and
  teaches the exact recovery command;
- completion tests pin the new group, restore file completion, and hidden non-mutating probes.

## Error and interruption behavior

| Point                                   | Outcome                                                                |
| --------------------------------------- | ---------------------------------------------------------------------- |
| Focused config cannot be interpreted    | Fail before backup or migration                                        |
| Backup source or destination fails      | Remove incomplete output; mode-specific retry hint; do not migrate     |
| Backup busy deadline expires            | Close connections; remove incomplete new output; clean retry error     |
| Retention cleanup fails                 | Service returns cleanup fact; CLI warns and continues to migration     |
| Migration lock is busy                  | Stop with a clean retry error; do not back up or migrate               |
| Operator declines interactive backup    | Continue without a snapshot; say so if migration later fails           |
| Non-interactive setting disables backup | Continue without a snapshot; say so if migration later fails           |
| Migration fails after backup            | Close connection; preserve snapshot; print exact restore command       |
| Restore source validation fails         | Do not open or change live destination                                 |
| Operator declines restore               | Do not open or change live destination                                 |
| Restore copy fails                      | Surface a clean failure; SQLite owns destination transaction atomicity |
| Completion sees non-current state       | Return no candidates, prompt nowhere, change nothing                   |

This effort does not claim recovery from process or host failure during restore. The operator keeps
the selected source backup, so a failed restore remains retryable after the destination is no longer
in use.

## Testing strategy

Focused tests establish the contracts at three layers:

1. **Service:** immutable inspection without sidecar changes; WAL-visible backup; bounded busy
   timeout; restrictive creation; manual and automatic naming; collision handling; mixed-version
   automatic-only retention; exact version-appropriate historical-schema validation; current-version
   common-sentinel lookalike and partial-next-version rejection; future-version refusal before
   destination open; restore direction; identical-path refusal; first-observation lock
   qualification; serialized safe-open recheck; staggered partial-migration refusal while the lock
   is held and after a changed-stale actor releases it before first acquisition; migration-failure
   association; and failure cleanup.
2. **Policy and CLI:** fresh/current/stale/future/malformed matrices; interactive accept and
   decline; non-interactive default and opt-out; backup-before-first-migration ordering; backup
   failure prevention; partial-migration failure and exact remediation; confirmation and `--yes`;
   separate interactive JSON and names-only stdout-purity captures that require the versioned notice
   on stderr; raw and shell-wrapped completion probes under stale, warning, invalid-config, and
   pre-0.14 marker-free states; platform-specific recovery-command quoting; and file completion
   across all shells.
3. **Permanent surfaces:** strict config parsing, sample completeness, doctor facts, CLI reference,
   upgrade guide, guide topic, operator path rendering, and a construction-site inventory that
   permits production writable `Database` construction only inside the database safety service.

Mutation checks neuter five safety pivots: make migration run before backup, bypass initial stale
qualification, remove only the preliminary-to-qualified version/cookie comparison, remove the
post-interaction schema recheck, and make completion use a writable open. Each mutation must fail a
focused test. An isolated-home real-CLI drive creates an old-schema fixture, exercises
non-interactive automatic backup, restores it, exercises opt-out, and verifies JSON/names-only
stdout without touching operator state. No live VM is needed because the entire feature boundary is
local SQLite and CLI behavior.

## Complexity guard

The implementation stops and returns to design review if it appears to require any of the following:

- a general snapshot or storage-provider abstraction;
- a general lock manager, leases, ownership records, or hostile-filesystem guarantees beyond the one
  fixed SQLite migration lock described above;
- a second migration runner or transaction rewrite;
- backup manifests, provenance records, checksums, encryption, or remote transport;
- command aliases or configurable backup naming, location, format, or retention.

The HLA is sufficient low-level direction for this bounded feature. A separate LLD is omitted unless
implementation exposes a component boundary not represented here.
