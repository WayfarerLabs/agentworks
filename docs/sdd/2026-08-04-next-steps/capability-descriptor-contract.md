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
kind enumeration in the codebase. Shape (illustrative, not final code):

```text
CapabilityKindDescriptor
    kind                     "vm-platform" | "harness-integration" | ...
    contract_version         integer the implementation declares compatibility with
    implementation_contract  required base class or protocol
    registry_policy          classes or factories, registered by name; never constructed instances
    config_schema            schema slots (see below); each slot names a Pydantic model contract
    union_assembly           framework-built discriminated union per slot at the registration
                             boundary, cached on the kind's registry entry
    entry_factory            builds the kind's read-only resource row
    kind_strategy            the ResourceKind strategy object for KIND_REGISTRY
    readiness                uniform classmethod contract over config; instance-scoped readiness
                             belongs to constructed, bounded-lifetime clients
    publisher                derived; the skip-plugin-seated idiom implemented once
    consumer_gating          which consuming surfaces gate on enablement, declared not hand-wired
    manifest_section         the kind's manifest section mapping (decode derives from this)
    migration_participation  whether `agw resource migrate` handles the kind
    snapshot_participation   derived; plugin-registry snapshot/restore iterates descriptors
```

### Schema slots

The descriptor's config contract is a set of named schema slots rather than exactly one model per
kind. Every current kind declares a single default slot, so wave 2's per-kind modeling proceeds
exactly as its plan specifies. The slot mechanism exists for the facet case: the harness-integration
kind will declare one slot per facet, an implementation's support for a facet is the presence of a
model in that slot, and absence means unsupported (matching the facet rule that no-op defaults are
forbidden). Wave 4 then adds facets without reshaping the descriptor.

This is the one deliberate deviation from phase 2's single-model framing, chosen because the cost
now is a naming layer, while retrofitting multi-model kinds after wave 2 would be a second migration
of every registration site.

## What derives from the descriptor

The switchboard collapses: every site that today independently enumerates the four kinds derives
from the descriptor table instead. From `starting-state.md`, that is the adapter table (one generic
adapter parameterized by descriptor replaces the four hand-written five-method classes), the graph's
kind set and readiness dispatch, the per-kind registry loaders, bootstrap publication, the
plugin-registry snapshot/restore tuple, and manifest decode's kind sections, plus the migrator's
kind participation flags. The existing guard test flips its job from "detect an omitted site" to
"assert every site derives."

## What stays domain-owned

Domain operations and interfaces (launch, resume, provision, credential fill, secret lookup),
lifecycle stage semantics (pure declaration, cheap construction, read-only preflight, one secret
resolution pass, authenticated runup, mutating operations), per-kind row content, the domain logic
inside consumer gates, and the secret resolution algorithm itself. The descriptor wires kinds into
the framework; it does not absorb what makes each kind itself.

## Registration-time conformance

The descriptor makes "trust but verify" enforceable at registration, replacing the current
`type`-and-cast seam: conformance to `implementation_contract`, required metadata present, a
side-effect-free constructibility check, required operations implemented, a model present for every
slot the implementation claims, and `contract_version` compatibility. Atomic seating (prepare
everything, then mutate registries) is preserved. This strengthens the internal extension framework;
it does not create a public plugin promise, which stays gated on wave 8's distribution-trust model.

## Secret backends under the descriptor

- The backend registry stores classes or factories like every other kind, ending the
  constructed-singleton special case and the adapter and graph asymmetries that follow from it.
- `secret-backend` is the implementation kind; `secret-source` is the declarable configured instance
  kind (the vm-site analog), carrying the per-source config model, references, and readiness.
  Zero-config backends get synthesized sources under their current names per `target-state.md`.
- Lifecycle layering (proposed decision): sources resolve in active-chain order, and in the first
  version a source's config MUST NOT reference secrets; that is a hard validation error. This keeps
  resolution single-stage and the chain a simple order rather than a dependency graph. Interactive
  authentication (a vault prompting the operator) is source-client behavior, not a secret reference,
  so it stays legal. If a future backend genuinely needs secret-valued config, the chain order is
  promoted to an explicit resolution order then, as its own decision.
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
