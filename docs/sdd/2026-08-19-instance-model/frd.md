# Instance Model and State: Functional Requirements

- Status: Seed FRD (saga lead authored; awaiting an effort lead)
- Date: 2026-08-19
- Parent: the `2026-08-04-next-steps` saga (destination 2 and the wave-4 enabling track)

## Rulings this seed rests on

- **Operator direction (2026-08-19):** four workstreams combine into one push: the database
  evaluation with a light repository layer, instance state (the fully resolved and applied config
  recorded per instance), instance-level spec overlays via the CLI, and CLI surfaces showing fully
  resolved specs for templates and live instances. One SDD carrying all four, phased, is the
  sanctioned shape. The effort lead may propose a split if the R1 assessment or the design work
  itself surfaces a reason the operator accepts; R4 is the likeliest candidate, since its near-term
  consumer is the thinnest, and the R1 database assessment cannot by itself speak to that.
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

### R3: Instance state, the applied record

For each provisioned instance (VM first, as the vertical slice; the store's contract must serve
sessions, workspaces, and agents without redesign), the system records the fully resolved
configuration that was actually applied: the effective spec after the whole inheritance stack and
platform defaults, as of the provisioning or reinit that applied it, with enough provenance to say
when and by what operation. The record is the store the wave-2 ruling named, designed once for its
three consumers (instance specs, integration applied-state, artifact ownership). This effort ships
the first consumer and documents the contract wave 4's integration applied-state builds on, in a
permanent home beside the store's implementation rather than inside this effort's SDD, since a later
wave must read a live doc and not spelunk a locked one. Artifact ownership (wave 6) has no stated
requirements anywhere yet, so the obligation there is only that the design must not preclude it: a
checkable constraint, never a deliverable, and the effort lead must not invent schema for it.
Applied state is readable through R5's surfaces and comparable against the currently declared spec;
drift is a reportable fact (doctor is the natural reporter). Remediation of drift (rotation,
re-apply) is out of scope; making it visible is in scope.

**The proving slice (operator direction, 2026-08-21):** record the operator SSH identity an instance
was actually provisioned with, so preflight can check it and fail cleanly when it is missing,
unreadable, or no longer the identity the instance trusts. Take this as R3's first vertical slice
rather than a later item: it exercises the whole arc (recorded at apply time, read at use time,
mismatch reported) on one concrete field with a consumer that exists today, and it answers the
hazard in the Why-now section directly. Record both the identity reference and the public key's
fingerprint. The reference catches a config edit pointing elsewhere and supports the
existence-and-readability check; the fingerprint catches regeneration at the same path, where the
reference is unchanged but every provisioned instance still trusts the old public key, which is the
quieter and more damaging case. A public-key fingerprint is not a secret, so persisting it costs
nothing in exposure. Failing cleanly means naming what the instance trusts against what the config
now presents, and what the operator can do about it; it does not mean re-applying anything.

### R4: Instance spec overlays via the CLI

An operator can attach a final configuration layer to a specific instance, applied after the
template chain, through a CLI verb (and correspondingly visible in the declarative model). The
overlay participates in the general layer-stack merge that wave 2's open door anticipated, never a
bespoke instance-only merge. Price this honestly: that general merge does not exist at HEAD.
`cli/agentworks/resources/inheritance.py` orders the template chain only, and the field-by-field
merge is implemented separately for each kind (vms, agents, workspaces, sessions), so participating
in a general merge means first unifying those implementations. If that generalization proves too
large for this effort, say so and route it rather than quietly adding a fifth per-kind merge; the
resolved result is what R3 records on apply and R5 shows on demand. Validation matches template
validation: an overlay that would produce an invalid effective spec fails at declaration time with
the same error quality templates get. One adjacent idea, recorded here so it is inheritable rather
than remembered: a template field could be marked as one an instance must set, so the template
declares the requirement and the overlay satisfies it. That is input to price during design, not a
requirement; it ships only if it falls out naturally.

### R5: Resolved-spec surfaces

The CLI can show the fully resolved spec for a template (what an instance created from it would get
after the inheritance chain resolves, with each value's source marked: declared, inherited,
defaulted, overlaid). Note for the effort lead, verified at HEAD: there is no separate
platform-defaults layer. Hardware defaults live once, on the resolved template
(`cli/agentworks/vms/templates.py`), and `cli/agentworks/capabilities/vm_platform/base.py` records
why a platform-side default was deliberately eliminated (a second declaration of the same value is
free to drift from the first). Do not reintroduce one; "defaulted" means the resolved template's own
default and for a live instance (both its current declared resolution and its applied record per R3,
with drift highlighted). The natural home is `resource show` per the focused-superset ruling on that
command; the effort lead proposes the exact surface in design. Machine output follows the JSON v1
discipline: existing fields preserved, additions optional and tagged.

## Acceptance

- Wave 4's applied-state slice can build on the store contract without reopening it (the contract
  document is reviewed by the saga lead before implementation of R3 lands).
- The onboarding field finding is discharged: an agent can state, before `vm create` mutates
  anything, the effective CPUs, memory, disk, and swap the instance will get, via a CLI surface.
  Today no command shows them: `agw resource show vm-template/NAME` renders the declared row only,
  so the resolved values first appear in provisioning output.
- The key-change hazard is visible: after an operator edits the configured identity, a doctor run or
  an R5 surface names the drift against each affected instance's applied record.
- The simple case does not get more verbose: an operator who never writes an overlay sees no new
  required ceremony.

## Out of scope

- A full ORM, or repository coverage beyond what the assessment justifies.
- Drift remediation (rotation operations, re-apply flows); wave 4 and later work own those.
- Integration applied-state and artifact ownership records themselves (the store contract must
  accommodate them; shipping them is wave 4 and wave 6 work).
- The living graph.

-- Seeded by the saga lead; the effort lead owns everything downstream of this FRD.
