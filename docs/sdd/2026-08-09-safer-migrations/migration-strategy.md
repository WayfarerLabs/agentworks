# Migration Strategy: Safer Database Migrations

- Status: Draft for phased review
- Date: 2026-08-09
- Target release: 0.14.0
- Builds on: [frd.md](./frd.md), [hla.md](./hla.md)

## 1. Current state

Agentworks stores operator state in one SQLite database. Every production writable CLI open funnels
through `cli._helpers.get_db()`, while `Database` currently opens the file, enables WAL, and runs
all pending schema migrations during construction. The migration runner commits each completed
version, but a failure within a version can leave earlier statements applied.

There is no migration notice, recovery snapshot, direct database backup command, direct restore
command, or database-specific configuration. `Database.check_schema()` and doctor inspect schema
without intentionally migrating, though the current SQLite connection is writable. Dynamic shell
completion invokes ordinary list commands and therefore currently reaches the same database-open
path as an operator command.

## 2. Target state

The transition is additive except for one safety hardening added in 0.14: 0.14 and later binaries
refuse to open a database whose schema is newer than they understand. That guard cannot retrofit an
already-released 0.13 binary, so downgrade remains restore-first.

| Surface                | Before                    | After                                                             |
| ---------------------- | ------------------------- | ----------------------------------------------------------------- |
| Stale writable open    | Migrates silently         | Announces versions; offers or defaults to backup; then migrates   |
| Current or absent open | Opens or initializes      | Same behavior; no automatic backup                                |
| Future schema          | Writable facade opens     | Refused before writable access                                    |
| Manual recovery        | No first-party commands   | `agw database backup` and `agw database restore PATH`             |
| Automation             | No migration choice       | Automatic pre-migration backup unless config strictly disables it |
| Completion             | Ordinary database path    | Hidden, non-prompting, side-effect-free probe                     |
| Doctor                 | Reports without migration | Same posture, with wording aligned to the new flow                |

The only new configuration is:

```toml
[database]
auto_backup_before_migration = true
```

An absent setting preserves the safe default. No legacy spelling, warning window, config rewrite, or
data rewrite is needed.

## 3. Delivery order

The reviewed SDD artifacts land before implementation under the active saga. Implementation then
lands in one focused PR with independently green commits:

1. Add the raw online-copy service, manual backup and restore commands, restore file completion, and
   their permanent command documentation.
2. Add WAL-aware schema inspection, safe writable-open sequencing, the focused config projection,
   migration interaction, future-schema refusal, and non-mutating completion probes. Update doctor,
   sample config, upgrade teaching, and migration guide in the same commit series.
3. Run the full repository gate, mutation checks, and an isolated-home real-CLI drive; resolve all
   project-specific, fresh-eyes, and integration-test findings before closeout.

No intermediate commit redirects operator state, changes the migration ladder, or relies on an
unused compatibility shim.

## 4. Upgrade behavior

### Current or new databases

A current schema opens unchanged. An absent database initializes through the existing migration
ladder without creating an empty pre-migration backup or showing a migration notice.

### Existing pre-0.14 database

On the first ordinary 0.14 command that needs writable state:

1. Agentworks reads the current and target schema versions without migration.
2. It announces the pending transition on stderr.
3. An interactive operator accepts or declines a default-yes backup prompt. A non-interactive caller
   uses the default-true focused setting.
4. When selected, Agentworks completes a timestamped SQLite online backup before the first migration
   statement and retains only the five newest automatic backups.
5. Agentworks runs the existing migration ladder. The original command continues after success.
6. On migration failure, the error preserves the completed backup and prints an exact executable
   restore command. If backup was declined or disabled, the error says no pre-migration backup was
   created.

The operation is safe to retry only according to the existing per-version migration contract. A
failed version may be partially applied; the printed restore command is the deterministic recovery
path when a pre-migration backup exists.

### Automation opt-out

Automation that deliberately accepts migration without a recovery artifact sets the Boolean to
`false` before opening stale state. Invalid or unreadable database config stops before backup or
migration because Agentworks cannot safely infer whether the opt-out applies.

## 5. Downgrade and restore

A 0.14-migrated database must not be opened by a release whose schema support ends below that
version. The rollback order is:

1. While the newer Agentworks CLI is still installed, stop other Agentworks processes that may use
   the state database.
2. Restore the chosen pre-migration backup:

   ```console
   agw database restore PATH
   ```

3. Confirm the displayed source and live destination, or use `--yes` only in intentional
   non-interactive recovery.
4. Do not run another ordinary newer-version command after restore.
5. Install the older Agentworks release and resume normal use.

Restore validates the source before opening the live destination, copies through SQLite's online
backup API, and exits without running migrations. The selected backup remains available for retry.
Agentworks does not coordinate other processes during restore; exclusive operator use is a stated
precondition. Restore does not automatically back up the database it replaces; an operator who wants
that additional artifact runs `agw database backup` first.

An on-demand manual backup can be restored through the same command. Manual backups are never
removed by automatic retention.

## 6. Worked example

An operator starts with schema version 12 and runs a 0.14 command whose latest schema is 14. With
the default interactive answer, Agentworks creates a file shaped like:

```text
database-backups/agentworks-pre-migration-20260809T224501123456Z-v12.db
```

Migration 13 commits, then migration 14 fails after an earlier statement has applied. The live
database is not assumed to have rolled back. Agentworks closes it and reports a recovery command
equivalent to:

```console
agw database restore "$HOME/.config/agentworks/database-backups/agentworks-pre-migration-20260809T224501123456Z-v12.db"
```

After the operator runs that command and confirms, the live database again contains the exact
pre-migration schema and data represented by the backup. A subsequent 0.14 writable command would
offer the migration again.

## 7. Compatibility and rollback matrix

| Situation                                | Supported outcome                                              |
| ---------------------------------------- | -------------------------------------------------------------- |
| 0.14 CLI with current 0.14 schema        | Opens normally; no automatic backup                            |
| 0.14 CLI with older schema               | Notice, backup decision, then migration                        |
| 0.14 CLI with future schema              | Refuses before writable access                                 |
| Older CLI with restored older backup     | Opens according to that release's existing contract            |
| Older CLI with 0.14 schema               | Unsupported; restore before downgrade                          |
| Restore an older valid backup using 0.14 | Copies without migration; next ordinary command owns migration |
| Restore invalid or malformed input       | Refuses before changing live state                             |

## 8. Residual and removal policy

There is no temporary compatibility code to remove. Closeout searches for old claims that migrations
are silent or always immediate, direct writable schema checks, completion commands without the
hidden probe marker, and current documentation that omits restore-before-downgrade ordering.
Historical changelog entries and locked SDDs remain unchanged.

The SDD can lock after implementation, review, isolated CLI validation, and the full repository gate
are complete. Its lasting contracts must already live in the CLI reference, config sample and
reference, upgrade guide, migration guide topic, and tests.
