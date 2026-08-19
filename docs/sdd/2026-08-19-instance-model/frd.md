# Instance Model and State: Functional Requirements

- Status: Seed FRD (saga lead authored; awaiting an effort lead)
- Date: 2026-08-19
- Parent: the `2026-08-04-next-steps` saga (destination 2 and the wave-4 enabling track)

## Rulings this seed rests on

- **Operator direction (2026-08-19):** four workstreams combine into one push: the database
  evaluation with a light repository layer, instance state (the fully resolved and applied config
  recorded per instance), instance-level spec overlays via the CLI, and CLI surfaces showing fully
  resolved specs for templates and live instances. One SDD carrying all four, phased, is the
  sanctioned shape; the effort lead may propose a split only if the assessment phase surfaces a
  reason the operator accepts.
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
the saga ledger and this effort discharges it. The operator's key-change finding (2026-08-18:
editing the configured SSH identity silently orphans provisioned VMs) is the motivating example for
applied state: drift between declared and applied identity must become a detectable fact rather than
a silent hazard.

## Requirements

### R1: Database assessment before commitment

An assessment artifact describing the current persistence estate as it actually is: every table, who
reads and writes it, the query shapes in use, the concurrency and migration story, and the concrete
pain points (with `file:line` evidence, not impressions). The assessment names what the other three
workstreams need from storage and recommends the smallest repository shape that serves them. It is
reviewed before any storage code changes.

### R2: A light repository layer, sized by its consumers

Data access for the surfaces this effort touches goes through a typed repository layer: no raw SQL
scattered at call sites, no full ORM. The layer's scope is what R3 through R5 need plus the tables
the assessment shows are already painful; migrating every existing call site is explicitly in scope
only if the assessment shows it is cheap, otherwise the boundary is recorded and the remainder is
follow-up work with an owner. Schema migrations follow the existing migration mechanics; the
safer-migrations posture (pre-migration notice, backup, restore) applies to any schema change that
touches operator state.

### R3: Instance state, the applied record

For each provisioned instance (VM first, as the vertical slice; the store's contract must serve
sessions, workspaces, and agents without redesign), the system records the fully resolved
configuration that was actually applied: the effective spec after the whole inheritance stack and
platform defaults, as of the provisioning or reinit that applied it, with enough provenance to say
when and by what operation. The record is the store the wave-2 ruling named, designed once for its
three consumers (instance specs, integration applied-state, artifact ownership); this effort ships
the first consumer and must document the contract the other two build on. Applied state is readable
through R5's surfaces and comparable against the currently declared spec; drift is a reportable fact
(doctor is the natural reporter). Remediation of drift (rotation, re-apply) is out of scope; making
it visible is in scope.

### R4: Instance spec overlays via the CLI

An operator can attach a final configuration layer to a specific instance, applied after the
template chain, through a CLI verb (and correspondingly visible in the declarative model). The
overlay participates in the general layer-stack merge (the open door), never a bespoke instance-only
merge; the resolved result is what R3 records on apply and R5 shows on demand. Validation matches
template validation: an overlay that would produce an invalid effective spec fails at declaration
time with the same error quality templates get. The parked instance-required-fields idea (template
fields flaggable as must-set-at-instance) is input for the effort lead to price during design; it
ships only if it falls out naturally.

### R5: Resolved-spec surfaces

The CLI can show the fully resolved spec for a template (what an instance created from it would get,
after inheritance and platform defaults, with each value's source marked: declared, inherited,
defaulted, overlaid) and for a live instance (both its current declared resolution and its applied
record per R3, with drift highlighted). The natural home is `resource show` per the focused-superset
ruling on that command; the effort lead proposes the exact surface in design. Machine output follows
the JSON v1 discipline: existing fields preserved, additions optional and tagged.

## Acceptance

- Wave 4's applied-state slice can build on the store contract without reopening it (the contract
  document is reviewed by the saga lead before implementation of R3 lands).
- The onboarding field finding is discharged: an agent can state, before `vm create` mutates
  anything, the effective CPUs, memory, disk, and swap the instance will get, via a CLI surface.
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
