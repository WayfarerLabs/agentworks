# Wave 2 Seed Notes

- Status: Published for the wave 2 effort lead
- Date: 2026-08-05
- Audience: the effort lead picking up `docs/sdd/2026-07-31-declarative-schema/` phase 2
- Authority: `target-state.md` and `capability-descriptor-contract.md` in this directory

This is the roadmap's seed for an adopted child. The declarative-schema SDD predates the roadmap and
is not the roadmap lead's to edit, so the changes phase 2 needs are recorded here for you to
integrate into your own artifacts (which you own, per the sdd skill's ownership rule). Update your
plan and HLA as living documents; completed checkboxes stay immutable, so integrate by adding and
annotating, not rewriting history. Flag anything here that contradicts what you find to the
operator.

## 1. Release the hold

Your plan holds phase 2 "until the next-steps SDD settles the capability-kind descriptor and the
0.14 compatibility-removal ordering." Both are settled: the descriptor contract merged via PR #405
(`capability-descriptor-contract.md`) and the 0.14 removals landed via PR #406 (the
`2026-08-05-deprecation-removal` SDD, locked). Your first commit should record the hold release in
your plan, citing both.

## 2. Insert descriptor adoption as step zero

Phase 2 executes through the descriptor. Before your 2.1 schema foundation, add the adoption step
from the contract's "Adoption path": introduce the descriptor table populated from existing wiring,
then derive one site at a time (snapshot tuple, bootstrap publication, registry loaders, graph kind
set and readiness dispatch, adapters, decode sections), full gate green after each step. The
migrator's participation flags stay hand-maintained (deferred field; see the contract). The existing
guard test flips to asserting derivation.

## 3. Adjust per-kind modeling to schema slots

The one deliberate deviation from your single-model framing: config models register into named
schema slots, with every current kind using a single default slot, so your 2.3 and 2.5 proceed as
planned under a slot-shaped registration API. Slot presence is the support claim. The secret-backend
`mapping_model` registers as that kind's default slot; whether wave 3 re-homes it onto
`secret-source` is wave 3's call, not yours. Registration also carries `contract_version` from day
one (operator ruling) and the conformance checks in the contract's "Registration-time conformance"
section, which replace the current type-and-cast seam.

## 4. Simplifications your plan can bank

- The removals landed before phase 2 started, so 2.5's session-template model needs no legacy
  harness-selector shim and no `restart_command` handling; your cross-SDD coordination notes with
  the harness-integration and session-resume SDDs are discharged (both are locked).
- Keep your plan's 2.4-before-2.5 ordering (hardening before the kind-model swap); the contract
  records the reconciliation against your HLA's folded framing.
- Secret backends stay where they are in wave 2. The capability mandate, the relocation into
  `capabilities/`, and the singleton-registry flip are wave 3's; the descriptor record carries the
  interim instance-policy exception until then.

## 5. Scope you absorb from the roadmap

- The generic capability discriminator compatibility removal (your 2.4 hardening) and the decision
  on `agw resource migrate`'s future are yours, as your plan already says.
- Rolled-in issues to fold into your requirements where they land: #214 (contributed samples and
  uniform validation; the strict unknown-key direction resolves its open tradeoff), #311 (structural
  secret-name reference extraction), and #170 (`metadata.expires` as an envelope-metadata rider, so
  it is modeled once).
- Honor the four open doors in `target-state.md` (source-agnostic extraction, layer-stack merge,
  graph immutability as a registry/fold property, one instance-state store).

## 6. Coordination handles

- The `agw resource schema` and describe surface names are yours to settle, coordinated with the
  onboarding-and-discovery child (seeding now) since those surfaces are its raw material.
- The roadmap ledger (`child-sdds.md`) tracks your status from merged PRs; you do not update it. The
  roadmap lead reviews your PRs before merge.
