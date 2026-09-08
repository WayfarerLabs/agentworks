# Implementation Plan: Session Batch Stop Parallelism

- Status: Design checkpoint
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
- [ ] Complete up to three authorized design feedback/fix cycles.
- [ ] Record design convergence before implementation begins.

### Phase 0 Definition of Done

- The safe concurrency unit, worker bound, state owner, evidence shape, output owner, transport
  policy, interruption behavior, and legacy disposition are explicit.
- Start/restart and single-session optimization receive a disposition without speculative framework.
- Every requirement has objective implementation proof.
- No material design or complexity finding remains.

## Phase 1: Extract Complete-Fingerprint Teardown

- [ ] Rebase onto the merged PR #764 database-use lock and retain its shared writable-lifetime and
      exclusive restore semantics unchanged.
- [ ] Add atomic stopped-state compare-and-set against the complete prepared runtime identity.
- [ ] Add one dedicated-only immutable complete-fingerprint plan in a session-domain teardown
      module.
- [ ] Extract database-free remote execution while keeping reconciliation on the invoking thread.
- [ ] Partition incomplete dedicated rows into the existing synchronous path and prove their
      missing-start-ticks persistence still precedes `kill-server`.
- [ ] Add collision checks for duplicate socket or VM/boot/PID process keys among concurrent plans.
- [ ] Keep named stop, restart, direct and cascading deletion on the synchronous dispatcher and run
      their existing regression suites.
- [ ] Prove socket validation, exact tmux targeting, force recovery, absence verification, and
      stopped persistence remain unchanged.

### Phase 1 Definition of Done

- There is still one teardown authority.
- Remote execution can run without a database or output handler.
- Stale persistence fails atomically without adding a global lifecycle lock.
- All current callers pass their existing regression suites while behavior remains serial.

## Phase 2: Concurrent Dedicated Batch Stop

- [ ] Prepare complete-fingerprint dedicated plans after the existing batch safety gates and leave
      incomplete dedicated and legacy rows on the current synchronous dispatcher.
- [ ] Preserve the empty-selection no-op and skip executor construction for an empty concurrent plan
      set, including serial-only and preparation-failure batches.
- [ ] Construct one 10-second, one-attempt transport per dedicated plan.
- [ ] Add the fixed, maximum-eight dedicated teardown executor with a pre-mutation submission gate,
      one mapped future per plan before release, and untouched accounting for a partial submission.
- [ ] Consume futures in completion order and reconcile successful stopped state before labeled
      output.
- [ ] Emit a compact heartbeat after each five-second quiet interval.
- [ ] Keep incomplete dedicated and legacy teardown serial after concurrent reconciliation.
- [ ] Preserve sibling progress and final aggregate failure when one plan fails.
- [ ] Leave start and restart batch launch loops serial and free of the new executor.

### Phase 2 Definition of Done

- Instrumented tests prove real overlap for dedicated sessions, including same-VM work.
- Serial compatibility work begins only after the concurrent lane drains.
- No worker accesses SQLite, global output, interaction, secrets, activation, or shared config.
- A per-session transport failure does not suppress sibling success.

## Phase 3: Interruption and Failure Accounting

- [ ] Cancel queued futures on first interrupt.
- [ ] Report and drain bounded in-flight work, reconciling every normally returned future.
- [ ] Repeat the reconciliation notice after later interrupts without pretending Python can
      terminate running thread work.
- [ ] Prove worker escape, timeout, incomplete-row serialization, cancelled-future accounting, and
      partial-submission abort at both CPython ownership gaps: post-enqueue/pre-future and
      post-thread-start/pre-registration.
- [ ] Make reconciliation retry-safe when an interrupt lands after SQLite committed the desired
      state.
- [ ] Preserve the current final aggregate command error for ordinary per-session failures.

### Phase 3 Definition of Done

- First interrupt never discards an available remote mutation result.
- Repeated interrupts do not abandon or misreport in-flight work.
- Every ordinary failure leaves the strongest safe database evidence.

## Phase 4: Collateral and Exact-Head Validation

- [ ] Update active command reference and session lifecycle/status guidance.
- [ ] Add `locked.md` from the implemented exact head and run locked-SDD validation.
- [ ] Run full Python, lint, Rulesync, website, and installed-wheel gates.
- [ ] Obtain fresh private project-values, Muntz, and cold correctness/security reviews on the exact
      implementation head.
- [ ] Run an isolated shipped-CLI drive with no ambient operator state.
- [ ] Run operator-approved live validation against disposable dedicated sessions on one and
      multiple VMs when the environment permits.
- [ ] Publish exact-head evidence, mark the PR ready, and complete up to three authorized final
      feedback/fix cycles.

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
