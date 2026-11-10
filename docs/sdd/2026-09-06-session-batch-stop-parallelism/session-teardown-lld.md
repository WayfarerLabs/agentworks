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

### PID-repair preparation

Before any native observation, `_repair_session_pid` copies the original seven-field
`SessionRuntimeSnapshot`. Its four writes use `compare_and_set_session_runtime_repair`: legacy/admin
learned boot, recovered fingerprint, dedicated learned boot, and verified stopped state. The desired
socket is explicit because legacy/admin stopped recovery can assign a dedicated address while the
expected socket remains null. The existing runtime-shape validator is shared with
`update_session_runtime`; no native ownership or absence authority comes from the snapshot.

The repair operation reuses the concrete conditional SQL primitive below. No match raises
`StateConflictError`, a kind-based `StateError` subtype. `ensure_pids_batch` propagates that
conflict before its existing best-effort catches, preventing stop, cascading deletion, and rehome
callers from continuing on the failed selection. `_ensure_pid` already propagates repair failures.
Ordinary ownership, absence, and connectivity refusals retain their existing caller handling.

The same propagation applies to `DatabaseDriverError`, preserving the original error. For example,
an enclosing `Database.transaction()` can hold a read snapshot while a second connection advances
the run, causing `SQLITE_BUSY_SNAPSHOT` before the conditional update reaches row-count handling.
Batch repair must not treat that failed write as a best-effort native refusal and continue with its
stale selection. No database write retry or error translation is added.

The fence covers the repair write, not the whole command. A successful repair still has a separate
subsequent refetch that can see later changes; this round does not introduce post-success command
serialization or migrate unrelated startup writes.

## Pre-mutation Collision Gate

After preparation, batch stop indexes only the selected plans eligible for concurrent execution:

- `(vm_name, socket_path)` for every non-null dedicated socket; and
- `(vm_name, canonical_boot_id, positive_pid)` for every complete process identity.

Any key claimed by more than one concurrent plan produces a typed whole-batch failure listing the
conflicting sessions. Rows with missing fingerprint fields stay on the synchronous compatibility
path. One shared raw-field-presence classifier excludes the stopped sentinel but does not validate
identity. Malformed non-null identities reach preparation and fail closed, never entering these
indexes or the pool and never gaining cleanup authority from classification. Unselected rows and
serial rows cannot create two overlapping workers and therefore do not add a new batch refusal.

Start ticks remain part of each plan and the worker's exact process-incarnation comparison. They are
not part of the collision key because two plans that claim the same live PID on one VM boot are not
safe to mutate concurrently merely because one stored start-ticks value is stale.

A concrete collision uses two different persisted managed socket names in the same validated owner
directory that alias one Unix socket and tmux server. Both workers can capture and validate that
server's same boot ID and PID before either invokes `kill-server`. Socket-string uniqueness misses
the overlap; the VM/boot/PID key refuses it before either worker mutates the shared process.

This gate runs after PID repair and before the first destructive submission.

## Dedicated Concurrent Plan

### `DedicatedTeardownPlan`

A frozen dataclass with value fields:

```python
@dataclass(frozen=True)
class DedicatedTeardownPlan:
    session_name: str
    session_uuid: str
    run_id: str | None
    vm_name: str
    socket_path: str
    stored_pid: int
    stored_boot_id: str
    stored_start_ticks: int
    target: Transport
    sudo: bool
    force: bool
```

Every required identity field is complete and validated before construction. `session_uuid` is the
durable logical identity and nullable `run_id` identifies the prospective managed launch current at
preparation. `vm_name` is the canonical database VM key resolved from the already-validated session
graph on the invoking thread; it is not derived from transport addressing. The values form the
expected identity for later compare-and-set persistence. Each plan owns a distinct transport
instance. No two concurrently running plans share it.

## Preparation

`prepare_dedicated_teardown(db, session, *, vm_name, target, target_owns_session, force)` runs only
on the invoking thread.

1. Accept only a complete-fingerprint row from the coordinator or synchronous preparation.
2. Require a dedicated socket and validate it as an exact managed path through the current database
   graph.
3. Require a positive persisted PID, canonical boot ID, and positive start ticks.
4. Copy the durable session UUID and nullable managed-run identity from the prepared row.
5. Copy the canonical `vm_name` from the already-validated batch relationship graph and construct
   the plan with `sudo=not target_owns_session`.

Preparation performs no remote mutation. The coordinator routes incomplete fingerprints to the
serial lane; if an ineligible row nevertheless reaches preparation, validation fails closed.

## Dedicated Remote State Machine

`execute_dedicated_teardown(plan)` runs in a worker and never accesses global output or SQLite. It
uses the plan's transport without copying ambient context, since workers consume no context-bound
presentation or bootstrap service.

### Reachable fingerprint

1. Apply the current native-socket check, refusing symlinks and existing non-socket objects, then
   capture the tmux server fingerprint through the exact socket.
2. Require a present capture to contain a complete fingerprint.
3. Validate the observed boot ID.
4. Require observed PID, boot ID, and start ticks to equal the complete plan.
5. Invoke exact `kill-server` through the socket immediately after comparison.
6. Prove the fingerprinted server process absent using the complete plan, without rereading SQLite.
7. Remove the exact socket path and verify its absence.
8. Return normally as verified stopped.

The absence proof helper accepts explicit fingerprint values so it no longer needs a refreshed
database row after the kill. An unreadable process stat may follow a process exiting after its
presence probe; one further explicit absence observation can prove that exit, while present or
unknown observations still refuse cleanup. Socket removal applies the current native-socket check
again and removes only a socket, never a symlink or another filesystem object.

Successful `kill-server` acknowledgment can precede process exit. After that one mutation, a
concrete still-present result permits further read-only absence observations at 50-millisecond
intervals within a one-second monotonic grace. No new observation is scheduled after that grace
expires. Unknown-state and transport errors propagate immediately. Each observation retains its
finite per-call transport timeout, so the grace is not a one-second bound on total teardown
duration. Socket cleanup still requires proven absence; a process surviving the grace retains its
persisted identity and produces failure.

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
fingerprint and verifies the recorded launch attempt before filling the missing identity. It
persists the result, rereads the session, and only then invokes `kill-server`. This preserves the
current persistence-failure and crash boundary without adding a staged concurrent protocol. The
reread row must retain the same session UUID, run ID, socket, and newly observed fingerprint. It
then enters the same complete-plan preparation as an originally complete row. The shared engine
captures the runtime again after preparation; the compatibility capture is not retained as
destructive authority across the persistence checkpoint.

The acquisition write itself is conditional on the original row's name, session UUID, nullable run
ID, socket, PID, boot ID, and missing start ticks. The database atomically fills the validated boot
ID and start ticks only if that snapshot still matches. A zero-row result raises a typed conflict
without changing a replacement. The subsequent reread separately refuses changes after the write; it
is not a substitute for the atomic acquisition predicate.

The compatibility path also preserves current-main recovery for uncertain launches: a positively
observed VM boot change proves the previous workload gone even without a known PID; an uncertain
launch on the current boot cannot gain cleanup authority. A stopped row with a live server at its
socket is a conflict, not authority to destroy that server.

The existing legacy function stays on the invoking thread:

1. probe `tmux has-session -t =NAME` on the default server;
2. fail if presence is unknown;
3. when present, invoke `kill-session -t =NAME`;
4. probe until the exact session is absent; and
5. conditionally mark the row stopped against the original snapshot copied before probing.

Incomplete dedicated and legacy rows run one at a time after all concurrent futures are reconciled.
Ordinary failure is accumulated and the next serial row proceeds. Compatibility code does not learn
the concurrent plan model.

## Transport Construction

The batch caller creates a distinct admin transport for every prepared dedicated session. The
initial implementation supplies:

```python
default_timeout=10
```

The values deliberately match the shipped read-only guest-observation timeout and attempt constants,
and eight likewise matches its worker ceiling. The remote teardown code passes no wider timeout or
retry override. Live validation covers every teardown step on supported environments. The value may
be increased before design lock if evidence shows 10 seconds is too short. Teardown keeps separately
named mutation constants because it cannot share the read-only helper's cancellation behavior.

Named stop and other synchronous teardown consumers use their existing transport instances and
timeout policy.

## Pre-mutation Submission Gate

The concurrent coordinator owns one batch-local gate with two thread-safe signals: aborted and
released. The gated worker wrapper waits for released, reads aborted, and returns without calling
the remote helper when aborted is set. The coordinator is the only writer and sets aborted before
released on every partial-submission exit. The gate carries no plan, result, database, transport, or
output state.

After the complete future-to-plan map exists, the coordinator records that boundary before setting
released without aborted. An exception at or after that release boundary therefore cannot create an
untracked mutation: every plan already has a mapped future and enters the ordinary drain and
reconciliation state machine.

## Batch Coordinator

`execute_concurrent_dedicated_teardowns(plans, *, db)` accepts complete-fingerprint dedicated plans
only.

An empty plan sequence returns immediately without constructing an executor. This preserves the
serial-only lane and lets a batch containing only preparation failures continue to its normal
failure accounting.

1. Create a batch-local submission gate and a `ThreadPoolExecutor` with `min(8, len(plans))`
   workers.
2. Submit a gated wrapper once for every plan in deterministic plan order; no context copy is
   needed. The wrapper waits and cannot call `execute_dedicated_teardown` before the gate resolves.
3. As each `submit` returns, map that future to its plan. Only after every future is mapped, mark
   the map complete and release the gate for execution.
4. If submission raises before the map is complete, mark the gate aborted before releasing it, call
   executor shutdown with queued-future cancellation and waiting enabled, and propagate the original
   failure or interruption after that call returns. A running wrapper observes abort and returns
   without remote mutation; queued work, including an enqueued work item whose future was never
   returned, is cancelled or takes the same no-mutation branch. Shutdown waits only for threads
   CPython registered with the executor. A thread interrupted after start but before registration
   may take the no-mutation branch after propagation.
5. Use `wait(..., timeout=5, return_when=FIRST_COMPLETED)` so the invoking thread can emit a
   heartbeat after a quiet interval.
6. Call `future.result()` and collect an ordinary exception as that plan's failure.
7. On normal return, compare-and-set verified stopped state before labeled success output.
8. Repeat until every submitted future is completed or cancelled.
9. Shut down and return all failures.

An ordinary per-session `Exception` does not cancel siblings. A `BaseException` escaping a worker, a
broken executor after gate release, or another coordinator-level failure enters the abort-and-drain
path. Successfully released sibling mutations are drained and reconciled before failure propagates.
Failure before a complete future map instead takes the submission-abort path, which guarantees that
every plan remains remotely and persistently untouched. The implementation does not infer that a
raised `submit()` call failed to enqueue: CPython enqueues a work item before thread adjustment and
before returning its future, and thread adjustment starts a worker before adding it to the
executor's tracked set. The gate covers both otherwise-unowned boundaries. It intentionally avoids
coordinator-owned thread tracking solely to wait for an already safe no-mutation branch. The maximum
active remote-task count is eight, and every plan has exactly one mapped future before remote
execution is authorized.

The heartbeat reports completed, active, and queued counts after the gate releases. A completion
resets the quiet interval, so fast batches emit only their ordinary session outcomes.

A completed future remains in coordinator bookkeeping until reconciliation and outcome rendering
finish. If `KeyboardInterrupt` lands during that main-thread work, interruption mode retries the
same database operation. Retry-safe compare-and-set recognizes a desired state that the interrupted
attempt already committed.

## Atomic Reconciliation

`SessionRuntimeSnapshot` is a frozen persistence-only value containing the reusable name, durable
session UUID, nullable managed-run ID, socket path, PID, boot ID, and start ticks. Serial
compatibility paths copy these primitives from the mutable row before their first native
observation. Complete plan reconciliation constructs the same snapshot on the invoking thread.
Nullable persistence evidence never enters `DedicatedTeardownPlan` or authorizes remote mutation.

One concrete database primitive updates the four runtime fields only when all seven original
identity fields still match in one conditional `UPDATE`, using null-safe predicates. It commits
through the existing transaction policy and returns whether one row matched. Identity acquisition
raises a typed conflict on no match. Stopped reconciliation instead reads the row once after no
match: an exact durable-identity and desired-runtime-state match means an earlier interrupted
reconciliation already committed and is idempotent success; any other or missing row is a typed
conflict and remains untouched. These are operation-specific policies, not flags on the primitive.

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

Before the complete future map exists, `KeyboardInterrupt` marks the submission gate aborted,
releases its waiting wrappers into the no-mutation branch, requests queued cancellation and executor
shutdown, and re-raises after shutdown returns. No reconciliation is needed because remote execution
was never authorized. A wrapper thread interrupted before executor registration may finish the
no-mutation branch after re-raise; the design promises remote safety, not a join the runtime cannot
guarantee.

After the complete future map exists, the gate is always released for execution. A
`KeyboardInterrupt` at that release boundary or during future collection becomes the stored primary
interruption. The coordinator then:

1. invokes `future.cancel()` for every unfinished future;
2. reports that active teardowns are being reconciled;
3. continues the five-second wait loop for futures that were already running;
4. reconciles every normally returned future; and
5. re-raises the stored interruption after all running work finishes.

Cancelled futures produce no session failure because their mutation did not start. Those sessions
remain eligible for a future retry.

Every plan in a partially submitted batch is counted as not started and remains eligible for retry.
This includes a callable already running in the executor but still waiting at the aborted gate and a
work item enqueued by a `submit()` call that raised before returning its future. Tests inject both
post-enqueue/pre-future and post-thread-start/pre-registration interruption and prove the remote
helper is never invoked, including after propagation.

A later interrupt during reconciliation repeats the notice and returns to the wait loop. Python
cannot terminate running executor threads, and interpreter shutdown joins them. The command
therefore does not offer a fictional immediate escape. Finite per-call timeouts, a finite state
machine, and five-second heartbeats make the mandatory reconciliation wait visible and bounded.

## Integration With Existing Callers

### Named stop

Named stop keeps its synchronous dispatcher, output, and transport policy. Complete dedicated rows
use the same preparation, database-free execution, and UUID/run/runtime compare-and-set as batch
workers. The incomplete dedicated branch retains its pre-kill launch proof and fingerprint
persistence, then reads the row again and joins that complete path. Named stop does not use an
executor or gain the batch timeout. Stopped-row and uncertain-launch special cases remain serial and
cannot turn a partial identity into a worker plan. Those compatibility cases and legacy teardown use
the same snapshot-fenced stopped-state database operation. A replacement during absence proof,
stale-socket cleanup, or exact legacy-session teardown cannot be overwritten by a by-name write.

Both complete paths share unreachable-runtime recovery: explicit absence proof permits exact socket
cleanup, an unforced identity or connectivity failure becomes actionable broken state, and forced
failure retains the original specific error. No error path guesses absence from unreadable state.

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
- collision refusal for duplicate socket or VM/boot/PID process keys among concurrent plans,
  including duplicate process keys with different stored start ticks;
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
- positive completion and two-connection row/run/runtime replacement during all three serial
  compatibility branches, with nullable expected fields and exact replacement preservation;
- all four PID-repair writes, including legacy/admin socket adoption, with independent-connection
  UUID-only, run-only, nullable-runtime-field changes and row removal; commit/rollback and desired
  runtime validation; named and batch conflict propagation before status or destructive dispatch;
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
