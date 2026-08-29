# HLA: Instance Model and State

- Status: R1, R2, and R4 merged; merge-strategy correction merged; R3 implemented and under review;
  R5 pending
- Date: 2026-08-23
- Last revised: 2026-08-29
- FRD: [frd.md](./frd.md)
- Assessment: [database-assessment.md](./database-assessment.md)
- Store contract: [store-contract.md](./store-contract.md)
- Saga: `docs/sdd/2026-08-04-next-steps/`

## Architectural summary

The feature adds one database-backed desired/applied instance-state seam, one shared typed layer
runner with schema-directed field policy, domain-owned payload codecs, and projections into existing
lifecycle and inspection surfaces. It does not add an ORM, a second declaration frontend, a generic
record API, or a new platform-default layer.

```text
template declarations --> existing inheritance linearization -+
                                                             |
optional DB overlay ----------------------------------------+--> typed layer runner
                                                                  + model-directed merge
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
its earliest position, and the selected row last. A shared typed layer runner accepts the resulting
ordered `(source, declaration)` layers and a domain seed containing defaults. Existing template
resolution supplies the inheritance layers; live instance resolution appends one optional overlay
layer. The same runner produces both, so an overlay does not create a fifth merge implementation.

The merge-strategy correction replaces the five hand-written field-policy reducers with one schema
walker. Declaration models own policy for both core fields and capability config. The strategy
vocabulary is closed and registration-validated:

- `merge` is the default for objects and mappings. It preserves non-conflicting keys and recursively
  consults the schema node for every conflicting key.
- `append-dedupe` is the default for lists. It preserves first-seen order and records every layer
  that contributes an equal item. Equality is structural and concrete-type-sensitive over the exact
  JSON carrier; an opaque or cyclic Python value is never equal and never executes authored equality
  code. Conformance requires an append-deduped list's element schema to stay within that comparable
  carrier or tells its author to mark the list `replace`.
- `replace` is the default for scalars and the explicit alternative for an object or list. It
  discards the complete prior value at that node. An empty replaced object or list therefore clears
  that subtree or list without adding a general removal language.

Conformance examines the annotation-declared structural domain without executing or inferring model
or before validators. Those validators do not enlarge the comparison carrier: admitted raw values
already in the carrier retain concrete-type comparison, while values outside it remain unequal and
reach final validation.

A field declares its strategy directly as typed Pydantic annotation metadata. A mapping-shaped
structured model can declare its own root strategy for every use; a containing field override takes
precedence over that model policy, and the shape default applies last. Explicit metadata may restate
an inherited policy without creating a second validation rule. Strategy metadata is code-owned model
policy, not a manifest key, serialized desired state, or second operator-facing type system.
Registration rejects duplicate strategy metadata, strategies incompatible with the annotated shape,
and strategy metadata placed where the merge engine has no conflict identity. Mapping value
annotations and every model recursively reachable through them are traversal positions; mapping keys
and individual sequence elements are not. A merge-by-key mapping requires exact-string keys or must
choose whole-node replacement. The v2 contract uniformly refuses validation aliases in every
reachable participating model, including through mapping values and below replacement boundaries, so
registration never depends on the path by which a model is reached. Serialization-only aliases
remain valid.

For discriminated and structural unions, a containing-field replacement wins before arm selection.
Otherwise the walker selects both arms. Different arms, or arms that cannot be selected, replace the
complete union node even under a containing-field `merge`. Equal selected arms apply an explicit
containing-field `merge` when present, then the selected model's root policy and the object default.
Only an explicit same-arm override can therefore produce a composite that differs from the arm
model's normal replacement policy; values from different arms never combine. An unknown schema key
survives, but a later conflict at that key replaces the earlier raw value instead of recursively
merging by runtime shape. A wholly unknown integration config has no usable schema, so a later
declaration replaces its complete prior config and the Registry's miss policy reports the selector
error. The merge engine never filters an invalid list item, converts `null`, or otherwise turns
invalid authored or persisted data into a valid effective declaration. An active-identity guard
makes the walk terminate if the deliberately open `CapabilityBlock` boundary supplies a cyclic
container.

The VM owner has two declaration slots. Its VM slot resolves the selected `vm-template` chain plus
the final VM layer. Its admin slot resolves the selected, non-inheriting `admin-template` plus the
final admin layer through the same runner. The admin template's concrete defaults seed that fold;
only fields explicitly supplied by the admin layer contribute. This models two declarations without
inventing a second merge mechanism.

The runner records provenance at the granularity the schema merge preserves:

- scalar path for replacements;
- nested leaf path for recursive object conflicts;
- resulting list position plus contributing layers for append-deduplicated lists; and
- a prefix reset plus the replacing layer at the node for whole-object or whole-list replacement.

Untouched descendants retain their earlier source. A replaced subtree retains no attribution from
the discarded value; its descendants inherit the node source through longest-prefix lookup until a
later child merge records a narrower source. When a contribution first materializes such a narrower
path, it seeds the record from the longest existing prefix before adding the new layer, so an equal
list item does not erase the inherited contributor. `ProvenancePath` is the single path shape
carried by layer results and merge operations and consumed by reference ownership and validation
attribution. String segments address fields and mapping keys; integer segments address positions in
the final effective list, never authored item values. Every producer and consumer uses the same
longest-prefix lookup, so a parent replacement remains authoritative until a narrower surviving
value records another contributor. Validation error locations normalize to that shape; a non-string
raw key makes a merge-by-key mapping wrong-shaped and replaces it at its owning container rather
than leaking, hashing, or converting the key to text. Two invalid siblings inside one nested object
can therefore still name the layers that separately declared them.

Defaults originate in the seed and receive `defaulted` provenance. They are not represented as a
platform layer. The existing resolved dataclasses remain compatibility projections while consumers
move to the resolved-value-plus-provenance result.

Overlay payloads reuse each declaration slot's strict template spec model but exclude `name`,
`inherits`, metadata, and framework provenance. Their merge semantics match templates. In
particular, an empty default-merged object or append-deduplicated list changes nothing, while an
empty value on a `replace` node clears that complete value. This is schema policy rather than an
instance-only tombstone.

Session harness selection remains one structural transition rather than a field policy callback.
When two layers select the same registered integration, its config model directs recursive merging.
When a later layer selects a different integration, the runner resets the entire config subtree and
merges only under the new integration's model. Repeated declarations of the same unknown selector
instead replace the complete prior raw config, because no model exists through which to recurse;
Registry miss handling remains authoritative. The imperative capability `merge_config` hook and its
sentinel-based provenance inference disappear; capability authors use the same model annotations as
core declarations. `MergedHarness` retains neither a provenance map nor a parallel selector-owner
field. Removing that public hook increments the harness-integration capability contract from version
1 to version 2. Shipped integrations move with the framework, exact contract-version registration
makes version-1 third-party integrations fail clearly, and there is no compatibility bridge that
could preserve two merge authorities.

An effective-instance validator runs after the overlay fold. It reuses domain reference and
capability rules by publishing the owning live or pending resource, without publishing the overlay
as a fake registry template. Shape validation alone is insufficient because a validly shaped overlay
can still name a missing secret, recipe, credential, install command, or harness integration.

### Live and pending resource publication

The Registry keeps its original collect-then-finalize lifecycle. Built-ins, capability rows,
plugins, YAML manifests, and the state database are peer publishers into one mutable Registry;
`Registry.finalize()` still runs exactly once after collection and produces the one retained frozen
dependency graph. The Registry remains publisher-agnostic and does not open or interpret the
database itself. Bootstrap owns publisher order and a database publisher projects typed, value-free
live-resource rows from one read snapshot.

Here, **declared resource** means a registry resource supplied by built-in, plugin, YAML, or
auto-declaration machinery. **Live resource** means a database-backed VM, workspace, agent, session,
or console, independent of current power or process state. **Pending live resource** means the
candidate resource a creating command adds only to its prospective registry build. All are
first-class graph nodes; capability implementation instances remain held implementation objects, not
graph nodes.

A live resource publishes both the intrinsic relationships recorded by its row and the references
from its fully resolved current desired declaration: its selected template chain plus the persisted
final instance layer. Intrinsic relationships include workspace and agent ownership by a VM, session
ownership by a workspace and optional agent, and console membership. It does not publish provider
observations or claim that desired state has been applied. The graph node is derived rather than
separately persisted, just like a YAML-backed node: deleting or changing the durable source changes
the next build, and an auto-declared target disappears when no collected reference requires it. An
explicit declaration wins under the Registry's existing precedence rules.

Creation uses the same assembly path with one extra publisher for pending resources. The candidate
VM and admin declarations are one pending VM publication; compound session creation publishes each
candidate child and the session. Pending publication precedes database publication so a reinit
candidate can claim the live identity whose durable desired declaration it will replace; the
database publisher skips that identity while publishing every other row from the same snapshot.
Finalization validates references, performs normal miss handling, and synthesizes auto-declared
targets before any lifecycle mutation. A failed command discards the prospective registry. After
success, the database publisher reconstructs the equivalent live rows on the next command.

The database publisher reads and decodes only typed desired declarations and emits references
through each domain's existing `effective_references` function. It never scans raw JSON for names or
exposes plaintext environment values. A stored declaration that cannot be decoded must produce the
established typed unsupported-or-malformed state result; graph construction must not silently claim
completeness after dropping that owner.

Finalization derives the established JSON v1 `used_by` projection from this same published graph.
That compatibility view keeps its existing per-kind semantics, including session-oriented secret
reachability and null for kinds without a live-instance projection. Ordinary graph queries retain
the complete direct live edges. Neither view reads the database after finalization.

A durable row may outlive a selected template, site, owner, or member. Publication preserves that
live node for recovery but emits an intrinsic edge only when its target is present; it never invents
the target or falls back to a default. An absent selected declaration also means there is no honest
effective declaration to project for that slot. R5 reports the selection as unresolved. Pending
resources remain strict because a command must not create a new stranded row.

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
prove that identity was applied. R3 changes that helper to return a typed applied/unproven result
and threads the result through phase B.

The terminal database checkpoint groups:

- final init status;
- the terminal VM event; and
- only the applied slices whose typed outcomes prove success, plus removal of prior SSH evidence
  when the remote write outcome or final configured identity became uncertain.

The existing status and event methods become transaction-aware only where required for this
composition. Remote work cannot be atomic with SQLite. If the remote key write succeeds and the
database checkpoint fails, Agentworks retains conservative old or absent state and reinit is the
retry path; it never fabricates a successful record.

Before committing an SSH fingerprint, the lifecycle re-derives the configured identity and compares
it with the pre-application result. This detects ordinary path replacement during the operation but
does not claim filesystem and OpenSSH path access are atomic. Authorized-key reconciliation is the
last Phase B remote mutation. A failed write has an ambiguous remote outcome, so it removes prior
SSH evidence. A successful write followed by an unstable proof does the same in the terminal
transaction rather than allowing stale evidence to authorize later SSH.

Reinit performs an additional configured public/private identity check after its cheap declaration
and recipe checks but before the applied-state boundary, activation, secret resolution, or transport
construction. Authorized-key reconciliation deliberately validates again immediately before the
remote write. The first check prevents avoidable backend work; the second retains the time-of-check
protection when a configured path changes during the operation.

Operational checks use an explicit comparison service at SSH-using boundaries. They do not gate all
`LiveVMNode.preflight()` calls because start, stop, delete, and establishment reinit have different
transport needs. The ordinary gated VM boundary is safe by default; platform-native recovery uses an
explicitly named exception. Native cloud transport may itself use the configured SSH key; the
exception exists because rekey, Tailscale repair, platform shell, and delete cleanup must remain
attemptable, not because native routing proves a different identity. Reinit permits not-recorded
state so existing VMs can establish it. Cleanup remains possible. `verify_vm_connection` and
ordinary live-SSH compositions are the first consumers; direct transport call sites receive explicit
coverage rather than assuming one boundary owns every connection. Drift guidance names restoration
or recreation rather than implying native shell changes persisted evidence.

## R5: resolved and applied projections

Template `resource show` gains an optional typed resolution hook on the four inheriting kinds. The
generic show service projects resolved values and provenance without importing domain reducers.

Live VM, workspace, agent, and session describe services are the natural instance surfaces. They
combine:

- current template plus overlay resolution;
- the persisted desired overlay;
- row-backed applied hardware values plus applied slices from the same database snapshot; and
- structural comparison states: not recorded, unverifiable, match, or drift.

Registry assembly already publishes structural live facts from one read-only database transaction.
Inspection reads those facts only from the retained graph rather than opening a second structural
snapshot. Any overlay or applied-state projection added to `resource show` must use one explicit
request snapshot. Doctor reads applied SSH slices in one batch, derives the current configured
private identity once per run, and reports per-VM state without an N+1 query.

JSON v1 retains every existing field and adds optional tagged objects. Human and JSON forms project
the same structural facts. Resolved specs include configured secret references only, never resolved
secret values.

A missing or removed template is unresolved current declaration, not the built-in default. A later
edit to the selected VM template can change currently resolved hardware while the VM row still
describes provisioned hardware. The applied marker supplies operation and time proof without
duplicating those values. Its absence on a historic row remains visibly not recorded. VM reinit does
not provision hardware again and must not create or replace that marker merely because other
initialization steps succeeded.

Historic VMs receive no synthesized SSH evidence. Ordinary canonical SSH paths refuse them until one
successful reinit establishes the slice. An unproven final admin key write clears older SSH evidence
and immediately tells the operator that routine SSH is unavailable, to retry reinit only when the
installed key still works, and otherwise to attempt platform or provider recovery before
reinitializing or recreating the VM.

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
