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

Owner-existence guards use the narrow `has_instance_records` query before an insert so orphaned
desired or applied state cannot silently acquire a new owner. VM backup uses the equally narrow
`has_vm_owner_tree_desired_overlay` and `list_vm_owner_tree_desired_overlays` queries. Those methods
select exactly the VM, its workspaces and agents, and those workspaces' sessions in SQL before any
payload is decoded. A malformed selected row therefore fails the backup, while a malformed row for
an unrelated owner cannot block it. These named predicates are part of the repository contract;
callers do not recreate the polymorphic owner-tree query or filter a decoded global record list. The
same backup snapshot reads the named VM's applied slices through `get_applied_slices`; R3 keys are
VM-only, so applied state does not require a second owner-tree query. Backup then decodes and
re-encodes each known slice through its VM-domain codec before exporting it.

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

The version-1 VM payloads are deliberately compact and non-secret:

- `hardware-provenance` is `{}`. The row itself proves that VM create reached its successful
  lifecycle checkpoint; row-backed CPU, memory, disk, and swap values are not duplicated.
- A verified `ssh-identity` is
  `{"status":"verified","private_key_ref":"...","fingerprint":"SHA256:..."}`.
- An identity whose recognized private-key format cannot expose its public identity
  non-interactively is `{"status":"unverifiable","private_key_ref":"..."}`. It records that the
  authorized-key write completed, but claims neither a fingerprint nor a comparison result.

Neither SSH payload stores private or public key material, passphrases, or agent state. Absence is
different from `unverifiable`: it means no successful lifecycle checkpoint currently proves the SSH
write. Ordinary canonical SSH operations refuse absent or drifted evidence before transport, while
recorded-unverifiable evidence may proceed without inventing a match. VM reinit alone may establish
an absent SSH slice; it still refuses known drift and replaces or clears only the SSH fact it can
prove. VM create establishes both the hardware marker and, after a successful authorized-key write
whose retained private identity remains readable and stable through the local checkpoint, the SSH
slice. Recovery and cleanup roots such as rekey and VM delete remain available without this
ordinary-operation proof.

`clear_applied_slices` removes only one supplied registered key for one typed owner. It rejects
caller-authored strings and keys registered for a different owner kind, treats an already absent key
as a no-op, and joins an enclosing lifecycle transaction. This is the narrow path for discarding
evidence that a lifecycle side effect made uncertain; it cannot delete a desired overlay, a future
unknown slice, or another owner's evidence.

VM desired overlays use one owner record for the paired final VM and admin layers. New writes use
payload version 2 with explicit `vm` and `admin` components. Readers retain compatibility with the
legacy payload-version-1 flat VM layer, treating its admin component as absent. Other instance kinds
continue to use their direct payload-version-1 layer. This payload evolution does not change the
physical store or require a database migration.

That declaration/evidence distinction controls forward compatibility. An older release refuses a
desired overlay with an unknown field or unsupported payload version rather than silently realizing
only the fields it understands. It reports version skew and points to a compatible or newer release,
not to corruption repair. Lifecycle/application paths stay strict; a base-safe read or access path
may explicitly warn and use the base template without claiming the overlay was applied. Applied
state differs because an unknown well-formed key is additive evidence the older release does not
consume, so omitting it from typed reads does not change an operator-authored declaration.

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
