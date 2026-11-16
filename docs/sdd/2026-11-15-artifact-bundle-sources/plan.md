# Artifact bundle sources: plan

## Delivery and authority

Start with a compact design checkpoint on its own branch and draft PR. Its FRD is proposed
requirements; authenticated operator review accepts the scope and settles the HLA's open decisions.
Implementation follows the direct-declaration effort, not a change to its handed-off head. The
default implementation vehicle is the same feature branch/PR after design review. If another effort
needs the design on main, request promotion through the standard SDD process first.

The phases below are working cut points, not an automatic PR series. Directory discovery can be
validated without imported definitions or a plugin runtime. This draft does not claim completed
design acceptance or implementation; private checkpoint evidence lives in its handoffs.

## Design checkpoint

- [ ] Review and accept the FRD, including an explicit initial Agent Plugins import profile.
- [ ] Settle relative-reference containment, imported inheritance and ordinary local-path migration.
- [ ] Write the small schema/discovery LLD: one whole-source selector, its inheritance/null
      clearing, candidate naming, explicit-map precedence, definition depth, invalid files and
      bounded expansion. Include parent-explicit/child-source precedence and all requested source
      forms, plus missing/stale collection inventory in inspection.
- [ ] Review the frozen design with the project reviewer and Muntz; record dispositions and obtain
      authenticated direction for any requirements changes.

## Phase 1: directory discovery

- [ ] Add the bundle-only selector using ordinary offline inheritance and whole-selector
      replacement/null clearing. Keep shared direct definitions as explicit maps. Use the existing
      source grammar in this phase; do not change relative-path meaning for only one artifact form.
- [ ] Add bounded candidate selection before content capture for each type, on workstation and Git
      sources. Expand the mixed layout into those same selections. Reuse revision resolution,
      canonical validation, normalization and the existing capture operation.
- [ ] Overlay the effective explicit maps after discovery, then use existing ordered owner bundle
      composition. Prove stable names, ambiguity/error behavior, add/change/remove and no partial
      successful capture without inventing a second inheritance fold.
- [ ] Reuse member provenance and supported capture reads. Make captured versus unknown collection
      membership clear in offline inspection. Preserve unchanged explicit-only fingerprints and
      native identities; do not bump the codec solely for directory discovery.
- [ ] Ship the phase's permanent guide/reference/examples with its code and validate the working
      directory-only increment before adding definition imports.

## Phase 2: relative references and definition imports

- [ ] Specify the migration for existing cwd-relative artifact references across bundles, owner
      templates and stored instance overlays. Cover inherited entries, independent overrides and
      declarations without a file base. Preserve authored provenance and avoid a silent fallback.
- [ ] Normalize references at the declaring-document boundary where possible without acquiring
      content; verify inherited bases and same-repository/ref cache reuse. Include known base
      changes in offline declaration identity while keeping imported bodies/revisions out of it.
- [ ] Import a workstation/Git bundle definition under the accepted containment/inheritance/depth
      and cross-repository trust contract. It can select directories and explicit artifacts. Reject
      unsupported nesting or bound/reject cycles if deeper definition imports were accepted.
- [ ] Retain definition provenance and recorded membership for inspection without claiming a current
      remote inventory. Define any necessary persisted-format transition and retain supported reads.
- [ ] Ship migration guidance, remote/local worked examples and boundary/cleanup tests with this
      working increment. Preserve all phase-1 behavior and explicit-map override rules.

## Phase 3: Agent Plugins profile

- [ ] Implement the operator-approved initial profile through the same source/discovery boundary.
      Validate supported metadata/components and explicitly reject semantics the profile cannot
      preserve. Add source-compatibility fixtures and native registration checks where applicable.
- [ ] Keep package execution/data lifecycle outside the proposed limited import unless the operator
      explicitly expands the scope and its design. Settle this before code; reading a manifest does
      not supply installed target files, persistent data or canonical MCP working-directory support.
- [ ] Ship the profile's supported/unsupported contract and examples alongside its code. Complete
      R1-R6 for the accepted overall scope before claiming this SDD complete.

## Validation and closeout

- [ ] Exercise local and Git variants of all discovery/import paths. Test add/change/remove,
      inherited relative bases, cross-directory invocation, duplicate names, malformed content,
      escaping paths, rejected nesting or accepted-depth cycles, acquisition bounds and a moving Git
      ref during capture. Ignore ordinary neighbors but fail malformed selected candidates.
- [ ] Verify inspection performs no source acquisition and exposes metadata rather than content or
      credential values. Exercise existing bundles, direct declarations if landed, multiple scopes
      and bundle-to-source transitions without native identity churn.
- [ ] Run required gates, package/install and guide/schema checks. Run independent project and Muntz
      reviews, plus the generic correctness lane for code-heavy work, at the exact handoff head.
- [ ] Hand off ready with exact-head evidence and a scoped integration charter through the standard
      [delivery process](../../../.rulesync/skills/agentic-dev-process/references/delivery.md).
      Follow the [integration process](../../../.rulesync/skills/integration-testing/SKILL.md);
      transient timing/budget directions belong in handoffs, not this specification.
- [ ] Complete live add/change/remove and cleanup validation on the approved profile through the
      separate integration-test session. Record unreachable surfaces and resolve material findings.
- [ ] Promote durable contracts, reconcile acceptance against R1-R6, retire owned fixtures and
      scratch, then lock only when all accepted requirements are complete or explicitly disposed.
