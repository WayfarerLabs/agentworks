# Degraded Runnable Recovery: Prior-Art Research

- Status: Design
- Date: 2026-09-06

## Executive summary

The external standards support the narrow design: use SQLite's own foreign-key diagnostic as a
fail-closed migration gate, translate its implementation exception at the application boundary, and
represent an unavailable derived machine fact without coercing it into an existing string field. The
resource-isolation behavior is already established internally by runnable status observation; this
effort applies that policy at list selection rather than inventing a new status system.

## Findings and decisions

### Foreign-key validation is a row-producing diagnostic

SQLite documents `PRAGMA foreign_key_check` as returning one row for each violated constraint,
including the child table, row identifier, referenced table, and constraint index. A non-empty
result is therefore authoritative evidence that migration must not checkpoint the target version.
This effort keeps the check after every migration step and changes only the application exception.

Source:
[SQLite PRAGMA foreign_key_check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)

### SQLite exceptions are implementation-facing

Python documents `sqlite3.IntegrityError` as the database exception for affected relational
integrity, including foreign-key failures. That is useful inside the database adapter but does not
carry Agentworks entity metadata or operator remediation. The database boundary should translate the
deliberately detected violation into `StateError`, while preserving the original safe opener's
stronger partial-migration warning where applicable.

Source:
[Python sqlite3 exceptions](https://docs.python.org/3/library/sqlite3.html#sqlite3.IntegrityError)

### JSON supports explicit absence, but project compatibility forbids widening the old field

RFC 8259 defines null as a JSON primitive distinct from strings. A session whose workspace row is
missing has no derivable VM name, and `"unknown"`, `"-"`, or `""` would overload the existing string
field. Agentworks JSON v1 also forbids changing an existing field type or collection meaning, so the
compatible narrow choice is to retain typed failure when no string can be represented.

Source: [RFC 8259, JSON values](https://www.rfc-editor.org/rfc/rfc8259.html#section-3)

### Internal prior art favors per-target isolation

The locked runnable-status design initializes every requested status to unknown, observes bounded
targets independently, and retains successful rows when another operational target fails. Session
and console observers already use this shape for transport and parser failures. Structural
incompleteness should join the same partition before target construction.

Source: `docs/sdd/2026-09-03-runnable-status-inspection/locked.md`

### Internal error taxonomy distinguishes preflight refusal from partial migration

`MigrationBlockedError` promises that its precondition failed before schema or data changes. The
generic post-step foreign-key check runs after a migration step and cannot make that promise.
`StateError` is therefore the honest direct-boundary type, and the safe opener retains its existing
partial-migration recovery wrapper.

Source: `cli/agentworks/errors.py` and `cli/agentworks/db/backup.py`

## Refuted approaches

- **Ignore violations to make inventory available.** Rejected because it advances an inconsistent
  schema and weakens the fail-closed migration contract.
- **Use `MigrationBlockedError` for every foreign-key result.** Rejected because the check may run
  after DDL or data changes and that subtype explicitly promises otherwise.
- **Use null or a display sentinel in `sessions[].vm_name`.** Rejected because null changes the v1
  type, while a string sentinel changes its meaning and forces consumer heuristics.
- **Move unrepresentable rows into an additive collection.** Rejected because existing consumers
  would receive exit zero with an incomplete `{sessions}` result, changing that collection's meaning
  even though its item type stayed stable.
- **Make all required-relationship helpers forgiving.** Rejected because lifecycle and focused
  operations need a valid target and authority boundary.
- **Add a generic runnable observer.** Rejected because sessions and consoles store different
  relationships and classify different evidence; only the failure policy is shared.

## Sources

| Source                                  | Quality                 | Design angle                              |
| --------------------------------------- | ----------------------- | ----------------------------------------- |
| SQLite PRAGMA documentation             | Primary                 | authoritative violation diagnostic        |
| Python `sqlite3` documentation          | Primary                 | adapter exception semantics               |
| RFC 8259                                | Primary standard        | machine representation of absence         |
| Agentworks locked SDD and error modules | Primary project sources | established isolation and error contracts |

-- agw-ns-onboard-disco
