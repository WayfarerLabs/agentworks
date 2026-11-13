# Model-declared runtime interpolation: plan

Vehicle: one feature PR on `feat/model-declared-interpolation`, initially a draft `review-requested`
design checkpoint. No implementation has started. This effort is independent of the test-only
cleanup in PR #896 and does not change that PR.

## Design checkpoint

- [x] Draft requirements, architecture, mechanism and migration strategy.
- [x] Audit core and all shipped system plugins, tracing candidate fields to consumers.
- [ ] Publish the migration inventory with recommendations, costs and explicit choices.
- [ ] Discuss and record the operator's disposition of new field adoption.
- [ ] Complete independent project and complexity reviews of the design checkpoint.

Definition of done: the operator can select concrete field groups from the inventory; the design
distinguishes established requirements from proposed adoption. New runtime support is not implied by
publishing the draft.

## Shared declaration and existing support

- [ ] Add the marker, bounded placement checks and schema extension.
- [ ] Extend the common field documentation projection and scalar shorthand metadata.
- [ ] Add the model-aware rendering adapter and migrate winning plaintext env.
- [ ] Migrate selected Shell commands and native extra arguments with existing semantics.
- [ ] Verify schema/documentation/runtime agreement and the literal unmarked default.

Definition of done: existing support has one eligibility declaration, normal model loading stays
context-free, and existing lifecycle, secret and compatibility behavior is preserved.

## Agreed new consumers

- [ ] Replace proposed inventory dispositions with the operator's agreed adoption list.
- [ ] Implement only agreed fields, including context threading and destination handling.
- [ ] Validate each new boundary and preserve existing path/merge conventions.
- [ ] Review the shipped manifests; change examples only where the new language helps, without
      mechanically replacing ordinary shell variables or already useful tilde paths.

Definition of done: each adopted field has a concrete consumption boundary, consistent checking and
execution values, correct quoting, field-aware failures and useful examples.

## Permanent documentation and handoff

- [ ] Add the dedicated `concept-templating` guide and useful command signposts.
- [ ] Update the runtime-context reference, schema README and affected component docs.
- [ ] Resolve the compatibility-cutoff documentation alignment with issue #878 as an explicit scoped
      decision, preserving the operator's two-minor-release policy.
- [ ] Run appropriate behavioral tests and required gates; review generated field output.
- [ ] Run independent project, complexity and correctness reviews for implementation.
- [ ] Hand off the exact green head through the standard PR-level validation process.
- [ ] Complete the SDD only after implementation, review and accepted validation; add its lockfile
      in the final delivery state.

## Evidence

Candidate audit snapshot: `3ad615680ee8e1ef56c864170deaf62392578661`, 2026-11-13. Design-only
checkpoints do not claim implementation or live validation results. Record checkpoint checks, review
dispositions and later implementation evidence here as they occur.
