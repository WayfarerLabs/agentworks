# Degraded Runnable Recovery: Prior Art

- Status: Complete; locked on merge of PR #764
- Date: 2026-09-06

## Governing project patterns

- The locked runnable-status design initializes requested rows to unknown, observes bounded targets
  independently, and retains successful rows when another operational target fails. Structural
  incompleteness joins that partition in the list service before target construction.
- The JSON v1 contract permits optional additions but forbids changing an existing type, meaning, or
  collection order. A missing workspace cannot truthfully populate the required string `vm_name`, so
  v1 retains typed failure rather than using null, a sentinel, or an incomplete collection.
- `MigrationBlockedError` promises that its precondition failed before schema or data changed. The
  generic post-step foreign-key check cannot make that promise, so direct construction uses ordinary
  `StateError` and the safe opener keeps its existing partial-migration wrapper.
- Existing restore already separates source validation from destructive confirmation and repeats
  validation inside the copy service. The new `--force` follows the CLI's established meaning of
  bypassing one named safety refusal; `--yes` remains the independent confirmation bypass.
- SQLite read transactions observe an unchanging snapshot, and Python's connection backup API copies
  from an existing source connection. Holding one read-only transaction across validation,
  confirmation, and backup binds the warned facts to the bytes restored without staging another
  database file.

Sources: `docs/sdd/2026-09-03-runnable-status-inspection/locked.md`, `cli/command-reference.md`,
`cli/agentworks/errors.py`, and `cli/agentworks/db/backup.py`.

## External source check

SQLite defines `PRAGMA foreign_key_check` as a row-producing diagnostic for violated constraints,
which supports retaining the non-empty-result migration gate. Python defines
`sqlite3.IntegrityError` as an adapter exception for affected relational integrity, which supports
translating the deliberately detected result at the Agentworks boundary. RFC 8259 confirms that JSON
null is distinct from a string; the project compatibility rule, not JSON syntax, is why null cannot
replace the existing string field.

Sources: [SQLite foreign-key check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check),
[SQLite transaction behavior](https://www.sqlite.org/lang_transaction.html),
[SQLite online backup API](https://www.sqlite.org/backup.html),
[Python sqlite3](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup), and
[RFC 8259 JSON values](https://www.rfc-editor.org/rfc/rfc8259.html#section-3).

## Rejected approaches

- Ignore or repair migration violations: weakens fail-closed migration and mutates operator state.
- Make shared observers forgiving: leaks list recovery policy into five strict non-list callers.
- Use null or a string sentinel for `sessions[].vm_name`: changes the frozen v1 type or meaning.
- Move unrepresentable rows to a sibling collection: changes `{sessions}` from the complete result.
- Add JSON v2 for this edge: introduces version-selection grammar and compatibility work beyond the
  recovery need; human and names-only inventory already expose the row.
- Add a generic runnable observer: sessions and consoles store and classify different evidence.
- Let restore force bypass all source validation: recovery value does not justify accepting a
  malformed, corrupt, unsupported, or schema-invalid database.
- Make `--force` imply `--yes`: accepting inconsistent relationships and authorizing replacement are
  distinct operator decisions.
