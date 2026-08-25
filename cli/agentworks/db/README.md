# Instance State Store

`Database.instance_state` is the typed persistence boundary for desired instance overlays and
applied-state slices. It uses the owning `Database` connection, so reads share its snapshot and
writes join its transaction.

`Database` owns repository construction. Callers do not supply a connection or transaction manager.

The `instance_records` table is a storage envelope, not a public record API. Callers use the named
desired-overlay and applied-state methods on `InstanceStateRepository`. They cannot choose a record
type or caller-authored record key, provide raw JSON or SQL, or issue arbitrary filters. The
repository owns canonical JSON object encoding and treats a malformed persisted envelope as
`StateError`, never as an absent record.

Because the polymorphic table deliberately has no owner foreign key, a damaged or hand-edited
database can contain identities that normal creation paths reject. Operator-facing errors retain a
valid owner kind but include the owner name only when its representation is printable and bounded;
persisted payload values and raw record keys are never used as diagnostic context.

Desired overlays express current intent. Applied slices are evidence from a completed lifecycle
operation. Applied slices use a repository-owned closed key type whose valid instance-kind/key pairs
are checked on writes and persisted reads. The current VM-only keys are `hardware-provenance` and
`ssh-identity`; workspace, agent, and session currently accept none. `replace_applied_slices`
replaces only the supplied slice keys, with one operation and one timestamp, and preserves all
unrelated facts. Empty replacement is a no-op. Existing instances have no synthesized records:
absence means not recorded until a lifecycle operation establishes state.

An unknown applied key is well formed only when it is 1 to 64 ASCII characters in lower-kebab form:
a lowercase letter followed by lowercase letters or digits, with single hyphens separating nonempty
segments. A well-formed unknown key is evidence written by a newer release, not corruption. An older
release omits that unconsumed slice from typed reads and partial replacement preserves its row.
Known keys attached to invalid owner kinds, malformed keys, and malformed envelopes still fail
loudly.

Every owner deletion must remove its records in the same transaction. VM deletion also removes
records for the agents, workspaces, and sessions it deletes; workspace deletion removes records for
its sessions. SQL against `instance_records` stays inside `InstanceStateRepository`, including its
private owner-batch helper.

## Extending the store

A new consumer adds all of these together:

1. A domain-owned typed payload and versioned codec.
2. A private record-type discriminator in `InstanceStateRepository`.
3. Consumer-named repository methods for only the required reads and writes.
4. Boundary tests for absent, valid, malformed, and unsupported-version data.
5. Lifecycle or declaration integration that records only facts the operation can prove.

A new slice on an existing record type adds its closed key and valid owner-kind pairing in
repository code; callers cannot mint keys by spelling a new string. This extension remains backward
compatible only while older readers ignore unknown well-formed keys and replacements preserve
unrelated rows.

Consumer fields belong in the versioned JSON object unless the shared store needs them for integrity
or a shared query. Do not add a generic blob API, runtime record-type registry, sidecar connection,
cache, ORM, or independent transaction manager.
