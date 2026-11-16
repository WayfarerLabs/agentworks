# Artifact bundle sources: plan

## Delivery and authority

Start with a compact design checkpoint on its own branch and draft PR. Its FRD is proposed
requirements; authenticated operator review accepts the scope and settles the HLA's open decisions.
Implementation follows the direct-declaration effort, not a change to its handed-off head. The
default implementation vehicle is the same feature branch/PR after design review. If another effort
needs the design on main, request promotion through the standard SDD process first.

No checkboxes claim completed acceptance, implementation or review in this initial draft.

## Design checkpoint

- [ ] Review and accept the FRD, including an explicit initial Agent Plugins import profile.
- [ ] Settle relative-reference containment, imported inheritance and ordinary local-path migration.
- [ ] Write the small schema/discovery LLD: exact source envelope, naming, ordering, overrides,
      nested imports, missing/invalid files and bounded expansion. Include worked local, Git,
      per-type, mixed-directory and remote-definition examples.
- [ ] Review the frozen design with the project reviewer and Muntz; record dispositions and obtain
      authenticated direction for any requirements changes.

## Implementation

- [ ] Add strict bundle source declarations and declaration-relative contexts that survive
      inheritance and whole-entry overrides. Keep existing explicit-only authoring compatible except
      for the operator-approved relative-path migration.
- [ ] Extend shared bounded capture for directory discovery and local/Git definition imports. Prove
      same-commit reads, deterministic names, cycle rejection and no partial successful capture.
- [ ] Add the approved Agent Plugins profile through the same boundary. Prove supported semantics
      and explicit errors for unsupported package/runtime behavior.
- [ ] Retain member/definition provenance for offline inspection; preserve owner/type/key identity,
      fingerprints where source meaning is unchanged, supported persisted reads and native cleanup
      behavior. Include declaration bases where relative resolution depends on them.
- [ ] Ship permanent artifact guide, schema reference, module README and safe runnable examples
      alongside the behavior. Permanent guidance must not depend on this SDD.

## Validation and closeout

- [ ] Exercise local and Git variants of all discovery/import paths. Test add/change/remove,
      inherited relative bases, cross-directory invocation, duplicate names, malformed content,
      escaping paths, nested cycles, acquisition bounds and a moving Git ref during capture.
- [ ] Verify inspection performs no source acquisition and exposes metadata rather than content or
      credential values. Exercise existing bundles, direct declarations if landed, multiple scopes
      and bundle-to-source transitions without native identity churn.
- [ ] Run required gates, package/install and guide/schema checks. Run independent project and Muntz
      reviews, plus the generic correctness lane for code-heavy work, at the exact handoff head.
- [ ] Hand off ready with exact-head evidence and a scoped integration charter. Wait for the actual
      integration report before each authorized feedback/fix round, up to four hours per the
      operator's current direction; a missing report does not count as a pass.
- [ ] Complete live add/change/remove and cleanup validation on the approved profile through the
      separate integration-test session. Record unreachable surfaces and resolve material findings.
- [ ] Promote durable contracts, reconcile acceptance against R1-R6, retire owned fixtures and
      scratch, then lock only when all accepted requirements are complete or explicitly disposed.
