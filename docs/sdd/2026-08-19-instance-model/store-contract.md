# Instance State Store Contract

- Status: Draft for R2 implementation
- Date: 2026-08-23
- Requirements: R2 and the storage boundary required by R3 through R5 in `frd.md`
- Basis: `database-assessment.md`

## Contract summary

The instance state store is one additive SQLite table behind one typed repository that shares the
owning `Database` connection. The table is a storage envelope, not a public record API. Consumers
use closed, named methods for desired overlays and applied-state slices. A consumer cannot select a
record type, issue a generic filter, or pass SQL.

The first implementation establishes the store and its closed repository surface. R3 supplies the
first domain payload codecs and lifecycle calls. Later consumers add their own payload model, codec,
private record-type discriminator, and named repository methods. Adding a consumer must not expose
the physical envelope as a general-purpose bag.

## Physical envelope

The additive `instance_records` table has this semantic shape:

| Column            | Contract                                                                              |
| ----------------- | ------------------------------------------------------------------------------------- |
| `instance_kind`   | One of `vm`, `workspace`, `agent`, or `session`.                                      |
| `instance_name`   | The stable name used by the owning instance table.                                    |
| `record_type`     | A private repository discriminator, initially `desired-overlay` or `applied-state`.   |
| `record_key`      | `spec` for the one desired overlay, or a repository-enumerated applied-slice key.     |
| `payload_version` | A positive version selected by the consuming domain's codec.                          |
| `value_json`      | Canonical JSON object encoding of the domain payload.                                 |
| `recorded_at`     | UTC time at which this value became authoritative.                                    |
| `operation`       | The lifecycle operation that established applied state; null for desired declaration. |

The primary key is `(instance_kind, instance_name, record_type, record_key)`. An index beginning
with `(instance_kind, record_type)` serves batch reads. The migration creates no rows. Existing VM
hardware columns remain the authority for as-provisioned values, but they do not prove when or by
which operation those values were applied. They therefore do not justify synthesizing new applied
provenance for historic instances. Current declarations and template resolution are not applied
evidence either.

Database constraints enforce the desired-overlay and applied-state envelopes but do not impose
operation semantics on unknown future private record types. Their owning consumers define those
semantics when they add named repository methods.

The repository closes keys within each known record type. For `applied-state`, a public closed key
type and valid instance-kind/key pairs are checked on writes and persisted reads. Adding a key is a
reviewable repository change; a caller cannot create one by spelling a new string. The current keys
are VM-only: `hardware-provenance` and `ssh-identity`. Workspace, agent, and session applied-state
keys are empty until a reviewed consumer adds them.

An unknown applied key is well formed only when it is 1 to 64 ASCII characters in lower-kebab form:
a lowercase letter followed by lowercase letters or digits, with single hyphens separating nonempty
segments. A well-formed unknown key is evidence written by a newer release, not corruption. An older
repository omits that unconsumed slice from typed reads, while partial replacement preserves its
row. Known keys attached to invalid owner kinds, malformed keys, and malformed envelopes remain
`StateError`.

There is deliberately no polymorphic foreign key. Typed instance-deletion paths remove their owned
records in the same transaction that removes the owner. This avoids a universal instance parent
table while preserving lifecycle cleanup.

## Typed carriers

The repository boundary uses three frozen value records:

- `VersionedPayload(payload_version, value)` carries one already validated domain payload as a JSON
  object. The repository owns canonical encoding, not domain validation.
- `DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)` is desired declaration
  only. Its absence means no instance overlay.
- `AppliedStateSlice(instance_kind, instance_name, key, payload, operation, recorded_at)` is
  evidence established by one completed lifecycle operation. Its key uses the closed
  `AppliedStateKey` type. Its absence means not recorded.

The repository validates persisted JSON and envelope invariants on every read. A missing row returns
`None` or an empty tuple according to the named method. A present malformed row raises `StateError`;
it never degrades to absence, match, or drift. The consuming domain then selects the decoder by
`payload_version` and validates the decoded object as its own persisted-data boundary.

## Closed repository surface

The public method set is:

```text
get_desired_overlay(instance_kind, instance_name) -> DesiredOverlayRecord | None
put_desired_overlay(instance_kind, instance_name, payload) -> DesiredOverlayRecord
clear_desired_overlay(instance_kind, instance_name) -> None
list_desired_overlays(instance_kind) -> tuple[DesiredOverlayRecord, ...]

get_applied_slices(instance_kind, instance_name) -> tuple[AppliedStateSlice, ...]
replace_applied_slices(instance_kind, instance_name, operation, slices: Mapping[AppliedStateKey, VersionedPayload]) -> tuple[AppliedStateSlice, ...]
list_applied_slices(instance_kind) -> tuple[AppliedStateSlice, ...]
```

No public method accepts `record_type`, a caller-authored record key, raw JSON text, a SQL fragment,
or an arbitrary filter. Future integration applied-state and artifact ownership consumers add named
methods to this closed surface. They do not gain a generic escape hatch.

`replace_applied_slices` canonical-encodes every supplied payload before writing. It then inserts or
replaces the supplied slice keys with one operation and one timestamp in a single transaction.
Slices not established by that operation remain unchanged. An empty input is a no-op, not a request
to erase unrelated evidence. This is what lets VM reinit replace only the facts it actually
reapplied and keeps workspace repair from manufacturing convergence.

## Connection and transaction rules

`Database.instance_state` returns the repository over the same connection. Repository reads
therefore participate in an enclosing read-only snapshot, and writes participate in an enclosing
`Database.transaction()`.

Each standalone repository mutation commits before returning. Inside an explicit transaction it
defers the commit to the outer boundary. Multi-slice replacement always creates or joins a
transaction, so callers never observe only part of one lifecycle checkpoint. Owner deletion and its
private record cleanup use the same connection and transaction.

The repository opens no connection, sidecar, cache, unit of work, or independent transaction
manager.

## Extension rule

A new consumer is complete only when it adds all of the following together:

1. A domain-owned typed payload model and versioned codec.
2. A private record-type discriminator owned by the repository.
3. Closed, consumer-named repository methods with the minimum required query shapes.
4. Persisted-boundary tests for absent, valid, malformed, and unsupported-version data.
5. Lifecycle or declaration integration that writes only facts the operation can prove.
6. Permanent documentation updates beside the code.

A new slice within an existing record type also adds its repository-owned closed key and valid
instance-kind pairing in the same change. Callers never supply an unregistered string key. The
extension remains backward compatible only while older readers ignore unknown well-formed keys and
partial replacement preserves unrelated rows.

New table columns are justified only by query or integrity requirements shared across consumers.
Consumer-specific payload fields remain in the versioned JSON object. Integration applied-state and
artifact ownership receive no speculative schema in this effort.

## Explicit non-contracts

The store does not provide:

- a public blob or key-value API;
- generic record-type registration by plugins;
- arbitrary filters or caller-authored SQL;
- backfill of applied state;
- drift comparison or remediation;
- SSH identity selection or agent behavior; or
- full transactional convergence for unrelated legacy CRUD.
