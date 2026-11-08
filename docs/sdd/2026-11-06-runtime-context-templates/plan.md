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
- [ ] Reject invalid winning expressions before prompts and mutations whenever their context is
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

## Final review iteration 1

Ready head: `b927a0ff8207a7f49a16f527a8995859c8d6beba`, handed off on 2026-11-07. Muntz's report
accepted the namespace and identified stale resource guidance and avoidable command-output
rendering. The namespace-only contract remains as directed by the operator; no legacy detector is
added.

Integration reports on 2026-11-08 tested that exact head against `c8d4dfb3`: all required gates
passed, with 10,264 non-integration tests passed and 11 skipped. Live Linux/macOS and native Windows
evidence found an identity-precedence regression and the stale guide. Windows measured literal-env
cases passed; Linux/macOS literal delivery failed in inherited SSH SetEnv expansion. Their bounded
live and cleanup evidence is in PR comments 6719400693 and 6725905647. These are not full acceptance
and are not evidence about the subsequently composed build.

## Final review iteration 2

The operator authorized two additional cycles, retaining the requirement to wait for an integration
report before each. Both iteration-one integration reports were present before this correction round
began. PR #867 returned to draft. Latest main `89bb63e40` was merged for a conflict-free composed
build; no other effort's SDD artifacts are edited by this effort.

The fixed feedback batch includes identity precedence before validation and secret collection,
field-owned command rendering, the retained resource guide, dollar-adjacency guidance, and precise
validation timing. Independent private reviews and required gates must pass before the resulting
head is handed off.

### Acceptance-blocking transport dependency

The inherited `_set_env_args` implementation in `cli/agentworks/ssh.py` quotes SSH configuration
syntax but does not preserve arbitrary literal values on clients that expand SetEnv values. The
reported Linux/macOS failures include escaped namespace text, opaque secrets containing that text,
and ordinary `${HOME}`. Native Windows cases passed on the measured client; no universal transport
pass follows from that result.

Read-only investigation compared official OpenSSH source and actual offline local 9.2 client probes.
Inspected 9.x sources preserve SetEnv text; 10.0 adds expansion with no portable dollar escape. A
transport fix needs a design covering old and new clients and the low-level run, transport run,
interactive, and streaming paths. Child-environment forwarding must not allow remote declarations to
alter local client execution; shell preludes would reopen ADR 0014. This exceeds a small incidental
correction. The accepted literal-data and preview-parity requirements remain in force;
operator/transport-owner disposition is pending, and the SDD remains open. Critical reading and
call-site evidence are recorded in PR comment 6728073054.

### Validation timing ruling pending

The accepted FRD requires known winning references to fail before prompts and mutations. Env checks
run at that boundary, but selected command fields render inside integration launch selection, after
secret resolution and potentially workspace access realization. They still fail before destructive
runtime replacement. The earlier completion checkbox for the broader timing requirement was
premature and is corrected while unmerged, under the SDD's erroneous-checkbox carve-out.

The proposed requirement amendment would retain early env validation and require selected command
validation before launch or destructive replacement, without a generic integration preflight hook.
This is a request for operator disposition, not an amendment to the accepted FRD. The current FRD
remains unchanged and this requirement remains incomplete until the operator rules or implementation
meets its existing timing.
