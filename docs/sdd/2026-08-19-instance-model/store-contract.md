# Instance State Store Contract

- Status: Accepted and implemented by R2; desired-overlay policy extended by R4
- Date: 2026-08-23
- Revised: 2026-08-26
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
| `record_key`      | `spec` for the one desired declaration record, or an enumerated applied-slice key.    |
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

Desired overlays have the deliberately different forward-compatibility policy. They are operator
declarations, not additive evidence, so an older release never drops an unknown field and realizes
the remainder as though it were the requested declaration. An unknown field or unsupported payload
version raises a typed version-skew `StateError` that recommends a compatible or newer release; it
is not diagnosed as corruption and must not recommend lossy repair. Lifecycle and application paths
remain strict. A read/access path may explicitly warn and use the base template when that is safe,
but it must not claim that it applied the stored overlay.

There is one desired record per instance owner even when the owner has more than one declaration
slot. A VM payload carries independently typed VM and admin layers together. Their CLI inputs,
validation, and folds remain distinct, while one versioned payload prevents persistence, backup,
reinit, or deletion from observing only half of the VM's desired declaration. This composite domain
payload changes no physical column, record key, repository method, or transaction rule.

The corrected composite VM payload uses payload version 2. Readers continue to accept the earlier
version 1 flat VM layer as the VM declaration slot with no admin layer. This is domain-codec
compatibility, not a schema migration or eager database rewrite; existing rows and backup archives
remain readable in place. The compatibility is intentionally one-way: older releases refuse version
2 rows, even when one or both components are empty. New VM writes always use version 2 so every
current record has one explicit two-component shape and one complete-component invariant.

There is deliberately no polymorphic foreign key. Typed instance-deletion paths remove their owned
records in the same transaction that removes the owner. This avoids a universal instance parent
table while preserving lifecycle cleanup.

## Typed carriers

The repository boundary uses three frozen value records:

- `VersionedPayload(payload_version, value)` carries one already validated domain payload as a JSON
  object. The repository owns canonical encoding, not domain validation.
- `DesiredOverlayRecord(instance_kind, instance_name, payload, recorded_at)` is desired declaration
  only. Its absence means no instance overlay. A payload may contain multiple typed declaration
  slots when its owner lifecycle selects them together, as VM creation does for VM and admin.
- `AppliedStateSlice(instance_kind, instance_name, key, payload, operation, recorded_at)` is
  evidence established by one completed lifecycle operation. Its key uses the closed
  `AppliedStateKey` type. Its absence means not recorded.

The repository validates persisted JSON and envelope invariants on every read. A missing row returns
`None` or an empty tuple according to the named method. A present malformed row raises `StateError`;
it never degrades to absence, match, or drift. The consuming domain then selects the decoder by
`payload_version` and validates the decoded object as its own persisted-data boundary,
distinguishing unsupported desired declarations from malformed persisted data as described above.

## Closed repository surface

The public method set is:

```text
get_desired_overlay(instance_kind, instance_name) -> DesiredOverlayRecord | None
put_desired_overlay(instance_kind, instance_name, payload) -> DesiredOverlayRecord
clear_desired_overlay(instance_kind, instance_name) -> None
list_desired_overlays(instance_kind) -> tuple[DesiredOverlayRecord, ...]
has_instance_records(instance_kind, instance_name) -> bool
has_vm_owner_tree_desired_overlay(vm_name) -> bool
list_vm_owner_tree_desired_overlays(vm_name) -> tuple[DesiredOverlayRecord, ...]

get_applied_slices(instance_kind, instance_name) -> tuple[AppliedStateSlice, ...]
replace_applied_slices(instance_kind, instance_name, operation, slices: Mapping[AppliedStateKey, VersionedPayload]) -> tuple[AppliedStateSlice, ...]
clear_applied_slices(instance_kind, instance_name, keys: Collection[AppliedStateKey]) -> None
list_applied_slices(instance_kind) -> tuple[AppliedStateSlice, ...]
```

No public method accepts `record_type`, a caller-authored record key, raw JSON text, a SQL fragment,
or an arbitrary filter. Future integration applied-state and artifact ownership consumers add named
methods to this closed surface. They do not gain a generic escape hatch.

`has_instance_records` is the creation guard for orphan state. It observes any current or future
private record type for one exact typed identity without decoding a consumer payload, so creation
cannot silently adopt state left behind without its owner.

The two VM owner-tree operations are the closed backup query shapes. They select desired-overlay
rows for the named VM and its current workspace, agent, and session descendants in SQL before any
payload is decoded. Malformed state in that selected owner tree therefore fails the backup snapshot,
while malformed state belonging to another VM is outside the read and cannot block it. The
repository owns the owner-tree SQL and desired-overlay discriminators; backup consumers receive only
typed records or the narrow existence result. The VM's single composite desired record keeps its VM
and admin declaration slots together through this projection.

`replace_applied_slices` canonical-encodes every supplied payload before writing. It then inserts or
replaces the supplied slice keys with one operation and one timestamp in a single transaction.
Slices not established by that operation remain unchanged. An empty input is a no-op, not a request
to erase unrelated evidence. This is what lets VM reinit replace only the facts it actually
reapplied and keeps workspace repair from manufacturing convergence.

`clear_applied_slices` accepts only registered applied keys valid for the supplied instance kind and
deletes exactly those keys for one instance. An empty collection and already-absent keys are no-ops.
This narrow mutation removes evidence that a lifecycle knows became uncertain after a remote side
effect. It does not accept unknown strings, expose record types, or turn absence into an applied
state. Like replacement, it joins an enclosing lifecycle transaction.

## Connection and transaction rules

`Database.instance_state` returns the repository over the same connection. Repository reads
therefore participate in an enclosing read-only snapshot, and writes participate in an enclosing
`Database.transaction()`.

Each standalone repository mutation commits before returning. Inside an explicit transaction it
defers the commit to the outer boundary. Multi-slice replacement always creates or joins a
transaction, and applied-slice clearing joins one when present, so callers never observe only part
of one lifecycle checkpoint. Owner deletion and its private record cleanup use the same connection
and transaction.

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
