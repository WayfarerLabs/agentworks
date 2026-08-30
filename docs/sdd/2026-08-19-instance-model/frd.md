# Instance Model and State: Functional Requirements

- Status: Active (R1-R4 and merge strategy merged; R5 implemented, final verification pending)
- Date: 2026-08-19
- Last revised: 2026-08-30
- Parent: the `2026-08-04-next-steps` saga (destination 2 and the wave-4 enabling track)

## Rulings this seed rests on

- **Operator direction (2026-08-19):** four workstreams combine into one push: the database
  evaluation with a light repository layer, instance state (fully resolved configuration-snapshot
  slices recorded per successful lifecycle operation, including outcome evidence where required),
  instance-level spec overlays via the CLI, and CLI surfaces showing fully resolved specs for
  templates and live instances. One SDD carrying all four, phased, is the sanctioned shape. The
  effort lead may propose a split if the R1 assessment or the design work itself surfaces a reason
  the operator accepts; R4 is the likeliest candidate, since its near-term consumer is the thinnest,
  and the R1 database assessment cannot by itself speak to that.
- **Operator lean (2026-08-08, reaffirmed 2026-08-19): assessment first, and "something in that
  direction, not full ORM."** The database work begins by describing what exists and what hurts, and
  the repository layer is judged by the queries the other three workstreams actually need, never by
  generality.
- **The four open doors (wave 2 ruling, in the saga's `target-state.md`):** source-agnostic
  reference extraction, a general layer-stack merge rather than a template-only chain, graph
  post-finalize immutability as a registry/fold property, and one instance-state store designed once
  for instance specs, integration applied-state, and artifact ownership records. This effort walks
  through those doors; it must not re-litigate them.
- **Use the database, not its sidecars (operator, 2026-08-12):** state reads go through the database
  with the concurrency semantics it already provides.
- **Database rows publish live resources (authenticated operator channel, 2026-08-26):** VMs,
  workspaces, agents, sessions, and consoles in the database are resources of the live type,
  independent of runtime liveness. The database publishes them during the ordinary Registry
  collection phase before finalization; creating commands temporarily publish pending resources
  through the same boundary.
- **Existing lifecycle evidence remains unknown (authenticated operator channel to the
  instance-model effort lead, 2026-08-21; reaffirmed 2026-08-23):** no migration reconstructs it
  from current declarations. A real lifecycle operation establishes only the configuration-snapshot
  slices warranted by its successful outcome; a slice can require additional evidence that the
  relevant work succeeded. Workspace repair is not full idempotent convergence, so it cannot
  manufacture a complete workspace lifecycle record. Unknown is visible and distinct from both match
  and drift.
- **Password-protected SSH keys must not regress (authenticated operator channel to the
  instance-model effort lead, 2026-08-21; clarified 2026-08-23):** password protection is neither a
  mismatch nor an unsupported condition. Derive a fingerprint without unlocking when the private key
  format exposes its public identity; otherwise report that the identity is unverifiable. Never
  substitute an adjacent public-key file for the private identity. How an ssh-agent-held identity
  participates remains deliberately unresolved.
- **Instance specs follow template-setting lifecycle boundaries (authenticated operator channel to
  the instance-model effort lead, 2026-08-24):** "Workspaces do not support idempotent reinit so
  repair should not have --spec. And I don't think we should support resume --spec (yet) either.
  There are a lot of sharp edges there. Basically, we should support --spec _exactly_ where you can
  set/change the template. It's the same deal, right?"
- **VM creation has two template-setting boundaries (authenticated operator channel to the
  instance-model effort lead, 2026-08-26):** the unprefixed `--template` and `--spec` pair selects
  and refines the VM declaration; `--admin-template` and `--admin-spec` do the same for the VM's
  admin declaration. Do not add `--vm-template` or `--vm-spec`. Both pairs form one lifecycle
  decision and are persisted atomically for later reinit.
- **Merge policy belongs to the declared model (authenticated operator channel to the instance-model
  effort lead, 2026-08-27):** template inheritance and final instance layers use one schema-directed
  merge contract across core and capability-owned models. Objects merge by key by default and
  recursively consult each conflicting child's schema. A model or field at any object depth can
  select an alternative strategy; `replace` discards the complete previous subtree, so none of its
  child strategies participate in that layer. Lists retain stable append-deduplication by default
  and can select replacement. Scalars replace by default.
- **Lifecycle evidence records the configuration associated with success (authenticated operator
  channel to the instance-model effort lead, 2026-08-30):** a persisted lifecycle record is the
  configuration snapshot used by a successful lifecycle operation. It describes what Agentworks
  expected that operation to create or configure. It does not claim that Agentworks independently
  observed the provider's realized state or detect provider-side normalization or inconsistency. For
  VM hardware, the public fact is `hardware-request`: the successful create request, compared as
  recorded request versus current declaration. The private store discriminator and key remain
  `applied-state` and `hardware-provenance` without a migration. SSH identity remains independently
  evidenced by the successful authorized-key write.

## Why now

Wave 4 (the harness scope framework) consumes the instance-state store for its applied-state slice;
settling the store's contract unblocks it. Field evidence from the 0.14.0 onboarding run
(`../2026-08-04-next-steps/message-2026-08-18-agentic-onboarding-run.md`) showed an agent cannot
state a mutation's infrastructure effect before making it, because the effective spec after
`inherits` composition and platform defaults appears in no CLI surface; that finding is routed in
the saga ledger and this effort discharges it. The operator raised a second motivating case
(2026-08-18) and the saga lead verified it at HEAD rather than relying on the report: VM
provisioning writes `config.operator.ssh_public_key` into the instance's `authorized_keys`
(`cli/agentworks/vms/initializer/ssh_keys.py`), while every later connection presents
`config.operator.ssh_private_key` (`cli/agentworks/transports/__init__.py`). Editing the configured
identity therefore leaves already-provisioned VMs holding the old public key while the transport
offers the new private one, and nothing reports the mismatch until a connection fails. Applied state
is what makes that drift a detectable fact instead of a silent hazard.

## Requirements

### R1: Database assessment before commitment

An assessment artifact describing the current persistence estate as it actually is: every table, who
reads and writes it, the query shapes in use, the concurrency and migration story, and the concrete
pain points (with `file:line` evidence, not impressions). The assessment names what the other three
workstreams need from storage and recommends the smallest repository shape that serves them. The
saga lead reviews it before any storage code changes.

### R2: A light repository layer, sized by its consumers

Data access for the surfaces this effort touches goes through a typed repository layer: no raw SQL
scattered at call sites, no full ORM. The layer's scope is what R3 through R5 need plus the tables
the assessment shows are already painful; migrating every existing call site is explicitly in scope
only if the assessment shows it is cheap, otherwise the boundary is recorded and the remainder is
follow-up work with an owner. Schema migrations follow the existing migration mechanics; the
safer-migrations posture (pre-migration notice, backup, restore) applies to any schema change that
touches operator state. This effort owns the test-estate trim for the surfaces it rewrites (the
simplification sweep's re-scope, operator direction 2026-08-19, recorded in that effort's
`message-2026-08-19-sweep-rescope.md`): a rewrite here leaves its area's tests trimmed to the
sweep's standard, and the plan carries that as definition-of-done, not follow-up.

### R3: Instance state and lifecycle evidence

For each provisioned instance (VM first, as the vertical slice; the store's contract must serve
sessions, workspaces, and agents without redesign), the system records slices of the fully resolved
configuration used by successful lifecycle operations: the effective spec after the whole
inheritance stack and model defaults, as of the operation that used it, with enough provenance to
say when and by what operation. This lifecycle evidence describes the configuration Agentworks
expected to create or apply successfully. A slice may require independent evidence that its
corresponding work succeeded, as SSH identity does. It does not claim that Agentworks observed
provider-realized hardware or detect provider-side normalization or inconsistency. The record is the
store the wave-2 ruling named, designed once for its three consumers (instance specs, integration
applied-state, artifact ownership). This effort ships the first consumer and documents the contract
wave 4's integration applied-state builds on, in a permanent home beside the store's implementation
rather than inside this effort's SDD, since a later wave must read a live doc and not spelunk a
locked one. Artifact ownership (wave 6) has no stated requirements anywhere yet, so the obligation
there is only that the design must not preclude it: a checkable constraint, never a deliverable, and
the effort lead must not invent schema for it. Lifecycle evidence is readable through R5's surfaces
and comparable against the currently declared spec; drift is a reportable fact (doctor is the
natural reporter). Remediation of drift (rotation, re-apply) is out of scope; making it visible is
in scope.

Under the existing-state ruling above, existing lifecycle evidence stays unknown until a real
lifecycle operation, such as reinit or resume, records the applicable successful configuration
snapshot. A missing record is a first-class outcome for comparisons and diagnostics, rendered as not
recorded rather than omitted. It is not drift, a match, or an implicit pass.

**The proving slice (operator direction, 2026-08-21):** record the operator SSH identity an instance
was actually provisioned with, so preflight can check it and fail cleanly when it is missing,
unreadable, or no longer the identity the instance trusts. Take this as R3's first vertical slice
rather than a later item: it exercises the whole arc (recorded at apply time, read at use time,
mismatch reported) on one concrete field with a consumer that exists today, and it answers the
hazard in the Why-now section directly. Record the fingerprint of the identity the transport will
actually present, derived from the private key at the use boundary, not read from the configured
public key file. That distinction is the whole safety property: the transport authenticates with
`operator.ssh_private_key`, while today's config and doctor checks only test that the public and
private files each exist and are readable, independently of each other. So a private key replaced at
the same path leaves a path reference unchanged, leaves a public-key file unchanged, and still
breaks the next connection. Only a fingerprint derived from the private key can catch it. The
configured reference stays worth recording as diagnostic context (it names what the operator pointed
at, which helps the message say something useful), but it is not the check. A public key fingerprint
is not a secret, so persisting it costs nothing in exposure.

Password-protected SSH private keys remain supported. The implementation must derive the
authoritative public identity from the configured private identity without unlocking wherever the
format permits, including password-protected OpenSSH keys, and must never use an adjacent public-key
file as evidence. Where an encrypted format does not expose its public identity without unlocking,
preflight says it cannot verify rather than implying a match or treating password protection as
unsupported. Failing cleanly means naming what the instance trusts against what the config now
presents, and what the operator can do about it; it does not mean re-applying anything. How an
ssh-agent-held identity participates remains deliberately unresolved.

### R4: Instance spec overlays via the CLI

An operator can supply a final configuration layer alongside a CLI operation that selects or changes
an instance's template. At the current surface, that means the four direct creation commands and
`agent reinit`, the sole existing-instance command that can repoint its owner to another template.
VM creation selects both a VM template and an admin template, so it accepts separate final VM and
admin layers as `--spec` and `--admin-spec`. Either layer may accompany an explicit template
selector or the corresponding default. The two template selections and two final layers are one
candidate VM declaration and one atomic desired-state decision. There is no independent
instance-spec mutation verb. VM reinit cannot change the VM template, workspace repair is not full
idempotent convergence, and session resume has unresolved sharp edges, so none accepts an instance
spec. An empty JSON object or the exact empty CLI value clears an agent's prior layer when passed to
`agent reinit`; omitting the option retains it, and whitespace-only input is invalid.

The overlay is applied after the template chain and is correspondingly visible in the declarative
model. It participates in the shared layer stack introduced by R4, never a bespoke instance-only
merge. The shared runner owns ordering and provenance, and the typed model tree owns field policy
through one closed merge-strategy vocabulary. The same schema walker applies to core declaration
models and capability-owned config models that participate in layered merging, at arbitrary nesting
depth; a capability does not receive an imperative merge callback.

Default object merge preserves non-conflicting keys and recursively merges conflicts. A `replace`
strategy on an object replaces the whole subtree, including with an empty object. A replaced list,
including an empty list, replaces the complete prior list; an unmarked list keeps stable
append-deduplication. A `replace` strategy on a field containing a discriminated or structural union
wins before arm selection. Otherwise, the union recurses only while both values select the same arm:
an explicit containing-field `merge` wins there, followed by the selected arm model's policy and the
object default. Different arms, or arms that cannot be selected, replace the complete union value
even under a containing `merge`, preventing a composite across arms. The environment-table mapping
value declares replacement at each per-key conflict, so same-arm and cross-arm environment conflicts
retain today's whole-entry replacement without attaching mapping-only policy to a scalar-shorthand
arm. An unknown key within a known schema is preserved; when two layers supply that same unknown
key, the later raw value replaces the earlier one rather than creating a second runtime-shape merge
language. A wholly unknown harness integration has no schema or usable effective config, so its
later raw config replaces its complete prior config and the Registry reports the selector miss. The
engine preserves malformed input for the existing final typed validation boundary rather than
filtering, coercing, or laundering it, and it must terminate on a cyclic Python value reaching the
deliberately open capability config boundary, including through a YAML alias.

Model-directed merging replaces the public `HarnessIntegration.merge_config` customization point.
That is an intentional hard cutover of the harness-integration capability contract from version 1 to
version 2: shipped integrations move to annotations, version-1 third-party integrations fail clearly
at registration, and no compatibility bridge or second merge authority remains.

The resolved result feeds the configuration-snapshot slices that successful lifecycle operations
record and R5 shows on demand. Validation matches template validation: an overlay that would produce
an invalid effective spec fails at declaration time with the same error quality templates get. One
adjacent idea, recorded here so it is inheritable rather than remembered: a template field could be
marked as one an instance must set, so the template declares the requirement and the overlay
satisfies it. That is input to price during design, not a requirement; it ships only if it falls out
naturally.

Database-backed VMs, workspaces, agents, sessions, and consoles are live resources whether or not
they are currently running. The state database is another resource publisher: ordinary registry
assembly collects those live resources and the references from their fully resolved desired
declarations before the one existing finalization pass. A reference introduced only by a persisted
instance spec therefore participates in normal miss handling and auto-declaration. In particular, a
secret introduced by a live resource appears on the ordinary secret inspection surfaces for as long
as at least one collected resource references it.

A creation command applies the same rule prospectively. Its candidate effective declarations publish
pending live resources into that command's otherwise ordinary pre-finalize collection. Failure
leaves no durable publication; success persists the live resource and its desired state, so later
commands reconstruct the same references from the database. This is graph reconstruction from
durable publishers, not post-finalize graph mutation, and "live" means database-backed rather than
currently active.

Existing database rows can be stranded after a template, site, owner, or member is removed. Registry
construction must retain those live nodes without inventing or defaulting the missing target, and
must leave recovery/list/delete paths usable. The later resolved-spec surface reports a missing
selection as unresolved. Prospective resources receive no such exception: their selected targets
must exist before mutation.

### R5: Resolved-spec surfaces

The CLI can show the fully resolved spec for a template: what Agentworks would request for an
instance created from it after the inheritance chain resolves, with each value's source marked as
declared, inherited, defaulted, or overlaid. Note for the effort lead, verified at HEAD: there is no
separate platform-defaults layer. Hardware defaults live once, on the resolved template
(`cli/agentworks/vms/templates.py`), and `cli/agentworks/capabilities/vm_platform/base.py` records
why a platform-side default was deliberately eliminated (a second declaration of the same value is
free to drift from the first). Do not reintroduce one; "defaulted" means the resolved template's own
default. For a live instance, the CLI shows both its current declared resolution and its lifecycle
evidence per R3, with drift highlighted. The natural home is `resource show` per the
focused-superset ruling on that command; the effort lead proposes the exact surface in design.
Machine output follows the JSON v1 discipline: existing fields preserved, additions tagged.

## Acceptance

- Wave 4's applied-state slice can build on the store contract without reopening it (the contract
  document is reviewed by the saga lead before implementation of R3 lands).
- The onboarding field finding is discharged: an agent can state, before `vm create` mutates
  anything, the effective CPUs, memory, disk, and swap Agentworks will request, via a CLI surface.
  Today no command shows them: `agw resource show vm-template/NAME` renders the declared row only,
  so the resolved values first appear in provisioning output.
- The key-change hazard is visible: after an operator edits the configured identity, a doctor run or
  an R5 surface names the drift against each affected instance's recorded lifecycle evidence.
- At every supported template-setting lifecycle boundary, an operator can supply the final inline
  instance layer and can tell from the command result whether that layer was set, retained,
  replaced, cleared, or explicitly absent, without the CLI echoing its values.
- A resource referenced only by the fully resolved desired declaration of a database-backed live
  resource is present on the same list, describe, verify, doctor, and graph surfaces as one
  referenced by a YAML declaration. Candidate references participate during creation without
  surviving a failed command.
- Template inheritance and final instance overlays follow the same model-declared merge policy for
  core and capability config: nested objects recurse by default, an object or list marked `replace`
  discards its complete prior value, union-arm changes cannot create hybrids, and provenance
  identifies the surviving leaf or replaced subtree without attributing discarded children. List
  provenance identifies positions in the final effective list, never authored item values.
- The simple case does not get more verbose: an operator who never writes an overlay sees no new
  required ceremony.

## Out of scope

- A full ORM, or repository coverage beyond what the assessment justifies.
- Drift remediation (rotation operations, re-apply flows); wave 4 and later work own those.
- Integration applied-state and artifact ownership records themselves (the store contract must
  accommodate them; shipping them is wave 4 and wave 6 work).
- Incremental mutation of a finalized registry or graph, and provider-observed runtime liveness.
  Registry construction remains collect-then-finalize and derives live resources from durable
  database state.
- Provider-observed or realized VM hardware, including detection of provider-side normalization or
  inconsistency with the successful provisioning request.

-- Seeded by the saga lead; the effort lead owns everything downstream of this FRD.
