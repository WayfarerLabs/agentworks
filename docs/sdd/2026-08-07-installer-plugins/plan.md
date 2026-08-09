# Implementation plan: installer resource plugins

- Status: Independent artifact review clean, pending roadmap review
- Date: 2026-08-08
- Scope authority: [FRD](./frd.md)
- Architecture: [HLA](./hla.md)
- Migration: [migration strategy](./migration-strategy.md)

## Delivery shape

The work ships as an always-green series:

1. The artifact PR establishes the revised scope and reviewed design.
2. A resource-policy PR lands the reusable R7 and C4 foundation with its permanent resource docs.
3. An installer-resource PR adds both manifest-only plugins, moves all 16 rows, and carries the
   operator migration docs.
4. Closeout locks the SDD only after implementation, package, completion, and live gates pass.

The plugin move does not begin until the foundation is merged or present as a reviewed stack base.
No commit publishes a row from both its old and new provider.

## Phase 0: design convergence

- [x] Revise the FRD and exhaustive inventory to the 2026-08-08 declared-resource-only ruling.
- [x] Write and review the HLA, migration strategy, and this plan.
- [x] Resolve every blocking and important artifact-review finding.
- [x] Rebase the artifact branch on current `main`, run scoped doc gates, and publish the artifacts
      for roadmap review.

Definition of done: the operator and independent reviewer agree that only the 16 declared rows move,
all core executors and excluded mechanisms are named explicitly, and R7/C4 have implementable
contracts with no open blocking decision.

## Phase 1: resource policy and retained provenance

- [x] Write `resource-disable-lld.md` for complete provider-claim collection, policy injection,
      publication-order-independent resolution, selector matching, retained disable marks,
      substitutions, and graph projections.
- [x] Independently review the LLD before implementation.
- [ ] Add typed config models and parsing for `[resource_policy].disabled` with canonical selector
      validation and sample-config coverage.
- [ ] Replace first-source enablement loss with deterministic retention of all disable marks on the
      frozen graph.
- [ ] Add config-independent registry policy input, a complete per-selector provider-claim ledger,
      exact idempotent republication, and unmatched/inapplicable selector validation.
- [ ] Make plugin manifest publication strong in both opt-in states and remove the weak-row contract
      where no remaining caller needs it.
- [ ] Implement order-symmetric collision errors and explicit disable-and-redeclare substitution
      records.
- [ ] Update `describe`, `doctor`, list projections, ADR 0021, the resource guide, and framework
      READMEs in the same PR.
- [ ] Cover parsing, multiple causes, both publication orders, plugin-off collisions, explicit
      replacement, ambiguous multi-provider claims, unmatched selectors, and provenance with unit
      and integration tests.
- [ ] Inventory all 27 shipped selectors whose same-name operator replacement now requires explicit
      disable, and add exact upgrade examples and regression coverage for the existing Azure,
      Claude, and Codex rows.
- [ ] Run the repository's required lint, type, and test gates plus independent implementation and
      fresh-eyes reviews.

Definition of done: a stable shipped name cannot change provider without one exact policy selector,
all disable causes and sanctioned substitutions survive graph freeze, and every permanent resource
contract matches the code.

## Phase 2: enabled-edge invariant and projections

- [ ] Reject enabled resource-to-resource edges to disabled targets during finalize, including exact
      source, target, location, cause, and remediation text.
- [ ] Cover re-enable and remove-reference remediations for every disabled target, plus the paired
      explicit-disable and same-name declaration alternative for declarable targets only. Prove
      capability errors never offer an operator declaration.
- [ ] Prove disabled sources are inert and settings references keep presence-not-availability
      semantics.
- [ ] Make resource-derived guide names read stored graph enablement while conceptual plugin topics
      remain discoverable.
- [ ] Verify normal list and dynamic completion hide disabled rows while include-disabled, describe,
      and doctor retain diagnostic access.
- [ ] Add Bash, Zsh, and PowerShell behavior tests through the existing dynamic completion sources.
- [ ] Run the full Phase 1 gates and independent reviews again.

Definition of done: invalid enabled edges fail before remote mutation, disabled cohorts do not break
unrelated configs, and every projection consumes one retained enablement truth.

## Phase 3: `apt` manifest plugin and ten-row move

- [ ] Add the installed `apt` plugin with manifests and conceptual guide content, but no capability.
- [ ] Move the five apt sources and five apt packages from the built-in package without changing
      decoded names or specs.
- [ ] Add exact roster, dependency, disabled, enabled, collision, replacement, and package-data
      tests.
- [ ] Prove the unchanged VM initializer consumes enabled plugin rows and preserves reinit
      idempotency.
- [ ] Update the sample config, CLI README, plugin README, built-in manifest README, resource and
      idempotency guides, and 0.14 upgrade guide in lockstep.
- [ ] Run scoped and full gates plus independent implementation and fresh-eyes reviews.

Definition of done: all ten apt rows have exactly one provider, enabling `apt` preserves current
behavior, disabling it gives precise pre-mutation remediation, and no apt executor or raw config
field moved.

## Phase 4: `install-command` manifest plugin and six-row move

- [ ] Add the installed `install-command` plugin with manifests and conceptual guide content, but no
      capability.
- [ ] Move the six user install commands from the built-in package without changing decoded names or
      specs.
- [ ] Add exact roster, predicate-field, disabled, enabled, collision, replacement, and package-data
      tests.
- [ ] Prove the unchanged admin and agent runners consume enabled plugin rows, preserve PATH
      behavior, and remain idempotent.
- [ ] Update every permanent roster, selection example, upgrade instruction, and sample-config
      assertion in lockstep.
- [ ] Prove exact plugin-enable remediation emits valid two-line TOML and preserves every existing
      enabled plugin name.
- [ ] Prove a plugin-disabled row's replacement remediation includes both the exact explicit-disable
      selector and a same-name operator declaration.
- [ ] Run scoped and full gates plus independent implementation and fresh-eyes reviews.

Definition of done: all six command rows have exactly one provider, enabling `install-command`
preserves current behavior in both user scopes, and no evaluator or runner moved.

## Phase 5: artifact and live acceptance

- [ ] Build the wheel, install it into an isolated environment, and load both plugins' YAML and
      guide Markdown through package resources.
- [ ] Drive the shipped CLI in an isolated home through disabled, enabled, collision, explicit
      replacement, list, completion, guide, describe, and doctor scenarios.
- [ ] Run live VM create and repeatable reinit on an available backend with bounded spend and
      cleanup according to the integration-testing and test-environment protocols.
- [ ] Record unavailable backends as explicit test gaps rather than simulated success.
- [ ] Run final repository gates and obtain clean independent code, docs, and fresh-eyes reviews.
- [ ] Update every plan checkbox truthfully, add `locked.md`, and use the repository's breaking
      conventional-commit form with a `BREAKING CHANGE:` footer for the operator-facing move.

Definition of done: AC1 through AC6 have evidence, the installed artifact behaves like the source
tree, live enabled initialization and reinit converge, no test resource remains, permanent docs are
current, and the SDD can be locked without unresolved work.
