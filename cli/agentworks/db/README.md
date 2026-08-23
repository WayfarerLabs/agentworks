# Instance State Store

`Database.instance_state` is the typed persistence boundary for desired instance overlays and
applied-state slices. It uses the owning `Database` connection, so reads share its snapshot and
writes join its transaction.

`Database` owns repository construction. Callers do not supply a connection or transaction manager.

The `instance_records` table is a storage envelope, not a public record API. Callers use the named
desired-overlay and applied-state methods on `InstanceStateRepository`. They cannot choose a record
kind, provide raw JSON or SQL, or issue arbitrary filters. The repository owns canonical JSON object
encoding and treats a malformed persisted envelope as `StateError`, never as an absent record.

Desired overlays express current intent. Applied slices are evidence from a completed lifecycle
operation. `replace_applied_slices` replaces only the supplied slice keys, with one operation and
one timestamp, and preserves all unrelated facts. Empty replacement is a no-op. Existing instances
have no synthesized records: absence means not recorded until a lifecycle operation establishes
state.

Every owner deletion must remove its records in the same transaction. VM deletion also removes
records for the agents, workspaces, and sessions it deletes; workspace deletion removes records for
its sessions. SQL against `instance_records` stays inside `InstanceStateRepository`, including its
private owner-batch helper.

## Extending the store

A new consumer adds all of these together:

1. A domain-owned typed payload and versioned codec.
2. A private record-kind discriminator in `InstanceStateRepository`.
3. Consumer-named repository methods for only the required reads and writes.
4. Boundary tests for absent, valid, malformed, and unsupported-version data.
5. Lifecycle or declaration integration that records only facts the operation can prove.

Consumer fields belong in the versioned JSON object unless the shared store needs them for integrity
or a shared query. Do not add a generic blob API, runtime record-kind registry, sidecar connection,
cache, ORM, or independent transaction manager.
