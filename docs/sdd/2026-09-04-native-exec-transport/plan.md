# Implementation Plan: Native Execution Transport

<!-- cspell:ignore sdds -->

- Status: Design
- Date: 2026-09-04
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [exec-transport-lld.md](./exec-transport-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `c962f52043e9ea239197ad96d5a383f98db9164d`
- Delivery: artifact PR now; separately authorized runtime PR after 0.18.0

## Delivery rules

- This PR contains the complete SDD artifact set and no runtime implementation. It remains draft
  during design review and becomes ready only when design feedback converges.
- The PR carries `sdd:native-exec-transport`. Each coherent design handoff uses `review-requested`.
- The operator authorized up to three published SDD feedback/fix rounds. Each round waits the
  standard minimum of one hour after handoff unless the operator sets another interval, collects one
  complete batch, critically dispositions every material item, returns the PR to draft before
  mutation, reruns private reviews after changes, and hands off one exact head.
- Runtime implementation is not authorized by this plan and must not ship in 0.18.0. After a new
  authenticated direction, it starts from the correct post-0.18 baseline on a separate PR.
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
- [ ] Commit and push the coherent artifact head, open the draft PR, apply
      `sdd:native-exec-transport` and `review-requested`, and publish the exact handoff.
- [ ] Complete up to three authorized published feedback/fix rounds, or stop sooner when one full
      batch produces no material changes.
- [ ] Remove `review-requested` and promote the artifact PR to ready when design converges.

### Phase 0 definition of done

- Every functional and quality requirement has an architecture owner, detailed design, migration
  disposition, implementation phase, and objective proof.
- Required exec-only behavior and optional rich native behavior are separated without a second hook
  or factory.
- Proxmox identity, stdin, result, timeout, ambiguity, secret, and provider-version behavior is
  explicit.
- pyinfra adoption is bounded to a future evaluation and adds no dependency or framework surface.
- No runtime implementation, lock file, 0.18 release claim, or material review finding remains.

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
- [ ] Update every bundled platform and version-1 conformance fixture, implement the Proxmox hook,
      and return the QGA transport from Proxmox create in the same transition.
- [ ] Narrow Debian attestation, Phase A provisioning, Tailscale repair, rekey, and logout to the
      execution type.
- [ ] Add an execution-only fake and prove all core native consumers except platform shell use only
      the narrow contract.
- [ ] Rename the shell guidance to `native_shell_unavailable_hint`, let Proxmox declare it, and
      reject `vm shell --platform` before credential, route, transport, or probe work.
- [ ] For platforms declaring native shell support, require a full `Transport` before interaction
      and preserve canonical shell behavior.
- [ ] Delete the optional-return error path and scan for any other broad native caller.

### Phase 2 definition of done

- Every bundled platform registers with one required native execution implementation.
- Proxmox release attestation, Phase A provisioning, repair, rekey, logout, and probe paths accept
  the adapter.
- Only the explicit platform shell path requires the full subtype, and Proxmox refuses it before
  provider work.
- Canonical commands still fail on canonical transport failure and never fall back.
- The capability remains version 1 with no compatibility adapter or broken intermediate handoff.

## Phase 3: Permanent collateral and static verification

- [ ] Update root and vm-platform capability requirements with required native execution and
      optional full interaction, while retaining contract version 1.
- [ ] Update the Proxmox guide and capability description for QGA recovery, VE 8 permissions, and
      unavailable platform shell.
- [ ] Update nearby code contracts and delete the temporary Proxmox non-compliance language.
- [ ] Confirm no config, sample config, completion, command reference, JSON schema, database
      migration, or release-upgrade guide changed without a new requirement.
- [ ] Run focused tests throughout, then the complete Python and repository gate set.
- [ ] Build and install the wheel in an isolated environment and smoke the affected CLI paths.

### Phase 3 definition of done

- Permanent capability and operator docs match implemented behavior and provider support.
- All static, unit, integration-mark-excluded, packaging, and repository gates pass.
- Residual scans find no optional native hook or claim that Proxmox lacks recovery execution.
- The runtime diff contains no pyinfra dependency or speculative framework layer.

## Phase 4: Review, live validation, and closeout

- [ ] Obtain clean private agentworks-reviewer, Muntz, and cold correctness/security passes on one
      exact runtime head; apply every material authorized correction and rerun affected gates.
- [ ] Load the integration-testing and agw-test-env skills, prepare an operator-reviewed live
      charter, and validate one full native platform plus Proxmox QGA on expendable resources.
- [ ] On Proxmox, prove create-time release attestation, Tailscale-independent rejoin and rekey,
      canonical no-fallback, platform-shell refusal, timeout honesty, and canary-secret absence.
- [ ] Record any unavailable Proxmox live evidence for authenticated operator disposition rather
      than satisfying acceptance from mocks.
- [ ] Complete the separately authorized published runtime feedback/fix rounds, if any.
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
- The ready runtime PR is explicitly outside 0.18.0 and awaits operator merge disposition.
