# HLA: Instance Model and State

- Status: R2 accepted; R4 implemented; R3 and R5 design in progress
- Date: 2026-08-23
- Last revised: 2026-08-26
- FRD: [frd.md](./frd.md)
- Assessment: [database-assessment.md](./database-assessment.md)
- Store contract: [store-contract.md](./store-contract.md)
- Saga: `docs/sdd/2026-08-04-next-steps/`

## Architectural summary

The feature adds one database-backed desired/applied instance-state seam, one shared typed layer
runner, domain-owned payload codecs, and projections into existing lifecycle and inspection
surfaces. It does not add an ORM, a second declaration frontend, a generic record API, or a new
platform-default layer.

```text
template declarations --> existing inheritance linearization -+
                                                             |
optional DB overlay ----------------------------------------+--> typed layer runner
                                                                  + domain reducer
                                                                  + value provenance
                                                                          |
                                                                          v
                                                               current resolved spec
                                                                  |              |
                                                        lifecycle apply      R5 inspection
                                                                  |
                                         proven successful slices only
                                                                  |
                                                                  v
Database connection --> InstanceStateRepository --> instance_records
       |                       |                           |
       |                       +--> desired overlay        +--> versioned JSON objects
       |                       +--> applied slices         +--> operation + time
       |                       +--> typed deletion
       |
       +--> existing instance rows, transactions, read snapshots, migration safety
```

The physical table is generic only as storage. Its consumer surface is closed and enumerated.
Desired overlays and applied facts remain distinct even when they share the table. An overlay is
current intent; only a successful lifecycle operation can establish applied state.

## R2: instance state repository

### Physical record

Migration 32 adds `instance_records` with the envelope specified in `store-contract.md`:

```text
PRIMARY KEY (instance_kind, instance_name, record_type, record_key)

instance_kind    vm | workspace | agent | session
instance_name    owning instance name
record_type      private repository discriminator
record_key       spec or a repository-enumerated slice key
payload_version  positive domain payload version
value_json       canonical JSON object
recorded_at      authoritative UTC timestamp
operation        lifecycle provenance for applied state; null for desired declaration
```

An index beginning with `(instance_kind, record_type)` serves doctor and show batch reads. No row is
backfilled. The table has no polymorphic foreign key because four owner tables would otherwise
require a universal parent that has no product meaning. Typed deletion hooks preserve ownership.
Database constraints enforce the two known discriminators only. Later private record types remain
free to define their own operation semantics with their named repository methods.

Schema migration remains an ordinary additive migration under the existing safe-open policy. The
exact restore sentinel is updated in the same change. Backup, migration locking, failure recovery,
and read-only open behavior are unchanged.

### Repository boundary

`Database.instance_state` returns an `InstanceStateRepository` over the same SQLite connection. The
repository owns every statement against `instance_records`, canonical JSON encoding, persisted
envelope validation, deterministic ordering, and record-type constants. Domain code never receives a
raw table row and cannot select `record_type`.

The repository exposes only the methods listed in `store-contract.md`. The typed carriers
distinguish desired overlay records from applied-state slices. Their payload is a versioned JSON
object that a domain codec has already validated on write and validates again on read. The
repository rejects a malformed persisted JSON object with `StateError`; it does not convert
corruption into absence.

Known record types also close their key space in repository code. Applied-state methods accept a
closed key type, and both writes and persisted reads enforce valid instance-kind/key pairs. A caller
cannot create a new slice by spelling a string.

An unknown applied key is well formed only when it is 1 to 64 ASCII characters in lower-kebab form:
a lowercase letter followed by lowercase letters or digits, with single hyphens separating nonempty
segments. A well-formed unknown key is an unconsumed fact written by a newer release, not a
malformed row. Older readers omit it from typed results, and partial replacement preserves its row.
Known keys attached to invalid owner kinds, malformed keys, and malformed envelopes still raise
`StateError`.

Desired overlays are stricter because they are operator declarations rather than additive evidence.
An older release refuses an unknown desired field or payload version instead of realizing only the
subset it understands. The error identifies version skew and recommends a compatible or newer
release, not corruption repair. Lifecycle/application paths remain strict; a base-safe read or
access path may explicitly warn and use the base declaration without claiming that the overlay was
applied.

Standalone writes commit before returning. Inside `Database.transaction()` they defer to the outer
boundary. Applied-slice replacement encodes the complete input first, then inserts or replaces the
supplied keys with one operation and timestamp in one transaction. It leaves unrelated slices
untouched. This is partial replacement by proof, not whole-instance convergence.

VM, workspace, agent, and session delete paths remove corresponding records in the same transaction.
VM deletion also removes state for the agents, workspaces, and sessions it deletes; workspace
deletion removes state for its sessions. This is the only existing CRUD family R2 expands, because
leaving polymorphic orphans would violate the new store's ownership contract.

### Extension contract

A later consumer extends the repository in code with:

1. a domain payload and versioned codec;
2. a private repository discriminator;
3. consumer-named methods for its exact reads and writes; and
4. malformed, unsupported-version, lifecycle, and projection tests.

A new slice on an existing record type adds its repository-owned key and valid owner-kind pairing in
the same reviewed change. This remains backward compatible only while unknown well-formed keys are
omitted by older readers and partial replacement preserves unrelated rows.

It does not register a plugin-provided record type at runtime or call a generic get/put method. Wave
4 can add integration applied-state this way without changing table, connection, transaction,
absence, or deletion semantics. No artifact-ownership model is invented before wave 6 states its
requirements.

## Shared resolution stack

The four inheriting template kinds already share one inheritance linearization in
`agentworks.resources.inheritance`: parents depth-first and left-to-right, each declaration once at
its earliest position, and the selected row last. They differ only in their typed reducers:

- VM and agent use scalar replacement, map child-wins, and ordered list append-deduplication.
- Workspace uses scalar replacement and map child-wins.
- Session also owns integration selection, integration-specific config merging, and per-key
  provenance.

R4 generalizes the execution of those reducers, not the reducer policies. A shared typed layer
runner accepts an ordered sequence of `(source, declaration)` layers, a domain seed containing
defaults, and a domain reducer. Existing template resolution supplies the inheritance layers. Live
instance resolution appends one optional overlay layer. The same runner produces both, so an overlay
does not create a fifth merge implementation.

The VM owner has two declaration slots. Its VM slot resolves the selected `vm-template` chain plus
the final VM layer. Its admin slot resolves the selected, non-inheriting `admin-template` plus the
final admin layer through the same runner. The admin template's concrete defaults seed that fold;
only fields explicitly supplied by the admin layer contribute. This models two declarations without
inventing a second merge mechanism.

The runner records provenance at the granularity the merge preserves:

- scalar path for replacements;
- map key for child-wins maps;
- item plus contributing layers for append-deduplicated lists; and
- domain-provided output paths for transforms such as session integration config merging.

Defaults originate in the seed and receive `defaulted` provenance. They are not represented as a
platform layer. The existing resolved dataclasses remain compatibility projections while consumers
move to the resolved-value-plus-provenance result.

Overlay payloads reuse each declaration slot's strict template spec model but exclude `name`,
`inherits`, metadata, and framework provenance. Their merge semantics match templates. In
particular, an empty additive list or map does not invent a removal tombstone that template
inheritance does not have.

An effective-instance validator runs after the overlay fold. It reuses domain reference and
capability rules without publishing the overlay as a fake registry template. Shape validation alone
is insufficient because a validly shaped overlay can still name a missing secret, recipe,
credential, install command, or harness integration.

The database is the one desired-overlay authority. R4 does not add VM, workspace, agent, or session
instance documents to the resource-manifest frontend. A spec can be supplied only at an existing
template-setting lifecycle boundary: the four direct creation commands and `agent reinit`. Each
template selection and final partial layer form one effective declaration for validation and
realization. For a VM, its VM and admin declaration slots are persisted together as one versioned
desired payload under the VM owner, so later reinit cannot observe or apply only half of the choice.
There is no desired-only spec mutation surface. The exact argv, inline JSON input, replacement
behavior, validation boundary, and lifecycle-coupling rule are specified in `instance-spec-cli.md`.

JSON `null` is rejected at every depth because omission is the one spelling for no contribution from
a field. It is not an alternate clear or inheritance operator.

The mutation surface reports the final retained desired-state outcome after success or unwind:
whether each layer was set, retained, replaced, cleared, or explicitly left absent. It identifies
the declaration slot and names sorted top-level fields without echoing values, because declaration
data may include plaintext environment values. This is desired-state feedback, not a claim that
remote work applied the declaration.

## R3: applied state and SSH identity

### Domain slices

Domain payloads live outside `agentworks.db`. The first VM codecs cover:

- an applied-provenance marker for row-backed hardware that a post-R3 VM create actually
  established, without copying the CPU, memory, disk, or swap values out of the VM row; and
- provisioned SSH identity, including the configured private-key reference and either an
  authoritative public fingerprint or a recorded unverifiable outcome.

Absence and recorded-unverifiable are different. Absence means no operation recorded this slice.
Recorded-unverifiable means an operation applied the configured public identity but the configured
private format did not expose its public identity non-interactively. Neither is a match or drift.

### Identity derivation

A pure leaf module owns SSH public-identity parsing and fingerprinting. It reads the configured
private path chosen by `OperatorConfig` and used by transport construction. For `openssh-key-v1`,
it:

1. decodes the PEM armor;
2. requires the `openssh-key-v1` magic;
3. parses bounded SSH length-prefixed fields through the declared public-key count;
4. fingerprints the first authoritative public blob with SHA-256; and
5. formats the standard OpenSSH fingerprint without trailing padding.

The parser never reads a sibling `.pub`, calls `ssh-keygen -lf PRIVATE_PATH`, prompts for a
passphrase, or adds a cryptography dependency. Truncated lengths, unsupported envelopes, and file
errors return typed outcomes rather than partial identities.

The configured public key is what initialization writes to `authorized_keys`; the configured private
key is what Agentworks passes through `ssh -i`. Before remote application, R3 compares those two
identities when both are verifiable. A mismatch refuses before the key write and cannot create a
false applied record. A protected legacy format that cannot reveal its private public half remains
supported but unverifiable.

OpenSSH may still consider agent-held or default identities because this effort does not add
`IdentitiesOnly=yes`. The stored fingerprint therefore names the configured transport identity, not
the sole identity the server accepted. Agent selection remains deliberately unresolved.

### Truthful lifecycle capture

Both VM create and reinit reach `run_initialization()`. The current authorized-key reconciliation
swallows an `SSHError` into a warning, so lifecycle return or `InitStatus.PARTIAL` alone does not
prove that identity was applied. R3 changes that helper to return a typed applied/not-applied result
and threads the result through phase B.

The terminal database checkpoint groups:

- final init status;
- the terminal VM event; and
- only the applied slices whose typed outcomes prove success.

The existing status and event methods become transaction-aware only where required for this
composition. Remote work cannot be atomic with SQLite. If the remote key write succeeds and the
database checkpoint fails, Agentworks retains conservative old or absent state and reinit is the
retry path; it never fabricates a successful record.

Before committing an SSH fingerprint, the lifecycle re-derives the configured identity and compares
it with the pre-application result. This detects ordinary path replacement during the operation but
does not claim filesystem and OpenSSH path access are atomic.

Operational checks use an explicit comparison service at SSH-using boundaries. They do not gate all
`LiveVMNode.preflight()` calls because start, stop, delete, and establishment reinit have different
transport needs. Reinit permits not-recorded state so existing VMs can establish it. Cleanup remains
possible. `verify_vm_connection` and ordinary live-SSH compositions are the first consumers; direct
transport call sites receive explicit coverage rather than assuming one boundary owns every
connection.

## R5: resolved and applied projections

Template `resource show` gains an optional typed resolution hook on the four inheriting kinds. The
generic show service projects resolved values and provenance without importing domain reducers.

Live VM, workspace, agent, and session describe services are the natural instance surfaces. They
combine:

- current template plus overlay resolution;
- the persisted desired overlay;
- row-backed applied hardware values plus applied slices from the same database snapshot; and
- structural comparison states: not recorded, unverifiable, match, or drift.

`resource show` already holds one lazy read-only database transaction for live facts. Any overlay or
applied read added there uses that connection. Doctor reads applied SSH slices in one batch, derives
the current configured private identity once per run, and reports per-VM state without an N+1 query.

JSON v1 retains every existing field and adds optional tagged objects. Human and JSON forms project
the same structural facts. Resolved specs include configured secret references only, never resolved
secret values.

A missing or removed template is unresolved current declaration, not the built-in default. A later
edit to the selected VM template can change currently resolved hardware while the VM row still
describes provisioned hardware. The applied marker supplies operation and time proof without
duplicating those values. Its absence on a historic row remains visibly not recorded. VM reinit does
not provision hardware again and must not create or replace that marker merely because other
initialization steps succeeded.

## Failure and integrity behavior

| Condition                                          | Outcome                                                      |
| -------------------------------------------------- | ------------------------------------------------------------ |
| No persisted slice                                 | Visible not recorded                                         |
| Valid recorded-unverifiable SSH payload            | Visible unverifiable, neither match nor drift                |
| Malformed persisted envelope or payload            | `StateError` and database-damage diagnostic                  |
| Present record whose owner no longer exists        | Orphan database-damage diagnostic                            |
| Current key differs from applied fingerprint       | Drift and clean preflight refusal                            |
| Authorized-key reconciliation warns/fails          | No SSH applied slice written                                 |
| Lifecycle remote success, database checkpoint fail | Conservative old/absent state; retry through lifecycle       |
| Candidate spec validation or reference check fails | No desired record written                                    |
| Agent declaration persisted, reinit later fails    | Desired retained; applied old/absent; lifecycle is retryable |

## Deliberate boundaries

This architecture does not:

- add a general repository framework, ORM, unit of work, or connection pool;
- expose the physical record envelope to consumers;
- reconstruct historic applied state;
- make workspace repair convergence;
- create YAML instance manifests or a second instance authority;
- add removal semantics absent from template inheritance;
- remediate drift;
- resolve ssh-agent identity selection;
- force `IdentitiesOnly=yes`; or
- design integration or artifact payloads before their owning waves.

## Validation strategy

R2 receives structural migration and repository tests, transaction and read-snapshot tests, exact
restore-sentinel coverage, private project review, fresh-eyes review, full Python gates, repository
gates, and isolated database acceptance. It does not require a live provider because it changes no
provider or remote behavior.

R3 through R5 additionally receive direct parser fixtures for protected and unprotected key formats,
real isolated-home CLI runs, and live VM validation of apply-time capture, current match, deliberate
identity drift, protected-key behavior, failed-operation non-capture, cleanup, and residue. Test
reports state every skipped environment or format rather than implying coverage.
