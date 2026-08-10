# Bootstrap fallback removal: implementation plan

## Definition of done

The platform create contract is complete-or-raise; no generic key-bearing fallback exists after
create; every current platform conforms; issue #471's Azure and AWS failure modes fail closed with
rollback; WSL2 retains its intentional primary bootstrap under platform ownership; permanent docs
match the code; all required reviews and gates are green.

## Prerequisite

- [x] Wait for PR #469 to merge, update this branch from `main`, and run its parallel default once
      before implementation so enforcement edits and gate evidence use the shipped test structure.

## Phase 1: behavior-preserving preparation on contract v1

- [x] Extract the generated-script executor into a WSL2-owned helper while the v1 Phase A caller
      remains in place; preserve private staging, cleanup, parsing, redaction, progress, database
      updates at the manager boundary, and observable logging.
- [x] Introduce the value-free bootstrap-progress protocol and have the existing concrete logger
      satisfy it without changing the v1 request/result contract.
- [x] Add focused tests for the extracted helper and logger lifecycle before changing platform
      ownership.

**DoD:** behavior and vm-platform contract version 1 are unchanged; WSL2 still bootstraps once
through Phase A with the same redacted log and database result; focused tests and strict mypy pass.

## Phase 2: fail closed under the existing result shape

- [x] Make Azure and AWS readiness exhaustion and cloud-init-wait failure raise inside create and
      trigger their existing rollback paths.
- [x] Make Proxmox bootstrap timeout, parsed failure, or missing Tailscale IP raise and roll back.
      Completion note: this deliberately removes the prior incomplete-success tolerance for missing
      cloud-init and guest-agent readiness; a Proxmox template must provide both prerequisites.
- [x] Add provider-shaped Azure and AWS regressions that inspect retained payloads, fixed stdin
      calls, staging calls, diagnostics, exception chains, and cleanup.
- [x] Add interrupt and second-interrupt/manual-removal coverage for Azure/AWS readiness waits and
      Proxmox bootstrap.

**DoD:** issue #471's four reproduced cases and Proxmox create-time bootstrap failures cannot return
an incomplete success or reach Phase A staging; create-time failures remain in platform rollback;
the still-v1 WSL2 primary path is unchanged; focused suites pass.

## Phase 3: atomic vm-platform v2 cutover

- [x] In one always-green commit, make the resolved Tailscale key and progress sink required in
      `ProvisionRequest`, remove all absent-key provider branches, remove
      `ProvisionResult.bootstrap_complete`, delete `BootstrapCompletion`, make
      `EphemeralTailscaleBootstrap.complete()` return only `str | None` or raise, remove the generic
      Phase A generated-script branch, and update manager call sites.
- [x] In that cutover, move logger construction before platform dispatch and preserve manager
      ownership, exactly-once close, primary-failure mapping, log-path guidance, and the complete
      Tailscale-plus-git-token redaction set; pin the intentionally new log on platform-create
      failures.
- [x] In that same commit, move the vm-platform descriptor and every in-tree implementation from
      contract version 1 to version 2; add exact registration rejection coverage for v1.
- [x] Add a structural regression proving no incomplete-bootstrap result, completion record, or
      generic Phase A generated-script branch remains; delete fallback-era completion-record tests.
- [x] Pin that post-create Tailscale SSH verification failure retains the secured `FAILED` VM
      behavior rather than reopening create rollback.
- [x] Add end-to-end coverage for successful join plus missing platform IP: Phase A rediscovers and
      records the IP without receiving the key as bootstrap input or touching staging.
- [x] Move WSL2's intentional primary bootstrap under `WSL2Platform.create()` ownership while
      preserving its accepted private staging, cleanup, redaction, and observable progress.
- [x] Simplify Lima to the required-key, complete-or-raise contract.
- [x] Add Lima/Azure/AWS fixed-stdin success and join-failure sentinel coverage for command text,
      returned output, logs, diagnostics, and exception chains; pin Azure/AWS rollback.
- [x] Add WSL2 and Proxmox sentinel coverage across success, bootstrap failure, cleanup failure,
      returned output, diagnostics, exception chains, and their accepted private staging cleanup.
- [x] Add interrupt and second-interrupt/manual-removal coverage for Azure/AWS readiness waits,
      WSL2's moved bootstrap, and Proxmox bootstrap.
- [x] In the same cutover commit, update the VM-platform README, plugin author example, general
      capability secret-delivery contract, nearby docstrings, vm-platform kind prose, and affected
      platform `TopicProse`; run guide catalog/rendering and inertness/safety coverage.
- [x] Confirm sample config, CLI command reference, and shell completions need no change because the
      operator CLI/config grammar is unchanged.

**DoD:** every shipped platform returns only after successful bootstrap; all failure paths stay in
their create-time readiness/bootstrap/interrupt rollback windows; #471's four reproduced cases
cannot reach key-bearing fallback staging; WSL2 output remains redacted in the same create log;
focused platform, manager, registration, and guide-projection suites pass; the tree is type-correct.

## Phase 4: full verification and closeout

- [x] Run focused tests for manager Phase A, WSL2, Lima, Proxmox, Azure, AWS, and provider-retention
      boundaries.
- [x] Run Ruff check and format, strict mypy, the full non-integration test suite, file lint,
      Rulesync drift, locked-SDD, and diff checks.
- [x] Run the required project review and fresh-eyes review; resolve every valid finding.
- [x] Record exact gate and review evidence, close issue #471 through the PR, and add `locked.md`
      only when the implementation and evidence are complete.

**DoD:** permanent docs and implementation agree; all gates and reviews are green; the SDD is locked
truthfully in the merge-ready PR.
