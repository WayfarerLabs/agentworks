# HLA: Safer Database Migrations

- Status: Draft for phased review
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
  inspect + optional snapshot + safe open
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

## Components and boundaries

### Path-level backup service

A focused module under `agentworks.db` owns SQLite file-to-file backup and restore. It accepts
explicit `Path` values so tests and commands can isolate state without mutating module globals. Its
public operations are shaped around the two real activities rather than a configurable engine:

- create an on-demand snapshot;
- create a pre-migration snapshot and apply automatic-only retention;
- restore one validated snapshot into the live database.

Both snapshot operations share one private online-copy helper. Source connections open by SQLite URI
in read-only mode; destinations use fresh writable connections. The copy is
`source.backup(destination)` and both connections close on every outcome. The service translates
SQLite and filesystem failures into existing kind-based domain errors without rendering output or
importing CLI code.

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
selected source read-only: the database opens, `PRAGMA quick_check` reports `ok`, `schema_version`
is a table, and its maximum version is a non-negative integer. This is a narrow
fail-before-destruction check, not provenance or hostile-filesystem verification. Restore then
copies the selected source into a fresh raw connection to the live path and closes without
constructing `Database` or creating a pre-restore backup. Operators who want a snapshot of the live
destination use the on-demand backup command before restore.

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
SQLite URI `mode=ro`, returning absent, stale, current, future, or malformed state without
migration. It remains WAL-aware; like any SQLite read, it may participate in existing WAL sidecar
coordination.

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

Safe writable open rechecks schema state, refuses future or malformed state, creates the selected
pre-migration snapshot, and only then constructs `Database`. If construction fails during migration,
the failed connection closes and the service raises the existing kind-based `StateError` with a
recovery hint derived from the snapshot path or from the fact that no snapshot was selected. This
keeps reusable safety ordering, failure association, and remediation below the CLI without adding an
activity-specific error class or moving schema execution out of `Database`.

### Interaction policy at the CLI choke point

Every production writable open already reaches `cli._helpers.get_db()`. That helper becomes the
interaction boundary:

1. Ask the database safety service to inspect schema without migration.
2. For an absent database, request a safe writable open with no notice or backup.
3. For a current database, request a safe writable open with no backup.
4. For a future database, raise `StateError` before a writable open, with an on-demand backup and
   older-version recovery hint.
5. For a stale database, render the current and target versions on stderr.
6. Interactive: inside `output.suppress_presentation()`, ask
   `Back up the state database before migrating?`, default yes. This routes the prompt through the
   existing stderr presentation path even when the command selected JSON or names-only output.
7. Non-interactive: load the focused database setting and back up when true.
8. Pass that Boolean to the service's safe writable open. If a selected backup raises the existing
   `BackupError`, catch it at this interaction boundary and preserve it with a mode-specific hint:
   an interactive retry may explicitly decline, while automation may deliberately set the documented
   config opt-out. The service has not constructed `Database`, so migration cannot have started.
9. Let the service's clean `StateError` propagate. When a snapshot exists, its hint includes the
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
malformed, and future state return no database without notices or prompts. Completion scripts treat
that outcome as no candidates. Explicit user `--names-only` and JSON commands do not carry the
hidden option and use the ordinary migration policy.

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
- the 0.14 upgrade guide explains the pre-migration snapshot and the restore-before-downgrade order;
- `concept-migration` distinguishes resource declaration rewrites from SQLite schema migration and
  teaches the exact recovery command;
- completion tests pin the new group, restore file completion, and hidden non-mutating probes.

## Error and interruption behavior

| Point                                   | Outcome                                                                |
| --------------------------------------- | ---------------------------------------------------------------------- |
| Focused config cannot be interpreted    | Fail before backup or migration                                        |
| Backup source or destination fails      | Remove incomplete output; mode-specific retry hint; do not migrate     |
| Retention cleanup fails                 | Service returns cleanup fact; CLI warns and continues to migration     |
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

1. **Service:** immutable inspection without sidecar changes; WAL-visible backup; manual and
   automatic naming; collision handling; mixed-version automatic-only retention; source validation;
   restore direction; identical-path refusal; safe-open ordering; migration-failure association; and
   failure cleanup.
2. **Policy and CLI:** fresh/current/stale/future/malformed matrices; interactive accept and
   decline; non-interactive default and opt-out; backup-before-first-migration ordering; backup
   failure prevention; partial-migration failure and exact remediation; confirmation and `--yes`;
   separate interactive JSON and names-only stdout-purity captures; completion probes;
   platform-specific recovery-command quoting; and file completion across all shells.
3. **Permanent surfaces:** strict config parsing, sample completeness, doctor facts, CLI reference,
   upgrade guide, guide topic, and operator path rendering.

Mutation checks neuter the two safety pivots: make migration run before backup and make completion
use a writable open. Each mutation must fail a focused test. An isolated-home real-CLI drive creates
an old-schema fixture, exercises non-interactive automatic backup, restores it, exercises opt-out,
and verifies JSON/names-only stdout without touching operator state. No live VM is needed because
the entire feature boundary is local SQLite and CLI behavior.

## Complexity guard

The implementation stops and returns to design review if it appears to require any of the following:

- a general snapshot or storage-provider abstraction;
- cross-process locks or hostile-filesystem guarantees;
- a second migration runner or transaction rewrite;
- backup manifests, provenance records, checksums, encryption, or remote transport;
- command aliases or configurable backup naming, location, format, or retention.

The HLA is sufficient low-level direction for this bounded feature. A separate LLD is omitted unless
implementation exposes a component boundary not represented here.
