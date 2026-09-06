# Degraded Inventory and Observation: Low-Level Design

- Status: Design
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Session structural facts

Resolve one internal list-only fact with no presentation strings:

```python
def session_list_vm(db: Database, session: SessionRow) -> tuple[str | None, bool]: ...
```

The resolver receives `Database` and `SessionRow`, uses `get_workspace` and `get_vm`, and never
raises for a missing relation. A missing workspace yields `(None, False)`. An existing workspace
whose VM is missing yields `(workspace.vm_name, False)`. Both rows remain selected. The stored
workspace name stays on `SessionRow`; the helper does not duplicate it.

The exact helper name may change during implementation to match the surrounding module vocabulary;
the behavior and single source of structural classification shall not.

`session_listing` resolves each selected row once, initializes its status to unknown when requested,
passes only structurally complete rows to the existing strict observer, and merges those results.
The exact implementation may use simple local dictionaries instead of retaining this helper if that
is clearer. `observe_session_statuses` does not change.

The service adds a positive `require_vm_names: bool = False` policy. The JSON v1 adapter passes
`True`; human and names-only paths retain the default. If any selected session's workspace is
missing, the service raises the existing typed missing-workspace error during local projection,
before printing progress or dispatching status work. A missing VM row does not trip this policy
because the existing workspace still supplies a truthful string VM name.

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
- Keep table assertions structural (column alignment or parsed JSON), never exact authored prose.

-- agw-ns-onboard-disco
