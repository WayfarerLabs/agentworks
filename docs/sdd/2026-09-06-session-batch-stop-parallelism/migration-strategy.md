# Session Batch Stop Parallelism: Migration Strategy

- Status: Draft for design review
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [session-teardown-lld.md](./session-teardown-lld.md)

## Migration Objective

Replace serial dedicated teardown inside `session stop --all` with bounded concurrent remote work
without changing persisted data, command grammar, capability contracts, or the safety meaning of a
stopped session.

The implementation targets 0.19.0. There is no deprecation cycle because no public spelling or
machine field changes.

## Current State

At source baseline `3641ea8c0cbc7c6389535099b9678932aa972f66`:

- batch stop performs VM gating and status observation before mutation;
- it creates one admin transport per VM and reuses it across selected sessions;
- `_execute_stop` calls `_teardown_session` serially;
- `_teardown_session` mixes remote probes and mutations with SQLite writes;
- missing start ticks are persisted before the current reachable-runtime kill;
- dedicated sessions use independent persisted tmux socket paths;
- legacy rows may share the default tmux server;
- runtime persistence updates by name without comparing prepared identity;
- named stop, restart, and deletion reuse the same teardown authority; and
- batch start and restart remain serial orchestration with interaction and shared-file mutation.

PR #764 is a prerequisite. Its database-use sidecar gives every writable `Database` a shared
lifetime lock and gives restore an exclusive replacement lock. This effort retains that boundary and
adds no second lock.

## Cutover Sequence

### 1. Add state fencing and eligibility

Add atomic stopped-state compare-and-set for a complete prepared runtime identity. Partition
selected rows after the current gates: only validated dedicated rows with complete fingerprints are
eligible for concurrency. Missing-fingerprint dedicated rows and legacy rows remain serial. Refuse
duplicate socket or VM/boot/PID process keys among concurrent plans before submission, regardless of
whether duplicate process keys carry different stored start ticks.

### 2. Extract value-based dedicated teardown

Introduce one immutable complete-fingerprint dedicated plan. Extract the database-free remote phase
from the current dispatcher and keep SQLite reconciliation on the invoking thread. Preserve the
incomplete dedicated branch's current pre-kill start-ticks persistence and the untouched legacy
helper.

At this point behavior remains serial. Existing teardown regression tests must be green.

### 3. Add batch-only transport policy

Create one transport per dedicated batch task with an initially 10-second finite default timeout and
the existing one-attempt policy. Confirm or adjust the value from live teardown evidence before
lock. Keep named and cascading transports unchanged.

### 4. Add the dedicated concurrent lane

Submit each complete-fingerprint plan once to the fixed worker pool. Consume futures in completion
order and compare-and-set successful stopped state on the invoking thread. Run incomplete dedicated
and legacy rows through the existing synchronous dispatcher afterward.

### 5. Add interruption reconciliation and heartbeat

Implement queued cancellation, first-interrupt draining, repeated-interrupt reconciliation, and a
five-second quiet-wait heartbeat before considering the concurrent path complete.

### 6. Update permanent collateral

Update the active command reference and session lifecycle guide to say that batch stop overlaps
independent dedicated teardowns, remains partially successful, and treats interruption as a
reconciliation boundary. Historical locked SDDs remain historical. The final locked record for this
effort captures the implemented head after live validation.

## Compatibility

| Surface                        | Before 0.19                 | 0.19 result                  |
| ------------------------------ | --------------------------- | ---------------------------- |
| `session stop NAME`            | synchronous exact teardown  | unchanged                    |
| `session stop --all [filters]` | serial exact teardown       | bounded complete-row overlap |
| `session start --all`          | serial                      | unchanged                    |
| `session restart --all`        | serial                      | unchanged                    |
| legacy shared-server stop      | serial exact `kill-session` | unchanged                    |
| schema and runtime fingerprint | current persisted fields    | unchanged                    |
| harness and VM capability APIs | version 1                   | unchanged                    |
| Python runtime dependencies    | current set                 | unchanged                    |

Output ordering for per-session batch lines may change from selection order to completion order.
Every line remains self-identifying, and the final aggregate success/failure contract is unchanged.

## Rollback

The code change is rollback-safe because no schema or data shape changes. Reverting to the serial
implementation reads every runtime row written by the concurrent implementation normally.

A rollback during an active command is not supported. Operators must let the command reconcile and
exit before changing versions.

## Mixed Runtime State

The database may contain dedicated and legacy rows together. The implementation does not perform a
bulk migration:

- complete-fingerprint dedicated rows enter the concurrent lane;
- incomplete dedicated and legacy rows retain the serial compatibility lane; and
- a later successful start or restart may migrate a legacy row through the existing lifecycle
  behavior.

No release ordering or coordinated VM-side rollout is required.

## Risk Controls

### A worker writes through SQLite

Control: worker APIs accept no database. Reconciliation tests use SQLite's same-thread restriction
and a recording database boundary.

### Concurrent output becomes unreadable

Control: workers emit nothing. The coordinator emits one session-labeled outcome after persistence.
It emits a compact heartbeat after each five-second interval without a completion.

### A transport timeout leaves an uncertain mutation

Control: one attempt only, no blind retry, fail closed, and direct the operator to status inspection
and an idempotent retry. Concurrent plans already contain complete persisted fingerprints.

### Shared tmux state is mutated concurrently

Control: a pre-mutation collision scan proves unique socket and VM/boot/PID process keys among
concurrent plans. Start ticks remain part of each worker's exact incarnation check, not evidence
that two plans claiming one live PID are independent. Only complete-fingerprint dedicated plans
enter the pool. Incomplete dedicated and legacy work remains serial.

### A concurrent lifecycle command replaces the prepared runtime

Control: the worker repeats the current immediate capture, complete identity comparison, and
destructive use without a database or queueing boundary between them. Final persistence
compare-and-sets the complete prepared identity and recognizes an already-committed desired state
after an interrupted attempt. PR #764's database-use lock prevents live-file replacement while the
writable command remains open.

### Interrupt loses successful work

Control: cancel only queued futures, reconcile running outcomes after the first interrupt, repeat
the notice after later interrupts, and bound remote calls. Python cannot safely abandon running
thread work.

### Start or restart inherits an unsafe generic pool

Control: the concurrent coordinator is named and typed for dedicated session teardown. Launch
operations remain serial and outside its API.

## Completion Conditions

The migration is complete when the exact implementation head has passed full gates, private design
and code review, Muntz review, cold correctness/security review, shipped-CLI isolation, and approved
live teardown validation. The PR becomes ready only after that evidence is published. The
implementation lead does not merge it.
