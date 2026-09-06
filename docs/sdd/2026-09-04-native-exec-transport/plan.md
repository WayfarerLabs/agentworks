# Implementation Plan: Native Execution Transport

<!-- cspell:ignore sdds -->

- Status: Implementation
- Date: 2026-09-04
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [exec-transport-lld.md](./exec-transport-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `c962f52043e9ea239197ad96d5a383f98db9164d`
- Implementation baseline: `a64b1b9cff5449807695e6933a0e92786b24a06d`
- Delivery: SDD and post-0.18.0 runtime implementation on PR #746

## Delivery rules

- This PR began as the complete SDD artifact set. After design approval, the operator authorized the
  post-0.18.0 runtime implementation on the same PR. It remains draft until implementation,
  validation, and review converge.
- The PR carries `sdd:native-exec-transport`. Coherent draft checkpoints use `review-requested`; the
  final handoff uses ready state.
- The completed design-review budget was up to three published feedback/fix rounds. The operator
  separately authorized up to three implementation feedback/fix rounds. Each round follows the
  standard batching, critical disposition, draft-before-mutation, private-review, and exact-head
  handoff rules.
- Runtime implementation is authorized on the post-0.18.0 baseline recorded above and must remain
  outside the 0.18.0 release.
- A changed product requirement, a need for persisted state or configuration, Proxmox live evidence
  that refutes the QGA design, or non-converging material review stops for operator direction.
- The lead does not merge its own PR. Completed plan checkboxes become immutable after merge; a
  later correction appends a superseding item.

## SDD artifact gates

From the repository root:

```console
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
```

The artifact handoff also requires link review, spelling and Markdown checks through `lint-files`,
manual typography and stale-claim scans, exact-head private agentworks-reviewer and Muntz review,
and a clean diff against the recorded baseline.

## Runtime implementation gates

From `cli/`:

```console
uv run ruff check agentworks/ tests/
uv run ruff format --check agentworks/ tests/
uv run mypy agentworks/ tests/
uv run pytest tests/ -m 'not integration'
```

From the repository root:

```console
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/*.test.mjs
```

The runtime handoff additionally requires focused Proxmox tests, installed-wheel CLI smoke tests,
private project and Muntz reviews, a cold correctness and security review, CI, and
capability-appropriate live validation under the integration-testing process.

## Requirement traceability

| Requirements | Architecture owner                           | Planned proof                                       |
| ------------ | -------------------------------------------- | --------------------------------------------------- |
| R1-R5        | required native factory and vm-platform hook | conformance, fake, route, and no-fallback tests     |
| R6-R10       | `ExecTransport` contract                     | base contract and sensitive-input tests             |
| R11-R13      | full `Transport` subtype and shell narrowing | concrete transport and shell tests                  |
| R14-R18      | `ProxmoxExecTransport` and QGA API           | API wire, adapter, create, recovery, and live tests |
| R19-R22      | atomic v1 migration and permanent collateral | registry, residual, docs, and release checks        |
| Q1-Q5        | verification strategy                        | focused, full, private, CI, and live evidence       |

## Phase 0: Complete SDD checkpoint

- [x] Refresh from `main` at `c962f52043e9ea239197ad96d5a383f98db9164d` and create
      `docs/native-exec-transport-sdd`.
- [x] Inventory the transport ABC, native factory, vm-platform hook, provision result, all core
      native consumers, all bundled platform returns, Proxmox QGA helpers, tests, and permanent
      docs.
- [x] Research pyinfra connector, command, facts, operation, and dependency boundaries from upstream
      sources.
- [x] Research the supported Proxmox VE 8 guest-exec and exec-status contract, QEMU Guest Agent
      semantics, timeout limits, stdin bound, result shape, and permission evolution.
- [x] Draft the FRD, HLA, execution LLD, migration strategy, prior-art research, and this plan as
      one coherent artifact set.
- [x] Run artifact gates, manual scans, link review, and cross-file consistency review.
- [x] Obtain clean private agentworks-reviewer and Muntz passes on one exact artifact head and apply
      every material correction authorized by the SDD charter.
- [x] Commit and push the coherent artifact head, open the draft PR, apply
      `sdd:native-exec-transport` and `review-requested`, and publish the exact handoff.
- [x] Complete up to three authorized published feedback/fix rounds, or stop sooner when one full
      batch produces no material changes.
- [x] Remove `review-requested` and promote the artifact PR to ready when design converges.
- [x] Incorporate the operator-authorized post-ready review of the three requirement and
      architecture threads and obtain clean exact-head private reviews.
- [x] Publish the exact-head feedback/fix handoff and receive authenticated design approval; the
      operator directed implementation to continue on the same PR instead of merging artifacts
      separately.

### Phase 0 definition of done

- Every functional and quality requirement has an architecture owner, detailed design, migration
  disposition, implementation phase, and objective proof.
- Required exec-only behavior and optional rich native behavior are separated without a second hook
  or factory.
- Proxmox identity, stdin, result, timeout, ambiguity, and secret behavior is explicit.
- Design approval preceded runtime implementation; no lock file, 0.18 release claim, or material
  design finding remained at that boundary.

## Phase 1: Build the narrow type and Proxmox carrier

- [ ] Refresh the baseline after 0.18.0 and confirm the SDD inventory before editing.
- [ ] Add `ExecTransport` with only `sudo`, `check`, `timeout`, and sensitive `input_text`; make
      `Transport` extend it and retain its existing wider `run`, terminal, streaming, and file
      surface.
- [ ] Keep existing result, error, and logger compatibility types; update only imports and type
      annotations required by the split.
- [ ] Split Proxmox guest exec dispatch from status polling with response validation and injected
      time controls.
- [ ] Implement the QGA execution transport with admin/root rendering, checked exits, finite stdin,
      payload-size enforcement, and logging.
- [ ] Implement remaining-deadline polling, explicit signal and truncation handling, safe timeout
      context, and no adapter redispatch after ambiguity.
- [ ] Prove that sensitive stdin appears only in the outgoing provider `input-data` field and is
      absent from argv, logs, results, diagnostics, exceptions, causes, and contexts.
- [ ] Preserve the existing private bootstrap staging, cleanup, and interrupt behavior.

### Phase 1 definition of done

- Existing full transports retain behavior and satisfy both types.
- The isolated Proxmox adapter satisfies the narrow execution contract before it becomes required by
  the platform API.
- Failure modes are explicit and no test assumes timeout cancellation.
- No token, sensitive stdin, or private bootstrap value reaches an unsafe diagnostic surface.

## Phase 2: Cut over the version-1 contract atomically

- [ ] Narrow `ProvisionResult.native_transport`, `VMPlatform.native_transport`, and the native
      factory; make the platform hook abstract and nonoptional.
- [ ] Update every VM-platform implementation and version-1 conformance fixture, implement the
      Proxmox hook, and return the QGA transport from Proxmox create in the same transition.
- [ ] Narrow Debian attestation, Phase A provisioning, Tailscale repair, rekey, and logout to the
      execution type.
- [ ] Add an execution-only fake and prove all core native consumers except the sole allowlisted
      platform-shell path use only the narrow contract.
- [ ] Rename the shell guidance to `native_shell_unavailable_hint`, let Proxmox declare it, and
      reject `vm shell --platform` before credential, route, transport, or probe work.
- [ ] For platforms declaring native shell support, require a full `Transport` before interaction
      and preserve canonical shell behavior.
- [ ] Delete the optional-return error path and scan for any other broad native caller.

### Phase 2 definition of done

- Every VM platform registers with one required native execution implementation.
- Proxmox release attestation, Phase A provisioning, repair, rekey, logout, and probe paths accept
  the adapter.
- Only the explicit platform shell path requires the full subtype, and Proxmox refuses it before
  provider work.
- Canonical commands still fail on canonical transport failure and never fall back.
- The capability remains version 1 with no compatibility adapter or broken intermediate handoff.

## Phase 3: Permanent collateral and static verification

- [ ] Update root and vm-platform capability requirements with required native execution and
      optional full interaction, while retaining contract version 1.
- [ ] Update the Proxmox guide and capability description for QGA recovery, the declared provider
      scope, and unavailable platform shell.
- [ ] Update nearby code contracts and delete the temporary Proxmox non-compliance language.
- [ ] Run `rg -n '727|non-compliant|noncompliant' cli/agentworks docs/guides scripts` and remove or
      disposition every residual temporary tracking statement.
- [ ] Confirm no config, sample config, completion, command reference, JSON schema, database
      migration, or release-upgrade guide changed without a new requirement.
- [ ] Confirm dependency manifests contain no new pyinfra package.
- [ ] Run focused tests throughout, then the complete Python and repository gate set.
- [ ] Build and install the wheel in an isolated environment and smoke the affected CLI paths.

### Phase 3 definition of done

- Permanent capability and operator docs match implemented behavior and provider support.
- All static, unit, integration-mark-excluded, packaging, and repository gates pass.
- Residual scans find no optional native hook or claim that Proxmox lacks recovery execution.

## Phase 4: Review, live validation, and closeout

- [ ] Obtain clean private agentworks-reviewer, Muntz, and cold correctness/security passes on one
      exact runtime head; apply every material authorized correction and rerun affected gates.
- [ ] Load the integration-testing and agw-test-env skills, prepare an operator-reviewed live
      charter, and validate one full native platform plus Proxmox QGA on expendable resources.
- [ ] On Proxmox, prove create-time release attestation, Tailscale-independent rejoin and rekey,
      canonical no-fallback, platform-shell refusal, timeout honesty, and canary-secret absence.
- [ ] Record any unavailable Proxmox live evidence for authenticated operator disposition rather
      than satisfying acceptance from mocks.
- [ ] Complete up to three authorized published runtime feedback/fix rounds, if needed.
- [ ] Merge or rebase the latest `main`, resolve conflicts semantically, and rerun exact-head gates
      and reviews.
- [ ] Promote load-bearing rules to permanent docs and code, truthfully complete the plan, add
      `locked.md`, remove `review-requested`, mark ready, and obtain operator disposition without
      self-merging.

### Phase 4 definition of done

- Exact pushed-head automated, private-review, CI, packaging, and authorized live evidence is green.
- No material correctness, complexity, security, capability, collateral, release, or migration
  finding remains.
- The implementation SDD is locked only after runtime completion, and no permanent behavior depends
  on the SDD path.
- The ready runtime PR is explicitly post-0.18.0 and awaits operator merge disposition.
