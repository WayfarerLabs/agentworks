# Declarative Schema Perspective on Next Steps

- Status: Initial perspective
- Date: 2026-08-04
- Baseline: Agentworks 0.13.0 (`v0.13.0`)
- Source material: The in-flight declarative-schema effort
  (`docs/sdd/2026-07-31-declarative-schema/`), its FRD, HLA, plan, and the phase 1 TOML-sunset LLD

## Purpose

This document records the perspective of the declarative-schema effort itself: what that in-flight
work is proving, what it has already decided, and what it hands to the broader next-steps design. It
is a companion to the capability perspective's "Declarative Schema Direction" section, written from
the implementer's vantage rather than the reviewer's. It is an input to this SDD, not a functional
specification.

The declarative-schema effort is a two-phase, single-branch effort. Phase 1 removes the legacy TOML
resource-declaration path so phase 2 replaces one decode surface instead of two kept in lockstep.
Phase 2 moves every capability and resource kind onto registration-time Pydantic models from which
validation, reference extraction, JSON Schema emission, live-rendered samples, and a describe
surface are all derived. As of this baseline, phase 1's core has landed on the effort branch (the
TOML resource path hard-errors, the loaders are relocated into the migrator as an independent
verification oracle, and the fixture suite is mid-conversion under a deliberately bounded window);
phase 2 is fully specified but not yet implemented.

## Recommended Disposition: Complete Phase 1, Hold Phase 2

This is the load-bearing recommendation of this perspective, and the next-steps planning should
treat it as fixed input:

- **Phase 1 should be driven to completion and considered done, independent of this SDD.** A single
  declaration frontend is a precondition under every sequencing option the other perspectives
  propose: you cannot cleanly model per-kind config, consolidate a descriptor, or remove the 0.14
  compatibility shapes while two decode frontends are kept in lockstep. Phase 1 is therefore not a
  sequencing question; it is groundwork that is nearly finished (only the bounded fixture burndown
  remains) and should land green rather than sit paused. Next-steps design can assume the TOML
  resource path is gone.
- **Phase 1 merges to main on its own**, rather than waiting on the same branch as a held phase 2.
  Concretely: the declarative-schema effort's PR #316 is retargeted to a phase-1-only deliverable
  (the SDD artifacts plus the TOML sunset) and merged once the red window is closed and phase 1's
  records land (the superseding ADR, the lockfile entries, the breaking-change marker and operator
  upgrade note). This gets the precondition onto main and into a release instead of drifting on a
  long-lived branch, and it is the SDD skill's multi-branch model: the SDD lands unlocked (no
  `locked.md`), and phase 2 resumes on a later branch tracking the same plan. Its breaking change
  (config.toml no longer loads resource declarations) reaches operators through the normal release,
  behind the deprecation runway already shipped in 0.13.0.
- **Phase 2 should be HELD at the phase gate until this next-steps SDD settles.** Per-kind Pydantic
  modeling is exactly where the open ownership boundaries bite: modeling the legacy harness selector
  and then unwinding it in 0.14 is wasted work, and modeling each kind before the capability-kind
  descriptor exists bakes in the per-kind switchboard the capability perspective wants consolidated.
  Phase 2's decisions (the model-as-authority contract, the two-walker extraction, the tagged-union
  assembly) are proven and should be adopted, but they should be adopted THROUGH the descriptor and
  after the 0.14 removals, not ahead of them.

In short: phase 1 clears the ground now; phase 2's design is sound but its execution should be
resequenced into this SDD's ordering rather than run standalone.

## Executive Assessment

The declarative model is the right consolidation lever, and the effort has de-risked it enough to
recommend the sequence the capability perspective proposes. The parts that looked hardest in
principle (reference extraction over malformed config, a native tagged union for capability config,
validation timing against the finalize fold) have concrete, decided answers that hold up against the
shipped registry contracts. The parts that remain genuinely open are ownership boundaries the
broader next-steps work must settle: the capability-kind descriptor, the secret-source instance
layer, and the multi-scope harness facets. The recommended disposition (above) follows directly:
land phase 1 as a completed precondition, hold phase 2 at the phase gate, and adopt phase 2's proven
decisions through this SDD's descriptor and removal ordering rather than as a standalone run.

| Area                                            | Assessment                                       |
| ----------------------------------------------- | ------------------------------------------------ |
| Types-as-schema with derived surfaces           | Proven direction; correct lever                  |
| Native tagged union for capability config       | Decided and shipped in pre-support               |
| Reference extraction over malformed config      | Solved by the two-walker split                   |
| Validation timing against the finalize fold     | Solved; keys on the fold verdict                 |
| Inheritance vs dependency in the graph          | Solved by typing the edge, not removing it       |
| Single frontend (TOML sunset) as a precondition | In flight; unavoidable and worth the churn       |
| Capability-kind descriptor generalization       | Out of scope here; belongs to descriptor design  |
| Secret-source configured-instance layer         | Out of scope here; declarative model unblocks it |

## What This Effort Has Already Decided

These are settled decisions on the effort branch, recorded here because later next-steps design
should build on them rather than reopen them.

### The discriminator is `name`, and capability config is a real tagged union

The naming-field-plus-blob pair (`platform` beside `platform_config`, and its siblings) collapses
into one tagged table whose internal discriminator is `name`: `spec.platform: {name: lima, ...}`.
The tag is `name` because a capability is addressed `kind/name` everywhere in the resource model;
the hosting surface fixes the kind, so the tag supplies the other half. This lets Pydantic's native
discriminated-union machinery serve both runtime dispatch and exact JSON Schema emission, with no
sibling-field indirection. Pre-support for both shapes plus the tagged emission shipped in 0.13.0,
so phase 2's job here is hardening (old shape to a hard error, plus an in-place manifest-upgrade
mode), not introducing the shape.

The lesson for the descriptor work: a capability implementation carries its registered name as a
`Literal` tag, and the framework assembles one union per kind from the registered models at the
post-registration boundary. A kind descriptor that owns "the config schema contract" should own this
union assembly.

### Reference extraction and validation are two different walks

The `dependencies` and `validate` split the capability perspective flags as fragile is preserved in
substance but no longer asks authors to maintain two raw parsers. Both become framework walks over
one registered model:

- A total, never-raising reference extractor reads reference-typed fields straight from the raw
  blob, applies owner-templated defaults, and skips any field whose value is malformed. Graph
  construction never depends on a blob being valid.
- A field-documentation walk feeds the sample renderer, the describe surface, and schema emission
  from the same model, so those surfaces cannot disagree.

This is the concrete answer to the capability perspective's open question about reference extraction
over partially-malformed config: the extractor is a distinct, totality-preserving walk, not a second
validation pass. Reference-typed fields are carried as `Annotated` metadata (a secret reference with
an owner-templated default name, a resource reference to a fixed kind), which flows into emitted
JSON Schema without confusing validators.

### Validation keys on the finalize fold, and operates on effective config

Two timing decisions matter for anyone building on this:

- Hard validation follows the registry's shipped finalize order: dependencies extracted first
  (total, enablement-blind), the readiness and enablement fold computed without validating, then the
  throwing validate pass over the resources that emerge ready and enabled. A resource that emerges
  disabled or not-ready skips hard validation at load; its config problems bite at enable or use.
  This preserves the disabled-backend inertness the secret contract already relies on, as a
  consequence of the general rule rather than a special case.
- Validation runs on effective (merged) config, never on a partial declared blob. Where a surface
  inherits (session templates today), the declared blob may be legitimately incomplete, so the
  finalize pass resolves the inheritance chain and validates the merged result. Every other surface
  is a chain of length one, so the rule is uniform.

### Inheritance is not a runtime dependency

An inheritance edge is source composition: it drives existence checking, cycle detection, and merge
ordering, and is excluded from runtime-need traversal (the secret resolve union, resolvability
prediction, dependency listings). A child that overrides a parent's default secret name depends on
the override alone; the parent's own edge to its default secret describes the parent's standalone
use and never attributes to the child. This is a typing decision on the existing graph, not a new
graph. The alternative of pulling inheritance out of the graph was considered and rejected: the
graph is exactly where inheritance's structural needs are already solved once and visible to
`doctor`.

## What Phase 1 Is Teaching

Phase 1 is mostly deletion, but it has surfaced two things worth carrying forward.

### A single declaration frontend is a real precondition, and its cost is the test suite

Removing the TOML resource path is not cosmetic. The decode layer's whole shape existed to keep the
TOML and manifest frontends in lockstep, and that lockstep is what made every schema-touching change
pay twice. Phase 2's clean per-kind model swap is only affordable because phase 1 first reduces the
problem to one frontend.

The concrete cost landed where it always does with a hard cutover: the test suite. Roughly seventy
fixtures declared resources in TOML because that was the supported path, and the load-time hard
error breaks all of them at once. That conversion is unavoidable, not optional, and it is being done
under an explicit bounded-red window with an enumerated inventory rather than pretended away. Any
future "remove the compatibility surface" step (the 0.14 harness and session removals the capability
perspective sequences first, for one) should budget the same fixture-conversion reality up front,
and should prefer a shared fixture helper so the conversion is uniform rather than per-file.

### The migrator's verification wants an independent oracle

Once TOML rows stop loading, the migrator's registry-equivalence check loses its pre-side. The
resolution is to relocate the old TOML loaders into the migrator as its private, frozen reader: the
pre-side reads the flat TOML shape while the post-side reads the emitted tagged YAML through the
manifest decoders, so the emission mapping under test is never on both sides. This is a general
pattern for any migration that transforms shape: verification is only meaningful when the two sides
derive independently. The next-steps migrations (secret-source, harness facets) will each want a
comparable oracle rather than a round-trip through the code under test.

## What This Effort Deliberately Does Not Do

The effort is scoped to the four current capability-hosting surfaces and the resource kinds. It does
not, and should not, attempt the generalizations the capability perspective identifies. Recording
the boundaries so they are not assumed complete:

- No capability-kind descriptor. This effort still touches the per-kind switchboard the capability
  perspective wants consolidated; it makes each kind's config a model, but it does not unify
  registration, graph stamping, and dispatch behind one descriptor. It does, however, make that
  consolidation cheaper by giving every kind a uniform config-schema contract to hang on the
  descriptor.
- No secret-source instance layer. Secret backend mappings become models like every other capability
  config, but the effort keeps the current single-level backend model. The two-level
  `secret-backend` / `secret-source` split is a separate design; the declarative model is a
  precondition for it, not a substitute.
- No multi-scope harness facets. The effort models today's session-bound harness-integration config.
  The facet model (machine, admin, agent, workspace, session) is the harness design phase's to
  specify; this effort should avoid encoding the session-only shape in a way that later has to be
  unwound, which is another reason to land the 0.14 removals before phase 2's harness modeling.
- No external plugin contract. The models improve registration-time conformance for config schema,
  but the effort makes no promise about a stable third-party API; that remains gated on the
  separately designed distribution-trust and enablement model.

## Interfaces With the Broader Next Steps

- The capability-kind descriptor should treat "the config schema contract" as one of its owned
  fields, and inherit this effort's model-as-authority decision, the two-walker extraction split,
  and the tagged-union assembly.
- The secret-source layer should expect per-secret config to be a model with the same
  reference-field metadata, and should carry the resolution-API evolution the capability perspective
  lists (typed outcomes, categorized failures, cleanup) rather than the current omitted-entry
  convention.
- The harness facet model should expect each facet to declare a Pydantic config model and merge
  policy, which is exactly the shape this effort proves out for the single session facet.
- The 0.14 compatibility removals should precede phase 2's per-kind modeling wherever a deprecated
  shape would otherwise be modeled and then immediately unwound (the legacy harness selector is the
  clear case).

## Questions for the Remaining SDD Artifacts

- Should the JSON Schema this effort emits become the interchange the descriptor work and the plugin
  SDK both consume, or is it an operator-and-editor artifact only?
- What is the smallest reference-field metadata vocabulary that serves secrets, resource references,
  and the future facet and secret-source references without a second redesign?
- Does the effective-config validation rule generalize cleanly to any future inheriting surface, or
  is session-template inheritance special enough to keep the rule scoped?
- How much of the per-kind decode fork this effort introduces as an interim state should the
  descriptor work absorb versus leave to each kind's model?
- When the secret-source and harness-facet layers add their own config surfaces, do they reuse this
  effort's error-framing bridge directly, or does the bridge need to become a shared service first?
