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

| Requirement | Design authority                         | Planned proof                                   |
| ----------- | ---------------------------------------- | ----------------------------------------------- |
| R1, R3      | FRD CLI/bound; HLA coordinator           | CLI regression and worker-bound tests           |
| R2          | HLA concurrency unit/collision gate      | overlap, collision, and legacy tests            |
| R4, R7      | HLA ownership/reconciliation; LLD        | thread-affinity and partial-evidence tests      |
| R5, R6      | teardown LLD remote state machine        | identity, timeout, force, and no-retry tests    |
| R8          | HLA output owner; LLD coordinator        | recording output-handler tests                  |
| R9          | HLA/LLD interruption controller          | queued/running/repeated-interrupt tests         |
| R10         | session-runtime lock and compare-and-set | exclusion and stale-result tests                |
| R11         | FRD non-goal; HLA rejected alternative   | structural scope review                         |
| R12         | this plan                                | full gates, private review, and live validation |

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
- [ ] Obtain fresh private project and Muntz reviews after the current-main and PR #764 dependency
      audit.
- [ ] Publish the complete design checkpoint as a draft PR with `review-requested`.
- [ ] Complete up to three authorized design feedback/fix cycles.
- [ ] Record design convergence before implementation begins.

### Phase 0 Definition of Done

- The safe concurrency unit, worker bound, state owner, evidence shape, output owner, transport
  policy, interruption behavior, and legacy disposition are explicit.
- Start/restart and single-session optimization receive a disposition without speculative framework.
- Every requirement has objective implementation proof.
- No material design or complexity finding remains.

## Phase 1: Extract One Phased Teardown Authority

- [ ] Rebase onto the merged PR #764 database-use lock and retain its shared writable-lifetime and
      exclusive restore semantics unchanged.
- [ ] Generalize the existing SQLite sidecar migration lock into one session-runtime mutation lock
      with bounded migration wait and non-blocking lifecycle acquisition. Keep unrelated SQLite
      writers outside this lock.
- [ ] Route absent/version-zero initialization through the lock, recheck state after acquisition,
      and prove it cannot race restore into an absent destination.
- [ ] Centralize owner-private creation of a missing database parent before sidecar acquisition and
      retain absent-parent initialization and restore coverage.
- [ ] Route create, start, restart, stop, direct delete, and workspace, agent, and VM cascading
      teardown through the same cross-process session-runtime exclusion boundary.
- [ ] Route batch PID repair, workspace-rehome repair, and partial-create rollback through the lock
      token boundary so low-level runtime updates and row deletion cannot bypass exclusion.
- [ ] Inventory first-party live-database replacement paths and prove PR #764's database-use lock
      excludes them from every writable lifecycle `Database` lifetime.
- [ ] Bind each session-runtime lock token to the same canonical database path and live lock handle
      as the `Database` it authorizes; reject cross-database and released-handle tokens.
- [ ] Validate filter names before acquisition, then reload selected or named rows and required
      relationships under the lock before status or plan derivation.
- [ ] Add atomic runtime compare-and-set plus affected-VM socket and positive boot/PID collision
      checks that fail closed even when start ticks are missing or differ.
- [ ] Add dedicated-only immutable plan, probe, execution, and outcome values in a session-domain
      teardown module, reusing the existing tmux fingerprint value.
- [ ] Split main-thread preparation, pre-kill fingerprint checkpoint, and final reconciliation from
      database-free remote probe and execution.
- [ ] Persist missing-start-ticks refinement before destructive submission and prove persistence
      failure prevents the kill.
- [ ] Route named stop, restart, direct deletion, batch stop, and workspace, agent, and VM cascading
      deletion through the synchronous dispatcher before enabling concurrency while leaving legacy
      teardown unchanged.
- [ ] Cover `last_started_at` persistence and failed-launch cleanup in the session-runtime lock
      inventory without including that field in runtime compare-and-set identity.
- [ ] Prove socket validation, exact tmux targeting, force recovery, absence verification, and
      stopped persistence remain unchanged.

### Phase 1 Definition of Done

- There is still one teardown authority.
- Remote execution can run without a database or output handler.
- Cross-process lifecycle mutation is excluded and stale persistence fails atomically.
- All current callers pass their existing regression suites while behavior remains serial.

## Phase 2: Concurrent Dedicated Batch Stop

- [ ] Prepare dedicated plans after the existing batch safety gates and leave legacy rows on their
      current synchronous helper.
- [ ] Preserve the pre-lock empty-selection no-op and skip executor construction for an empty
      dedicated plan set, including legacy-only and preparation-failure batches.
- [ ] Construct one 10-second, one-attempt transport per dedicated plan.
- [ ] Add the fixed, maximum-eight dedicated probe/checkpoint/teardown executor.
- [ ] Consume probe outcomes in completion order, persist required refinement, then submit that
      session's destructive phase.
- [ ] Consume teardown outcomes in completion order and reconcile each before labeled output.
- [ ] Emit a compact heartbeat after each five-second quiet interval.
- [ ] Keep legacy exact-session teardown serial after dedicated reconciliation.
- [ ] Preserve sibling progress and final aggregate failure when one plan fails.
- [ ] Leave start and restart batch launch loops serial and free of the new executor.

### Phase 2 Definition of Done

- Instrumented tests prove real overlap for dedicated sessions, including same-VM work.
- Legacy work never overlaps another legacy mutation.
- No worker accesses SQLite, global output, interaction, secrets, activation, or shared config.
- A per-session transport failure does not suppress sibling success.

## Phase 3: Interruption and Failure Accounting

- [ ] Cancel queued futures on first interrupt and stop submitting destructive follow-up work.
- [ ] Report and drain bounded in-flight work, reconciling every returned outcome.
- [ ] Repeat the reconciliation notice after later interrupts without pretending Python can
      terminate running thread work.
- [ ] Prove pre-kill persistence failure, worker escape with durable complete identity, timeout,
      partial fingerprint, and cancelled-future accounting.
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

- Parallel start/restart orchestration requires its own issue and SDD if measurements justify it.
- Compound remote teardown for named-stop latency requires separate safety analysis.
- Operator-configurable concurrency requires evidence from the fixed-bound implementation.
- systemd/cgroup process containment remains issue #715.
