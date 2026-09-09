# Degraded Inventory and Observation: Low-Level Design

- Status: Implemented; locked on merge of PR #764
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Session structural facts

`session_listing` resolves each selected `SessionRow` with non-raising `get_workspace` and `get_vm`
lookups. A missing workspace yields no VM name and is not observable. An existing workspace whose VM
is missing preserves `workspace.vm_name` but is not observable. Both rows remain selected. The
stored workspace name stays on `SessionRow`; no replacement location model is introduced.

`session_listing` classifies each selected row once for the forgiving partition, initializes its
status to unknown when requested, passes only structurally complete rows to the existing strict
observer, and merges those results. The observer may repeat its own strict relationship resolution
for the complete rows it receives. The exact implementation may use simple local dictionaries
instead of retaining this helper if that is clearer. `observe_session_statuses` does not change.

The service adds a positive `require_vm_names: bool = False` policy. The JSON v1 adapter passes
`True`; human and names-only paths retain the default. If any selected session's workspace is
missing, the service raises the existing typed missing-workspace error during local projection,
before dispatching status work. A missing VM row does not trip this policy because the existing
workspace still supplies a truthful string VM name.

## List and renderer types

The internal `SessionListRow.vm_name` changes to `str | None`. Human rendering projects it with
`or "-"`. Unknown grouping keeps `None` in a separate collection and renders a no-resolved-VM clause
outside the named VM groups.

Machine projection retains the existing v1 item shape in `sessions[]`. Its adapter requests required
VM names from the service, so `session_listing_data` never receives `None`; a defensive assertion at
that closed projection boundary prevents a future caller from silently emitting a changed type.

No description dataclass, database row, or lifecycle signature becomes nullable.

## Observer algorithms

### Sessions

```text
listing result = every selected name -> UNKNOWN
observable = []
for session in selected:
    vm_name, is_observable = resolve list VM fact
    if not is_observable:
        continue
    observable.append(session)
listing result.update(observe_session_statuses(observable))
return listing result
```

The strict observer remains unchanged and retains responsibility for expected identity and transport
failures. An orphan never reaches it, and an orphan does not prevent a later healthy row on the same
stored VM name from being included through its own valid workspace.

### Consoles

```text
listing result = every selected name -> UNKNOWN
observable = every console whose stored vm_name resolves through get_vm
listing result.update(observe_console_statuses(observable))
return listing result
```

The batch observer remains strict. Singular console describe and lifecycle keep their existing
missing-VM behavior without added validation.

## Migration refusal

After each migration step:

```text
violations = PRAGMA foreign_key_check
if violations:
    raise StateError(
        "state database has foreign key violations after migration VERSION",
        entity_kind="database",
        hint="Restore a backup or repair the inconsistent relationships before retrying.",
    )
checkpoint VERSION
commit
```

The message need not include the full violation rows. Tests assert exception type, metadata,
checkpoint behavior, and cause boundaries, never prose wording.

## Restore source validation

A frozen `RestoreSourceInspection` contains `schema_version` and `has_foreign_key_violations`. The
constraint query stops after the first returned row. Validation always performs the existing SQLite,
quick-check, version, expected table-and-column shape, and expected foreign-key-declaration checks;
the keyword-only `allow_foreign_key_violations` policy controls only whether the violation fact
raises a typed `StateError`.

`prepare_restore(backup_path, database_path, ...)` resolves and observes both paths and requires
each existing endpoint to be a regular file. It opens the source in non-blocking mode before SQLite,
records its observed path identity, begins a read transaction, and performs all validation on that
transaction's pinned snapshot. It then opens an existing destination, records its file identity, and
refuses equal observed identities, or records that the destination is absent without creating it. It
returns a context-managed `PreparedRestore` carrying the inspection, paths, still-open source, and
optional existing destination connection. Its argument-free `apply()` creates a private staged file
beside the live path and copies the source snapshot into that file through the bounded online-backup
mechanism. Context exit closes the prepared connections on success, refusal, cancellation, or
failure.

Binding either existing endpoint uses a non-blocking OS descriptor, verifies that it is a regular
file, and compares descriptor and path identities around SQLite's open. Binding a destination uses
SQLite's non-creating `mode=rw` open and retains the SQLite connection afterward. Existing paths
that remain replaced are refused, and retaining the descriptor prevents the original inode from
being reused during the bind. Newly reserved paths remain owned from exclusive creation through the
SQLite bind. Python's pathname-only SQLite API cannot exclude an adversarial replace-and-restore
entirely within one open call; that same-user threat is outside this local recovery contract.

Every writable `Database` acquires a shared SQLite-backed database-use lock before opening the state
database and releases it only after closing the state connection. It resolves the database path
before deriving either the use-lock path or SQLite path, matching restore's canonical identity for
symlinked and absent destinations. After the staged copy completes, restore acquires that lock
exclusively and retains it through installation. An existing destination must then checkpoint its
WAL cleanly, switch to delete journal mode, accept an exclusive SQLite writer lock within the
bounded wait, and retain its observed identity. Apply copies its file mode to the stage, closes the
destination, verifies that no WAL, shared-memory, or rollback-journal sidecars remain, checks the
live path again, and atomically replaces it. An expected-absent destination is installed with a hard
link, whose no-overwrite behavior refuses a concurrent file. Failures and interrupts remove the
private stage only while it retains the identity Agentworks created. SQLite therefore never writes
through the live pathname during the copy, and another Agentworks process cannot recreate old
coordination state before installation.

The existing `validate_restore_source(backup_path) -> int` opens and validates a snapshot, returns
only its schema version, and closes it. The existing
`restore_backup(backup_path, database_path) -> None` prepares and applies a snapshot, preserving
both public signatures and return contracts. The additive prepared API owns the keyword-only
`allow_foreign_key_violations=False` policy because the CLI must present risk and confirmation
between validation and mutation without reopening or racing the source.

The CLI maps `--force` to `allow_foreign_key_violations=True` during preparation. A violating
inspection causes a warning before the existing source/destination confirmation and a second warning
after `apply` succeeds. `--yes` continues to control only confirmation. Warnings remain visible
under `--yes`, and force cannot bypass any other validation result.

## Test seams

- Seed current-schema orphans by disabling foreign-key enforcement only inside test setup, then
  restore enforcement before invoking production code.
- Instrument transport construction and database writes to prove orphans remain local.
- Pair orphan and healthy rows in the same selection to prove partial success.
- Exercise missing workspace and missing VM separately in human projection; prove JSON rejects the
  former before observation and preserves the latter as a string with unknown requested status.
- Exercise unfiltered, agent/admin-filtered, relationship-filtered, and names-only selection.
- Exercise one focused path per resource to prove list tolerance does not leak; existing observer
  and lifecycle tests retain the broader strict contract.
- Build an older schema, inject a relationship violation, and verify direct construction plus safe
  open type and checkpoint behavior.
- Inject a current and historical restore-source violation. Prove default validation and restore
  refuse before destination mutation, forced restore copies, malformed sources and invalid schemas
  still refuse under force, and inspection reports the violation Boolean.
- Change and replace the source path after preparation. Prove apply copies the inspected snapshot,
  not later path content, and prove every exit path closes the prepared source.
- Prove preparation rejects equal resolved paths and altered foreign-key declarations, destination
  replacement cannot redirect apply, decline does not reserve or remove an absent destination,
  failed apply cleans its own reservation, and every exit path closes prepared connections.
- At the CLI boundary, prove force and yes are independent control inputs and capture warnings by
  event and stream rather than asserting authored sentences.
- Keep table assertions structural (column alignment or parsed JSON), never exact authored prose.
