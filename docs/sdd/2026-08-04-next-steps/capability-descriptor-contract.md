# Capability Descriptor Contract

- Status: Design-track artifact, draft for review
- Date: 2026-08-05
- Inputs: the capability and declarative-schema perspectives (`inputs/`), the declarative-schema
  SDD's phase 2 HLA and plan, `target-state.md`'s settled rulings, and code reconnaissance recorded
  in `starting-state.md`

## Purpose

This artifact settles the capability-kind descriptor contract (destination 3) far enough to release
declarative-schema phase 2 from its hold and seed wave 2 against it. It is a design decision record,
not an implementation plan: the wave 2 effort owns the plan and the code. Where this document says
"settled," a child SDD builds on it; where it says "open," the wave 2 seed carries the question.

## Constraints inherited (fixed input)

- Phase 2's proven contracts execute through this descriptor: model-as-authority, the two-walker
  split, the tagged union discriminated on `name` and assembled per kind at the existing
  post-registration boundary, the error-framing bridge as the single choke point, and validation on
  effective config keyed to the finalize fold.
- Secret backends are ordinary capabilities on the shared contract (operator mandate, 2026-08-05),
  via the backend/source split.
- The harness facet model (wave 4) will want one integration kind exposing several configuration
  surfaces, one per facet.
- The four open doors from `target-state.md` (source-agnostic extraction, layer-stack merge, graph
  immutability as a registry/fold property, one instance-state store) must not be closed.
- Capability kinds remain core-owned. Generalizing registration does not let plugins invent kinds.

## The descriptor

One frozen, typed, core-owned record per kind, registered in a single table that becomes the only
capability-kind enumeration in the codebase (`KIND_REGISTRY` and manifest decode's declarable-kind
tables legitimately enumerate all resource kinds and stay). Shape (illustrative, not final code):

The descriptor is minimal by rule: a field exists only when it has a wave 2 consumer. Because the
descriptor is a frozen record registered in one place, adding a field later is purely additive (no
registration-site migration), so deferral costs nothing and speculation buys nothing.

Day-one fields:

```text
CapabilityKindDescriptor
    kind                     "vm-platform" | "harness-integration" | ...
    implementation_contract  required base class or protocol
    registry_policy          classes or factories, registered by name; constructed instances only
                             as a descriptor-carried interim exception (secret-backend, until
                             wave 3)
    config_schema            schema slots (see below); each slot names a Pydantic model contract
    union_assembly           framework-built discriminated union per slot at the registration
                             boundary, cached on the kind's registry entry
    entry_factory            builds the kind's read-only resource row
    kind_strategy            the ResourceKind strategy object for KIND_REGISTRY
    readiness                uniform classmethod contract over config; instance-scoped readiness
                             belongs to constructed, bounded-lifetime clients
    publisher                derived; the skip-plugin-seated idiom implemented once
    manifest_section         how the kind appears in manifest decode: today the kind-sections
                             entry plus, for capability-hosting surfaces, the tagged-table field
                             mapping (the legacy sibling-field pairs die with tagged-shape
                             hardening)
```

Snapshot/restore needs no field at all: it iterates the descriptor table, so participation is
membership, and a flag would only add a way to be wrong.

Deferred fields, recorded here (and as comments in the implementation) with the trigger that creates
each, so wave 2 neither builds them early nor reinvents them later:

```text
    contract_version         when the first incompatible contract change or the wave 8 external
                             plugin API needs version negotiation; until then every first-party
                             implementation tracks HEAD and the field would hold one value forever
    consumer_gating          when gating derivation actually consolidates (the first new consuming
                             surface, waves 3 and 4); wave 2 changes no gating behavior
    migration_participation  only if wave 2 rules that `agw resource migrate` survives AND should
                             derive from the live descriptor; note the counterargument that the
                             migrator is deliberately an independent frozen oracle and may never
                             derive from live wiring
```

### Schema slots

The descriptor's config contract is a set of named schema slots rather than exactly one model per
kind. Every current kind declares a single default slot, so wave 2's per-kind modeling proceeds
exactly as its plan specifies. The slot mechanism exists for the facet case: the harness-integration
kind will declare one slot per facet, an implementation's support for a facet is the presence of a
model in that slot, and absence means unsupported (matching the facet rule that no-op defaults are
forbidden). Slot presence IS the support claim; there is no separate support flag that could
disagree with it. Wave 4 then adds facets without reshaping the descriptor.

This is the one deliberate deviation from phase 2's single-model framing, chosen because the cost
now is a naming layer, while retrofitting multi-model kinds after wave 2 would be a second migration
of every registration site.

## What derives from the descriptor

The switchboard collapses: every site that today independently enumerates the four kinds derives
from the descriptor table instead. From `starting-state.md`, that is the adapter table (one generic
adapter parameterized by descriptor replaces the four hand-written five-method classes), the graph's
kind set and readiness dispatch, the per-kind registry loaders, bootstrap publication, the
plugin-registry snapshot/restore tuple, and manifest decode's kind sections. The migrator's kind
participation flags stay hand-maintained unless the deferred `migration_participation` field is ever
created (see the deferred list above). The existing guard test flips its job from "detect an omitted
site" to "assert every site derives."

## What stays domain-owned

Domain operations and interfaces (launch, resume, provision, credential fill, secret lookup),
lifecycle stage semantics (pure declaration, cheap construction, read-only preflight, one secret
resolution pass, authenticated runup, mutating operations), per-kind row content, the domain logic
inside consumer gates, and the secret resolution algorithm itself. The descriptor wires kinds into
the framework; it does not absorb what makes each kind itself.

## Registration-time conformance

The descriptor makes "trust but verify" enforceable at registration, replacing the current
`type`-and-cast seam: conformance to `implementation_contract`, required metadata present, a
side-effect-free constructibility check, required operations implemented, and every provided slot
model conforming to the slot's model contract (presence is the support claim, so there is no
claimed-but-empty slot to check). Version negotiation joins this list when the deferred
`contract_version` field is created. Atomic seating (prepare everything, then mutate registries) is
preserved. This strengthens the internal extension framework; it does not create a public plugin
promise, which stays gated on wave 8's distribution-trust model.

## Secret backends under the descriptor

- The backend registry eventually stores classes or factories like every other kind, ending the
  constructed-singleton special case and the adapter and graph asymmetries that follow from it. That
  flip is wave 3's work, not descriptor adoption's: graph stamping and the resolve loop consume
  constructed instances today, so the flip must choose a construction point and touch the resolve
  machinery. Descriptor adoption therefore records the instance policy as an explicit,
  descriptor-carried interim exception on the secret-backend record, which wave 3 removes; the
  adoption itself stays mechanical.
- The backend's per-secret mapping model (phase 2's `mapping_model`) registers as the
  `secret-backend` kind's default schema slot in wave 2. Whether wave 3 re-homes it onto the source
  is wave 3's call.
- `secret-backend` is the implementation kind; `secret-source` is the declarable configured instance
  kind (the vm-site analog), carrying the per-source config model, references, and readiness.
  Zero-config backends get synthesized sources under their current names per `target-state.md`.
- Lifecycle layering (proposed decision): sources resolve in active-chain order, and in the first
  version a source's config MUST NOT reference secrets. Enforcement is structural, at registration
  conformance: the source config model may carry no secret-reference-annotated fields, so the rule
  cannot be violated by any operator config. This keeps resolution single-stage and the chain a
  simple order rather than a dependency graph. Interactive authentication (a vault prompting the
  operator) is source-client behavior, not a secret reference, so it stays legal. If a future
  backend genuinely needs secret-valued config, the chain order is promoted to an explicit
  resolution order then, as its own decision.
- Relocation into the `capabilities/` tree rides wave 3, and `_VMPlatformKind` moves in from
  `vms/kinds.py` during descriptor adoption for symmetry.

## Reconciliations with the phase 2 plan

- The phase 2 HLA folds tagged-shape hardening into the kind-model swap while the plan keeps them as
  separate ordered steps. Keep the plan's split: hardening lands first so the old-shape error
  survives the decoder swap, and wave 1's removals land before either, so the session-template model
  needs no legacy-selector shim (confirm at wave 2 seeding).
- Union assembly stays at the existing registration boundary; the lazier-plugin-load risk noted in
  the HLA transfers to wave 8 unchanged.
- The `agw resource schema` and describe surface names remain open in the phase 2 plan; the
  descriptor does not fix them. They are wave 2's call, coordinated with the onboarding child.

## Adoption path (wave 2 step zero)

Always-green and mechanical: introduce the descriptor table populated from the existing wiring, then
derive one site at a time (snapshot tuple, bootstrap publication, registry loaders, graph kind set
and readiness dispatch, adapters, decode sections, migrator flags), with the full gate passing after
each step. Phase 2's per-kind modeling then proceeds on top, registering models into slots.

## Open questions for the wave 2 seed

- The exact shape of the side-effect-free constructibility check.
- Naming for the slot vocabulary and whether the default slot is spelled at all in single-slot
  kinds.
- Whether the four entry dataclasses unify behind a generic entry or stay per-kind behind the
  `entry_factory`.
- How much of the phase-1 interim per-kind decode fork the descriptor adoption absorbs versus leaves
  to each kind's model swap.
- Whether consumer-gating declarations can fully replace the per-surface guard tests or only
  generate them.
- Which readiness shape the secret-source kind declares (the capability classmethod over config
  versus the consuming-resource hook). Wave 3's call, but the descriptor must record the choice.
