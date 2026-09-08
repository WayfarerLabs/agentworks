# Low-Level Design: Concurrent Session Teardown

- Status: Draft for design review
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Module Shape

Create `agentworks.sessions.manager._teardown` as the single home for dedicated-session teardown
preparation, remote execution, reconciliation, and batch coordination. The existing manager package
re-exports only the private seams needed by current lifecycle and deletion callers.

`_lifecycle.py` continues to own command-level selection, VM boundaries, status policy, start, and
restart. The existing `_teardown_legacy_session` stays synchronous and does not enter the new value
or executor model. `_teardown_session` remains the one dispatcher: legacy rows take that existing
path, while dedicated rows use the extracted prepare, execute, and reconcile phases.

## Database Boundary

PR #764 is an implementation prerequisite. It adds a database-use sidecar whose shared lock is held
for every writable `Database` lifetime and whose exclusive lock is held by restore across live-file
replacement. Batch stop keeps its writable database open through worker reconciliation, so the live
file cannot be replaced beneath the command.

No new session-runtime lock or token is added. Existing lifecycle commands do not promise
cross-process serialization, and eligible workers retain the current immediate fingerprint
capture-to-kill sequence. The only new persistence gap is between verified remote completion and the
coordinator's stopped-state update; atomic compare-and-set fences that gap.

## Pre-mutation Collision Gate

After preparation, batch stop indexes only the selected plans eligible for concurrent execution:

- `(vm_name, socket_path)` for every non-null dedicated socket; and
- `(vm_name, canonical_boot_id, positive_pid)` for every complete process identity.

Any key claimed by more than one concurrent plan produces a typed whole-batch failure listing the
conflicting sessions. Rows with missing or invalid fingerprint fields never enter these indexes or
the pool; they stay on the synchronous compatibility path. Unselected rows and serial rows cannot
create two overlapping workers and therefore do not add a new batch refusal.

Start ticks remain part of each plan and the worker's exact process-incarnation comparison. They are
not part of the collision key because two plans that claim the same live PID on one VM boot are not
safe to mutate concurrently merely because one stored start-ticks value is stale.

This gate runs after PID repair and before the first destructive submission.

## Dedicated Concurrent Plan

### `DedicatedTeardownPlan`

A frozen dataclass with value fields:

```python
@dataclass(frozen=True)
class DedicatedTeardownPlan:
    session_name: str
    socket_path: str
    stored_pid: int
    stored_boot_id: str
    stored_start_ticks: int
    target: Transport
    sudo: bool
    force: bool
```

Every identity field is complete and validated before construction. The values form the expected
identity for later compare-and-set persistence. Each plan owns a distinct transport instance. No two
concurrently running plans share it.

## Preparation

`prepare_concurrent_dedicated_teardown(db, session, *, target, target_owns_session, force)` runs
only on the invoking thread.

1. Reject the stopped sentinel as no work.
2. Require a dedicated socket and validate it as an exact managed path through the current database
   graph.
3. Require a positive persisted PID, canonical boot ID, and positive start ticks.
4. If any fingerprint field is missing, classify the row for synchronous teardown instead of
   constructing a concurrent plan.
5. Construct the plan with `sudo=not target_owns_session`.

Preparation performs no remote mutation. An invalid stored value retains the current per-session
failure behavior; a merely incomplete fingerprint is not an error and stays serial.

## Dedicated Remote State Machine

`execute_dedicated_teardown(plan)` runs in a worker and never accesses global output or SQLite. It
uses the plan's transport without copying ambient context, since workers consume no context-bound
presentation or bootstrap service.

### Reachable fingerprint

1. Capture the tmux server fingerprint through the exact socket.
2. Require a present capture to contain a complete fingerprint.
3. Validate the observed boot ID.
4. Require observed PID, boot ID, and start ticks to equal the complete plan.
5. Invoke exact `kill-server` through the socket immediately after comparison.
6. Prove the fingerprinted server process absent using the complete plan, without rereading SQLite.
7. Remove the exact socket path and verify its absence.
8. Return normally as verified stopped.

The absence proof helper accepts explicit fingerprint values so it no longer needs a refreshed
database row after the kill.

### Unreachable or indeterminate fingerprint

If initial capture is absent or unknown, the worker preserves the current recovery ordering using
the complete plan. It first tries to prove the stored runtime absent regardless of `force`. If proof
succeeds, it removes and verifies the exact stale socket and returns normally. If proof cannot
establish absence, an unforced plan raises the current actionable broken-state wrapper; a forced
plan raises the more specific existing identity or connectivity failure. `force` changes the
error/selection policy, not whether safe proven-absence recovery is attempted.

The worker never kills a process by PID and never treats a transport failure as absence.

### Exception behavior

The worker does not wrap exceptions. `Future.result()` raises an ordinary per-session exception for
coordinator accounting. A `KeyboardInterrupt`, `SystemExit`, or other `BaseException` enters the
coordinator's abort-and-drain path before propagation.

## Synchronous Compatibility Path

Dedicated rows with a missing persisted fingerprint field remain on the current invoking-thread
path. In particular, when start ticks are missing, that path captures and validates a complete
fingerprint, persists it, rereads the session, and only then invokes `kill-server`. This preserves
the current persistence-failure and crash boundary without adding a staged concurrent protocol.

The existing legacy function stays on the invoking thread:

1. probe `tmux has-session -t =NAME` on the default server;
2. fail if presence is unknown;
3. when present, invoke `kill-session -t =NAME`;
4. probe until the exact session is absent; and
5. mark the row stopped through its existing main-thread database call.

Incomplete dedicated and legacy rows run one at a time after all concurrent futures are reconciled.
Ordinary failure is accumulated and the next serial row proceeds. Compatibility code does not learn
the concurrent plan model.

## Transport Construction

The batch caller creates a distinct admin transport for every prepared dedicated session. The
initial implementation supplies:

```python
default_timeout=10
```

The SSH transport already defaults to one attempt. The remote teardown code passes no wider timeout
or retry override. Live validation covers every teardown step on supported environments. The value
may be increased before design lock if evidence shows 10 seconds is too short; it remains one named
internal batch-mutation constant rather than a CLI or configuration contract.

Named stop and other synchronous teardown consumers use their existing transport instances and
timeout policy.

## Batch Coordinator

`execute_concurrent_dedicated_teardowns(plans, *, db)` accepts complete-fingerprint dedicated plans
only.

An empty plan sequence returns immediately without constructing an executor. This preserves the
serial-only lane and lets a batch containing only preparation failures continue to its normal
failure accounting.

1. Create a `ThreadPoolExecutor` with `min(8, len(plans))` workers.
2. Submit `execute_dedicated_teardown` once for every plan; no context copy is needed.
3. Map each future to its plan.
4. Use `wait(..., timeout=5, return_when=FIRST_COMPLETED)` so the invoking thread can emit a
   heartbeat after a quiet interval.
5. Call `future.result()` and collect an ordinary exception as that plan's failure.
6. On normal return, compare-and-set verified stopped state before labeled success output.
7. Repeat until every submitted future is completed or cancelled.
8. Shut down and return all failures.

An ordinary per-session `Exception` does not cancel siblings. A `BaseException` escaping a worker, a
broken executor, or another coordinator-level failure enters the abort-and-drain path. Sibling
mutations are drained and reconciled before failure propagates. The maximum active remote-task count
is eight, and every eligible session has exactly one future.

The heartbeat reports completed, active, and queued counts. A completion resets the quiet interval,
so fast batches emit only their ordinary session outcomes.

A completed future remains in coordinator bookkeeping until reconciliation and outcome rendering
finish. If `KeyboardInterrupt` lands during that main-thread work, interruption mode retries the
same database operation. Retry-safe compare-and-set recognizes a desired state that the interrupted
attempt already committed.

## Atomic Reconciliation

Add a database operation that updates session runtime only when the row still matches all prepared
runtime identity fields. It uses one `UPDATE ... WHERE name = ? AND socket_path IS ? AND pid IS ?`
plus the corresponding boot ID and start-ticks predicates, checks `rowcount == 1`, and commits
through the existing transaction policy. After zero affected rows, it reads the row once: an exact
match for the desired state means an earlier interrupted reconciliation already committed and is
idempotent success; any other or missing row is a typed conflict and remains untouched.

`reconcile_dedicated_teardown(db, plan)` runs on the invoking thread after a normally returned
future. It compare-and-sets from the plan's complete expected identity to the persisted socket,
stopped PID, null boot ID, and null start ticks.

The function does not overwrite declaration or harness state. Persistence precedes success output. A
compare-and-set conflict or other database error becomes that session's failure even if remote
teardown was verified, because remote success cannot be rolled back or honestly hidden.

Compare-and-set is the persisted-state fence against a concurrent command, programming error, or
changed or missing row in the open database. PR #764's shared database-use lock excludes first-party
live-database replacement while the writable lifecycle `Database` remains open; arbitrary external
file replacement remains outside Agentworks' guarantees.

## Interruption State Machine

The first `KeyboardInterrupt` that escapes future collection becomes the stored primary
interruption. The coordinator then:

1. invokes `future.cancel()` for every unfinished future;
2. reports that active teardowns are being reconciled;
3. continues the five-second wait loop for futures that were already running;
4. reconciles every normally returned future; and
5. re-raises the stored interruption after all running work finishes.

Cancelled futures produce no session failure because their mutation did not start. Those sessions
remain eligible for a future retry.

A later interrupt during reconciliation repeats the notice and returns to the wait loop. Python
cannot terminate running executor threads, and interpreter shutdown joins them. The command
therefore does not offer a fictional immediate escape. Finite per-call timeouts, a finite state
machine, and five-second heartbeats make the mandatory reconciliation wait visible and bounded.

## Integration With Existing Callers

### Named stop

Named stop keeps its synchronous dispatcher, output, and transport policy. The complete dedicated
branch may call the extracted database-free remote helper synchronously; the incomplete dedicated
branch retains its current pre-kill fingerprint persistence. It does not use an executor or gain the
batch timeout.

### Batch stop

Batch stop preserves current filter, relationship, VM, status, force, and unknown-state gates. It
partitions complete dedicated plans from incomplete dedicated and legacy rows, validates concurrent
plan collisions, announces the count, reconciles the concurrent lane, then runs the serial lane. It
raises the current aggregate `ExternalError` when any session failed.

### Create, start, restart, and deletion

Create, start, restart, and deletion keep their current orchestration. Any synchronous teardown they
invoke continues through the one dispatcher and never enters the concurrent coordinator.

## Tests

### Unit and service tests

- PR #764 shared database-use and exclusive restore-lock behavior remains unchanged;
- collision refusal for duplicate socket and complete fingerprint identities among concurrent plans;
- dedicated plan validation for admin, agent, force, and stopped rows;
- missing fingerprint fields select the synchronous compatibility path;
- synchronous missing-start-ticks persistence still precedes `kill-server`;
- successful reachable complete-fingerprint teardown;
- current unforced and forced unreachable-runtime behavior;
- exact socket cleanup and exact legacy session targeting;
- distinct transport identity per concurrent task;
- maximum eight active workers and same-VM overlap;
- empty concurrent-plan handling for serial-only and preparation-failure selections;
- serial incomplete-dedicated and legacy execution without the plan model;
- atomic and retry-safe compare-and-set, persistence-before-output, and persistence failure;
- sibling success under worker failure;
- five-second heartbeat behavior;
- first-interrupt queued cancellation and running reconciliation;
- repeated-interrupt reconciliation; and
- named stop plus create/start/restart and deletion regression coverage.

Runtime tests use SQLite's real thread guard, a thread-recording output handler, and controlled
transports to prove workers do not touch SQLite or output. Tests assert the behavior boundary
without pinning source syntax.

### Live Validation

In an authorized disposable environment:

1. create at least four dedicated sessions, including two on one VM when budget permits;
2. stop the batch and record elapsed time plus per-session status;
3. verify every exact tmux server and socket is absent;
4. verify the database reports stopped;
5. inject one safely recoverable per-session failure and prove siblings stop;
6. retry the failed selection successfully.

The test does not use production sessions or rely on timing alone. Instrumented unit overlap is the
deterministic concurrency proof; live elapsed time is supporting evidence.
