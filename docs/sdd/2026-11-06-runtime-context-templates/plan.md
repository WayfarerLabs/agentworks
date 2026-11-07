# Runtime context templates: plan

Governing requirements: [FRD](frd.md). Architecture: [HLA](hla.md). Detailed contract:
[interpolation LLD](interpolation-lld.md). Background: [prior art](prior-art-research.md).
Lifecycle: [delivery](migration-strategy.md).

Delivery: one PR containing the full feature, full SDD, permanent documentation, and validation. The
completed implementation is handed off ready on PR #867. Completion is tracked against the accepted
namespaced contract; lock only after all feature work and final feedback are complete.

## Design checkpoint

- [x] Inspect current env, setup, command interpolation, and managed-home producers at a pinned
      head.
- [x] Research primary-source prior art and record its implications and limitations.
- [x] Draft FRD, architecture, interpolation contract, migration, and this plan.
- [x] Establish a permanent reference describing current runtime context and interpolation.
- [x] Run independent project and complexity reviews of the design checkpoint; disposition material
      findings and record the reviewed head and results here.
- [x] Obtain operator disposition of the draft requirements, including the recommended omission of
      ambient lookup and references between managed env entries.

## Implementation

- [x] Record the accepted namespaced-only contract and destination-focused design, with opaque
      secrets and no ambient lookup or dependencies between env entries.
- [x] Implement the pure shared parser with explicit literal escapes and field-aware errors; prove
      one-pass rendering, unknown/unavailable distinction, and malformed syntax behavior.
- [x] Supply consuming user, VM, agent, workspace, and session context using the existing shared
      model; compute `user_home_dir` from its Linux username through the managed-home helper shared
      with provisioning and setup, without duplicating home state in each context.
- [x] Render winning plaintext env entries after inheritance, stored overlays, and scope merging.
      Remove early session-only expansion and preserve secret collection and identity precedence.
- [x] Reuse plaintext rendering in offline `env show`, retaining winning scope provenance and
      default secret redaction. Prove parity without a transport or target activation.
- [x] Migrate existing command interpolation to the shared parser and catalogue. Verify every
      integration's literal workload fields and intentionally expanded extra args.
- [x] Cover VM/agent shell and exec, session create/start/restart, console extra panes, and setup
      composition, including new agents and contexts missing workspace/session values.
- [x] Reject invalid winning expressions before prompts and mutations whenever their context is
      known, including before a restart destroys the current runtime.
- [x] Update `docs/arch/runtime-context.md` with the accepted vocabulary, grammar, availability,
      phase, consumer boundaries, examples, and how another consumer should adopt the renderer.
      Update schema-owned field descriptions, CLI reference, applicable guide signposts, sample
      config, and release guidance in the same implementation increment.

## Validation and delivery

- [x] Run focused behavior tests from the LLD vectors and required Python/file-quality checks.
- [x] Run the independent project and complexity lanes, plus the generic correctness lane for code,
      and resolve material findings under the development-process rules.
- [ ] Load `integration-testing` and `agw-test-env` before PR validation/live testing; derive a
      bounded charter from the accepted change. Do not infer permission to probe live user
      environments.
- [ ] Hand off one exact reviewed and validated state with its documentation and migration notes.
- [ ] Once implementation and documentation are complete, record truthful completion and add the
      lockfile. The permanent reference must stand alone if this entire SDD directory is deleted.

## Final review iterations

The operator authorizes up to three final PR review iterations. Mark the completed implementation
ready, then wait for the integration report before starting each subsequent correction round. Allow
up to four hours for the report. Record the exact reviewed head, findings, fixes, and evidence for
each iteration. Published feedback informs disposition; it does not expand the authorized scope.

## Implementation evidence

Private review head: `c124f36b90b81fe1c75e1f4276bb1ea04f59a70b`. Project, complexity, and generic
correctness lanes returned clean final verdicts. Modeled native arguments preserve literal data;
explicit extra arguments render before quoting. The optional required-workspace-root API change was
declined because both production callers supply it and the current behavior is sound.

Ruff and strict Mypy passed, with 886 files type checked. A real CLI smoke exercise used temporary
config/state and a hermetic child environment: three successful inspection calls, one expected
unavailable-context refusal, and zero transport attempts. It verified plaintext context, scope
precedence, reserved-prefix escaping, shell/Go literals, and opaque secret reveal with fixture data.
The full non-integration suite passed: 10,264 tests, with 11 skipped. File-quality, typer isolation,
rulesync drift, and locked-SDD checks passed. Website validation passed 160 Python tests, 103 Node
tests, and deterministic double builds at both supported site bases. The ready handoff records the
exact published head; live integration evidence is pending the standard pipeline.
