# Implementation Plan: Session Batch Stop Parallelism

- Status: Published implementation feedback round 1 of 2
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [session-teardown-lld.md](./session-teardown-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Source baseline: `3641ea8c0cbc7c6389535099b9678932aa972f66`
- Delivery: one design-and-implementation PR for issue #730

## Delivery Rules

- The PR carries the complete design first, then implementation after the design feedback loop
  converges. It remains draft during active mutation and becomes ready only at a complete exact-head
  handoff.
- The operator authorized up to three published feedback/fix cycles for the design and up to three
  for the final implementation under the standard process. Each published handoff receives the
  standard minimum feedback window. A no-change round ends its loop.
- The November resumption supersedes the remaining implementation allowance with two feedback/fix
  rounds. Each new round waits for the separate integration-test session's report, which can take
  several hours. Round 1 began after the report on `daa4559a1` arrived.
- Before changing a ready head, remove `review-requested` and return the PR to draft. Reapply the
  signal only after the coherent new head passes the required gates and reviews.
- Every material finding receives an explicit accept, modify, decline, or defer disposition. A
  divergent contract or non-converging finding stops for operator direction.
- The implementation lead does not merge its own PR.
- PR #764's database-use lock is an implementation prerequisite. Rebase onto its merged result
  before implementation so restore exclusion has one authority rather than being recreated here.

## Full Implementation Gates

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

The final head also receives installed-wheel CLI smoke tests, structural stale-surface scans,
private project and Muntz review, cold correctness/security review, deterministic website builds if
site content changes, and capability-appropriate live validation. Tests assert behavior and state,
not authored prose.

## Requirement Traceability

| Requirement | Design authority                       | Planned proof                                   |
| ----------- | -------------------------------------- | ----------------------------------------------- |
| R1, R3      | FRD CLI/bound; HLA coordinator         | CLI regression and worker-bound tests           |
| R2          | HLA concurrency unit/collision gate    | overlap, collision, and legacy tests            |
| R4, R7      | HLA ownership/reconciliation; LLD      | thread-affinity and completion tests            |
| R5, R6      | teardown LLD remote state machine      | identity, timeout, force, and no-retry tests    |
| R8          | HLA output owner; LLD coordinator      | recording output-handler tests                  |
| R9          | HLA/LLD interruption controller        | queued/running/repeated-interrupt tests         |
| R10         | existing boundary and compare-and-set  | batch-gate and stale-result tests               |
| R11         | FRD non-goal; HLA rejected alternative | structural scope review                         |
| R12         | this plan                              | full gates, private review, and live validation |

## Phase 0: Design Checkpoint

- [x] Refresh the effort on `main` at `3641ea8c0cbc7c6389535099b9678932aa972f66`.
- [x] Inventory batch and named stop, start/restart, shared teardown, SQLite ownership, transport
      timeout/retry behavior, output, tmux socket topology, and ADR 0015.
- [x] Research primary Python futures, SQLite, tmux, and sidecar-lock contracts.
- [x] Draft the FRD, HLA, teardown LLD, migration strategy, prior-art research, and this plan in one
      coherent artifact set.
- [x] Run documentation lint, spelling, link, locked-SDD, Rulesync, and diff checks.
- [x] Obtain initial clean private project and Muntz design reviews and incorporate material
      findings.
- [x] Obtain fresh private project and Muntz reviews after the current-main and PR #764 dependency
      audit.
- [x] Publish the complete design checkpoint as a draft PR with `review-requested`.
- [x] Audit implementation readiness against PR #764 head `5199bae9f` and close the canonical-VM
      plan field and partial-submission ownership gaps before implementation.
- [x] Complete two design feedback/fix cycles, within the three authorized: round 1 incorporated
      published and implementation-readiness findings; round 2 completed with a clean Muntz
      confirmation and no further findings.
- [x] Record design convergence at `0275edc1e` before implementation begins.

### Phase 0 Definition of Done

- The safe concurrency unit, worker bound, state owner, evidence shape, output owner, transport
  policy, interruption behavior, and legacy disposition are explicit.
- Start/restart and single-session optimization receive a disposition without speculative framework.
- Every requirement has objective implementation proof.
- No material design or complexity finding remains.

## Phase 1: Extract Complete-Fingerprint Teardown

- [x] Rebase onto the merged PR #764 database-use lock and retain its shared writable-lifetime and
      exclusive restore semantics unchanged.
- [x] Add atomic stopped-state compare-and-set against the complete prepared runtime identity.
- [x] Add one dedicated-only immutable complete-fingerprint plan in a session-domain teardown
      module.
- [x] Extract database-free remote execution while keeping reconciliation on the invoking thread.
- [x] Partition incomplete dedicated rows into the existing synchronous path and prove their
      missing-start-ticks persistence still precedes `kill-server`.
- [x] Add collision checks for duplicate socket or VM/boot/PID process keys among concurrent plans.
- [x] Keep named stop, restart, direct and cascading deletion on the synchronous dispatcher and run
      their existing regression suites.
- [x] Prove socket validation, exact tmux targeting, force recovery, absence verification, and
      stopped persistence remain unchanged.
- [x] Reconcile main's durable session UUID and managed-run identity into the prepared plan and
      stopped-state compare-and-set, with replacement-row and run-advance conflict coverage.

### Phase 1 Definition of Done

- There is still one teardown authority.
- Remote execution can run without a database or output handler.
- Stale persistence fails atomically without adding a global lifecycle lock.
- All current callers pass their existing regression suites while behavior remains serial.

## Phase 2: Concurrent Dedicated Batch Stop

- [x] Prepare complete-fingerprint dedicated plans after the existing batch safety gates and leave
      incomplete dedicated and legacy rows on the current synchronous dispatcher.
- [x] Preserve the empty-selection no-op and skip executor construction for an empty concurrent plan
      set, including serial-only and preparation-failure batches.
- [x] Construct one 10-second, one-attempt transport per dedicated plan.
- [x] Add the fixed, maximum-eight dedicated teardown executor with a pre-mutation submission gate,
      one mapped future per plan before release, and untouched accounting for a partial submission.
- [x] Consume futures in completion order and reconcile successful stopped state before labeled
      output.
- [x] Emit a compact heartbeat after each five-second quiet interval.
- [x] Keep incomplete dedicated and legacy teardown serial after concurrent reconciliation.
- [x] Preserve sibling progress and final aggregate failure when one plan fails.
- [x] Leave start and restart batch launch loops serial and free of the new executor.

### Phase 2 Definition of Done

- Instrumented tests prove real overlap for dedicated sessions, including same-VM work.
- Serial compatibility work begins only after the concurrent lane drains.
- No worker accesses SQLite, global output, interaction, secrets, activation, or shared config.
- A per-session transport failure does not suppress sibling success.

## Phase 3: Interruption and Failure Accounting

- [x] Cancel queued futures on first interrupt.
- [x] Report and drain bounded in-flight work, reconciling every normally returned future.
- [x] Repeat the reconciliation notice after later interrupts without pretending Python can
      terminate running thread work.
- [x] Prove worker escape, timeout, incomplete-row serialization, cancelled-future accounting, and
      partial-submission abort at both CPython ownership gaps: post-enqueue/pre-future and
      post-thread-start/pre-registration.
- [x] Make reconciliation retry-safe when an interrupt lands after SQLite committed the desired
      state.
- [x] Preserve the current final aggregate command error for ordinary per-session failures.

### Phase 3 Definition of Done

- First interrupt never discards an available remote mutation result.
- Repeated interrupts do not abandon or misreport in-flight work.
- Every ordinary failure leaves the strongest safe database evidence.

## Phase 4: Collateral and Exact-Head Validation

### November resumption

The September validation below records the implementation as it then stood. Work resumed on
2026-11-08 after main advanced to `c8d4dfb3c` and version 0.19.0 released. The FRD's release target
records the original schedule; this effort adds no version-dependent compatibility or deprecation.

- [x] Merge current main, preserving running-restart consent, selected-session identity checks,
      native mutation guards, and legacy teardown authorization.
- [x] Audit transport-take2 requirements R4, R9, and R10: current transport consumers remain in
      place until migration, with no dependency on the replacement stack. Migration must preserve
      independent worker ownership, privilege selection, finite calls, and no mutation retries.
- [x] Complete fresh full gates, installed-wheel validation, and private reviews after resumption.
- [x] Record reachable live coverage and any remaining VM or workstation gaps.

#### Live validation checkpoint

The final executable head passed 10,251 Python tests with 11 skipped. Ruff, formatting, mypy across
884 files, repository lint, locked-SDD validation, Rulesync, typer isolation, 160 Python website
tests, 103 Node website tests, and deterministic website builds passed.

At `51b717218`, an installed wheel on Linux drove the real coordinator against independent local
tmux servers and isolated SQLite through a subprocess-backed target. Ten batches stopped 120
servers; each result was checked against process incarnation, socket removal, and persisted stopped
fields. Ten mixed batches stopped two healthy siblings each and refused a deliberately incorrect
start-time identity without changing its live runtime or persisted fingerprint. Peak observed
concurrency was eight; twelve-session batches took 0.055 to 0.062 seconds. These local measurements
do not calibrate the ten-second network-call timeout.

The initial drive exposed a process exiting between its presence probe and stat read. The shared
absence proof now rechecks presence once after an unreadable stat, accepting only explicit absence.
Present and unknown observations still fail closed, and no mutation is retried. Boundary tests and
the rebuilt-wheel drive cover the correction; all three private review lanes are clean.

The isolated installed CLI also passed config initialization, empty batch stop, and names-only
listing. All disposable processes, sockets, homes, keys, and databases were independently checked
after cleanup. Operator state was untouched. Remote SSH, multiple actual VMs, Windows, live
interruption, compatibility lanes, and supported-environment timeout calibration remain gaps because
this workspace has no configured test inventory or workstation bed. At that checkpoint, publication
was blocked by automatic approval review requiring destination-specific authorization.

#### November 9 main integration

The operator authorized normal branch publication on 2026-11-09. Commit `391216dc1` was pushed and
the PR body updated. A subsequent fetch found main at `438c4546c`, including stronger session
ownership, partial-runtime recovery, and native-socket cleanup checks. GitHub reports conflicts and
cannot run the PR's normal merge-based CI until the integration is complete. CodeQL passed on the
published head; the earlier local evidence above remains evidence for its recorded baseline.

- [x] Integrate `438c4546c`, preserving its launch-attempt ownership and native-socket checks in
      both synchronous and concurrent dedicated teardown.
- [x] Revalidate changed-boot recovery for uncertain launches and stopped-row socket conflicts.
- [x] Repeat full gates and private review on the integrated implementation, then publish it.
- [x] Recheck hosted CI and record the remaining live coverage before the final handoff.

The installed-wheel retest at `315c1cc64` reproduced a conservative shutdown-timing failure:
`kill-server` returned successfully while the exact process remained briefly alive, then the process
exited before coordinator return. The follow-up correction adds bounded read-only post-kill
observation grace, never another destructive attempt. Private correctness and complexity review
judged this part of the shared teardown completion contract rather than transport migration.

- [x] Validate delayed exit, persistent liveness, and unknown observation during post-kill grace,
      including one mutation only and no cleanup before proven absence.

#### November implementation handoff and first feedback round

Head `daa4559a1`, incorporating main `438c4546c`, was published ready on November 9 with all hosted
checks green. The integrated baseline had 11,448 local full-suite passes and 11 skips, plus one
unchanged credential-helper process-group cleanup failure that passed on isolated rerun. The grace
correction passed 72 focused tests and all three private review lanes. An installed wheel stopped 36
real local tmux servers, verifying one kill each, process/socket absence, stopped SQLite state, and
preservation of mismatched runtimes in mixed batches.

The separate integration-test session reported on that exact head. PowerShell 5.1 and 7.6.9 each
stopped 12 selected sessions across two real Debian guests while preserving an unselected sentinel.
A real ten-second read-only worker timeout preserved its failed runtime and database identity while
nine siblings stopped. That supports the current ten-second, one-attempt policy. Sampled peak SSH
concurrency was eight, with attribution gaps, not a continuous upper-bound proof. A Linux multi-VM
drive stopped ten sessions but did not send its planned Ctrl-C; interruption, replacement races, and
legacy/incomplete compatibility remain source/unit coverage. The tester's initial full gates had
fixture failures; all affected tests passed after fixture correction, without a full corrected suite
rerun. The incomplete Windows private-key scan remains an explicit cleanup-evidence limit.

- [x] Receive and critically assess the integration report before beginning a feedback/fix round.
- [ ] Consolidate complete dedicated execution and fenced synchronous reconciliation while
      preserving stopped, uncertain-launch, and incomplete-fingerprint compatibility preparation.
- [ ] Fence incomplete fingerprint acquisition against the original row, run, and nullable runtime
      snapshot before writing; prove replacements during remote launch checks remain unchanged.
- [ ] Simplify worker evidence and coordinator drain branches without weakening submission
      ownership, primary-error propagation, or interrupted-persistence retry.
- [x] Resolve retry-factory feedback against the independent one-attempt requirement in R6.
- [ ] Run the full gates and fresh private project, complexity, and cold correctness reviews on the
      round's exact result, then publish a new ready handoff for the separate tester.

The retry-factory deletion suggestion was assessed against R6 rather than today's equal defaults.
The independently named attempt constant and explicit factory forwarding remain: five factory lines
feed the existing SSH retry setting. Removing them couples destructive work to the shared default;
per-helper overrides or a transport wrapper add more machinery. Independent complexity review
confirmed this is the smallest shape that enforces the accepted policy.

Cold review of the first round checkpoint reproduced an acquisition race in the touched incomplete
path. Its by-name fingerprint write could overwrite replacement state before the later refetch
check, and a runtime-only replacement could be erased without a conflict. The correction makes the
pre-kill identity upgrade conditional too, retaining serial launch proof, persistence, refetch, and
fresh capture rather than adding partial worker plans or a lifecycle lock.

Fresh-wheel startup also exposed an incidental dependency compatibility failure: resolving Typer
0.29.0 removes the `click_type` option parameter used by unchanged CLI code. The same wheel's
version and help commands pass with the repository's locked Typer 0.27.3. Dependency declarations,
the lock, and CLI sources match main and are not changed by this teardown effort. The failed
diagnostic wrote a standard error log in the ambient profile; no database or runtime command ran.
The local empty-batch CLI smoke was refused by automatic approval review before execution, so it
remains unrun rather than being described as a pass.

- [x] Update active command reference and session lifecycle/status guidance.
- [ ] Add `locked.md` from the implemented exact head and run locked-SDD validation.
- [x] Run full Python, lint, Rulesync, website, and installed-wheel gates.
- [x] Obtain fresh private project-values, Muntz, and cold correctness/security reviews on the exact
      implementation head.
- [x] Run an isolated shipped-CLI drive with no ambient operator state.
- [x] Run operator-approved live validation against disposable dedicated sessions on one and
      multiple VMs when the environment permits.
- [ ] Complete the November two-round feedback allowance, waiting for each separate integration
      report before any next round and stopping on convergence.

### Phase 4 Definition of Done

- Permanent documentation, implementation, tests, and locked record agree.
- Full local and CI gates are green at the handed-off head.
- Live evidence verifies runtime and database state, not elapsed time alone.
- No material correctness, security, complexity, or integration finding remains.

## Residual Work Deliberately Deferred

- Parallel start/restart orchestration is tracked by issue #778 and requires its own SDD if
  measurements justify it.
- Measured named-stop latency and any compound remote teardown are tracked by issue #777 and require
  separate safety analysis.
- Operator-configurable concurrency requires evidence from the fixed-bound implementation.
- systemd/cgroup process containment remains issue #715.
