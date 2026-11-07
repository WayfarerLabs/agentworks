# Runtime context templates: plan

Governing requirements: [draft FRD](frd.md). Architecture: [HLA](hla.md). Detailed contract:
[interpolation LLD](interpolation-lld.md). Background: [prior art](prior-art-research.md).
Transition: [migration](migration-strategy.md).

Delivery: one PR containing the full feature, full SDD, permanent documentation, and validation. Its
current draft is a design checkpoint, labeled `review-requested` to solicit feedback. Continue
implementation on that same PR after requirements settle; do not merge the SDD separately. This plan
records proposed implementation, not completion. No lockfile belongs to this design checkpoint.

## Design checkpoint

- [x] Inspect current env, setup, command interpolation, and managed-home producers at a pinned
      head.
- [x] Research primary-source prior art and record its implications and limitations.
- [x] Draft FRD, architecture, interpolation contract, migration, and this plan.
- [x] Establish a permanent reference describing current runtime context and interpolation.
- [x] Run independent project and complexity reviews of the design checkpoint; disposition material
      findings and record the reviewed head and results here.
- [ ] Obtain operator disposition of the draft requirements, including the recommended omission of
      ambient lookup and references between managed env entries.

## Implementation

- [ ] Confirm accepted requirements and update the architecture/LLD for any environment-reference
      decision. Define all lookup, redaction, and inspection semantics before adding such a source.
- [ ] Implement the pure shared parser with explicit literal escapes and field-aware errors; prove
      one-pass rendering, unknown/unavailable distinction, and malformed syntax behavior.
- [ ] Supply consuming user, VM, agent, workspace, and session context using the existing shared
      model; compute `user_home_dir` from its Linux username through the managed-home helper shared
      with provisioning and setup, without duplicating home state in each context.
- [ ] Render winning plaintext env entries after inheritance, stored overlays, and scope merging.
      Remove early session-only expansion and preserve secret collection and identity precedence.
- [ ] Reuse plaintext rendering in offline `env show`, retaining winning scope provenance and
      default secret redaction. Prove parity without a transport or target activation.
- [ ] Migrate existing command interpolation to the shared parser and catalogue. Verify every
      integration's literal workload fields and intentionally expanded extra args.
- [ ] Cover VM/agent shell and exec, session create/start/restart, console extra panes, and setup
      composition, including new agents and contexts missing workspace/session values.
- [ ] Reject invalid winning expressions before prompts and mutations whenever their context is
      known, including before a restart destroys the current runtime.
- [ ] Update `docs/arch/runtime-context.md` with the accepted vocabulary, grammar, availability,
      phase, consumer boundaries, examples, and how another consumer should adopt the renderer.
      Update schema-owned field descriptions, CLI reference, applicable guide signposts, sample
      config, and release guidance in the same implementation increment.

## Validation and delivery

- [ ] Run focused behavior tests from the LLD vectors and required Python/file-quality checks.
- [ ] Run the independent project and complexity lanes, plus the generic correctness lane for code,
      and resolve material findings under the development-process rules.
- [ ] Load `integration-testing` and `agw-test-env` before PR validation/live testing; derive a
      bounded charter from the accepted change. Do not infer permission to probe live user
      environments.
- [ ] Hand off one exact reviewed and validated state with its documentation and migration notes.
- [ ] Once implementation and documentation are complete, record truthful completion and add the
      lockfile. The permanent reference must stand alone if this entire SDD directory is deleted.

## Design review record

Reviewed head: `0724c4788113b686e26d85708d144622c16b6a79`.

The independent project reviewer and complexity reviewer returned clean final verdicts. The first
round's two complexity findings were incorporated: recognize only opening delimiters as template
syntax, and derive the managed home through the shared rule rather than duplicate home state in the
context. The operator's target-environment clarification is transcribed in the draft FRD; omitting
lookup and cross-entry dependencies remains a recommendation for draft acceptance.

The repository file-quality script passed formatting, Markdown lint, and spelling checks. No runtime
implementation or live-environment testing is part of this design checkpoint.
