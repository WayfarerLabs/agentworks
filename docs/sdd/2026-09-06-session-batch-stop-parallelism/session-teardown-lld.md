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

## Database Mutation Lock

Generalize the existing migration lock into one database mutation lock at a neutral database
boundary. It retains the existing dedicated SQLite sidecar beside the resolved state database and
acquires exclusion with `BEGIN IMMEDIATE`. The sidecar is separate from live state, so the lock may
span remote work without holding a transaction on the operator database. The connection owns the
lock; rollback and close release it, and process exit closes the connection without leaving a stale
marker.

Before opening the sidecar, the helper establishes a missing database parent with the current
`mkdir(parents=True, mode=0o700, exist_ok=True)` policy and translates filesystem failure to the
existing typed state/backup boundary. Initialization and restore therefore remain valid when both
the database and parent are absent. The implementation centralizes this preparation rather than
leaving later callers to create the directory after lock acquisition.

`Database` retains its resolved path as read-only internal state so isolated databases and the
operator database cannot contend accidentally. The generalized helper retains an explicit timeout:
absent/version-zero initialization and schema migration use the current bounded blocking value,
while session lifecycle and live-database restore pass zero for non-blocking acquisition and
translate contention to the appropriate typed busy-state error. The existing sidecar path remains
stable so initialization, migration, and lifecycle operations from the same installation cannot
acquire independent locks.

Every mutating composition root holds one connection-backed lock handle and one explicit token for
nested internal calls. In-command workers do not reacquire it. No new locking dependency or second
failure-translation layer is introduced.

The shared lock boundary covers every session runtime mutation:

- create acquires before its final absent-name check and holds through initial runtime persistence;
- start and restart acquire before their final live-status classification and hold through launch
  persistence;
- named and batch stop hold through remote teardown and reconciliation; and
- direct and cascading delete hold through teardown and row removal;
- PID/fingerprint repair holds through every runtime-identity update, including batch repair called
  by workspace rehome; and
- partial-create rollback holds through `SessionNode.teardown` row deletion;
- `restore_backup` holds the same lock while it copies a validated backup into the live database;
  and
- absent/version-zero initialization and schema migration use the same sidecar lock.

The database-open path may inspect absence before acquisition to choose its path, but it MUST
reinspect under the lock before initializing. If restore or another initializer populated the path,
open continues from the newly observed schema instead of overwriting it. This closes the currently
unlocked first-open path and makes restore's exclusion meaningful even when the destination was
initially absent.

Low-level `_repair_session_pid`, `ensure_pids_batch`, teardown, runtime-update, session-row
deletion, and live-database replacement seams either require the held token or acquire the lock when
they are the composition root. Tests inventory every production call site and prove the token
reaches mutation. Read-only list, describe, status, and backup creation do not acquire it.

Batch stop validates filter names and performs a read-only candidate query before acquisition so bad
input and a structurally empty selection finish without contending. If candidates exist, it acquires
the lock, reapplies the original filters, and reloads every selected row plus its VM/workspace
relationships. The locked re-selection may also become empty and finish successfully. All status
gates and plans consume only those fresh locked rows. Named mutators also refetch their target row
and relationships after acquisition. The implementation splits the current combined filter-and-query
helper or adds an equivalent private seam so pre-lock rows cannot leak into mutation preparation.

## Pre-mutation Collision Gate

With the database-scoped lifecycle lock held, batch stop reads every session on each affected VM and
builds two indexes:

- `(vm_name, socket_path)` for every non-null dedicated socket; and
- `(vm_name, boot_id, pid, start_ticks)` for every positive complete live fingerprint.

Any key claimed by more than one row produces a typed whole-batch failure listing the conflicting
sessions. Persisted stopped rows still participate in socket collision detection because a later
start can reuse their path. Incomplete fingerprints are already handled by the existing PID repair
and unknown-status gates and never become concurrent work.

This gate runs after PID repair and before the first destructive submission. It validates ownership
for the complete affected scope, not only the selected rows.

## Dedicated Values

### `DedicatedTeardownPlan`

A frozen dataclass with value fields:

```python
@dataclass(frozen=True)
class DedicatedTeardownPlan:
    session_name: str
    socket_path: str
    stored_pid: int
    stored_boot_id: str
    stored_start_ticks: int | None
    target: Transport
    sudo: bool
    force: bool
```

The non-null fields are validated before construction. The values also form the expected identity
for later compare-and-set persistence. Each plan owns a distinct transport instance. No two
concurrently running plans share it.

### `DedicatedTeardownOutcome`

A frozen dataclass containing only:

```python
@dataclass(frozen=True)
class DedicatedTeardownOutcome:
    refined_fingerprint: TmuxServerFingerprint | None = None
    error: Exception | None = None
```

`error is None` means the runtime and exact socket were verified absent. A non-null error means
failure; the optional existing `TmuxServerFingerprint` carries the strongest safe identity learned
before that failure. The future-to-plan map supplies the session name, so the result does not repeat
it.

## Preparation

`prepare_dedicated_teardown(db, session, *, target, target_owns_session, force)` runs only on the
invoking thread while its lifecycle lock is held.

1. Reject the stopped sentinel as no work.
2. Require a dedicated socket and validate it as an exact managed path through the current database
   graph.
3. Require a positive persisted PID and canonical boot ID.
4. Validate optional start ticks.
5. Construct the plan with `sudo=not target_owns_session`.

Preparation performs no remote mutation. A preparation failure is a per-session failure only after
the batch-wide status, collision, and lock gates have made the selection safe.

## Dedicated Remote State Machine

`execute_dedicated_teardown(plan)` runs in a worker and never accesses global output or SQLite. It
uses the plan's transport without copying ambient context, since workers consume no context-bound
presentation or bootstrap service.

### Reachable fingerprint

1. Capture the tmux server fingerprint through the exact socket.
2. Require a present, complete fingerprint.
3. Validate observed boot ID.
4. Require observed PID and boot ID to equal persisted values.
5. If stored start ticks exist, require equality.
6. If stored start ticks are absent, retain the observed existing `TmuxServerFingerprint` as
   refinement evidence.
7. Invoke exact `kill-server` through the socket.
8. Prove the fingerprinted server process absent using the best complete fingerprint, without
   rereading SQLite.
9. Remove the exact socket path and verify its absence.
10. Return verified stopped.

The absence proof helper accepts explicit fingerprint values so it no longer needs a refreshed
database row after the kill.

### Unreachable or indeterminate fingerprint

If initial capture is not present, preserve the current teardown ordering. The worker first tries to
prove the stored runtime absent regardless of `force`. If proof succeeds, it removes and verifies
the exact stale socket and returns stopped. If proof cannot establish absence, an unforced plan
returns the current actionable broken-state wrapper; a forced plan returns the more specific
existing identity or connectivity failure. `force` changes the error/selection policy, not whether
safe proven-absence recovery is attempted.

The worker never kills a process by PID and never treats a transport failure as absence.

### Exception preservation

The function tracks the latest complete refinement evidence. It catches ordinary `Exception` at its
outer boundary and returns that exception with the evidence. It does not catch `KeyboardInterrupt`,
`SystemExit`, or other `BaseException` values.

## Legacy Synchronous Path

The existing legacy function stays on the invoking thread:

1. probe `tmux has-session -t =NAME` on the default server;
2. fail if presence is unknown;
3. when present, invoke `kill-session -t =NAME`;
4. probe until the exact session is absent; and
5. mark the row stopped through its existing main-thread database call.

Legacy rows run one at a time after all dedicated outcomes are reconciled. Ordinary legacy failure
is accumulated and the next legacy row proceeds. Compatibility code does not learn the dedicated
plan or outcome model.

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

`execute_concurrent_dedicated_teardowns(plans, *, db)` accepts dedicated plans only.

An empty plan sequence returns immediately without constructing an executor. This preserves the
legacy-only lane and lets a batch containing only dedicated preparation failures continue to its
normal failure accounting.

1. Create a `ThreadPoolExecutor` with `min(8, len(plans))` workers.
2. Submit the remote executor directly for every plan; no context copy is needed.
3. Map each future to its plan.
4. Use `wait(..., timeout=5, return_when=FIRST_COMPLETED)` so the invoking thread can emit a
   heartbeat after a quiet interval.
5. Reconcile every completed future before its labeled output.
6. Collect `(session_name, error)` failures.
7. Repeat until every submitted future is completed or cancelled.
8. Shut down and return all failures.

The worker boundary converts every ordinary per-session `Exception` into an outcome. A
`BaseException` escaping a worker, a broken executor, or another coordinator-level failure enters
the abort-and-reconcile path so already-started mutations are drained and persisted before the
failure propagates. The coordinator submits the selection once, so the maximum queued set is the
selection and the maximum active set is eight.

The heartbeat reports completed, active, and queued counts. A completion resets the quiet interval,
so fast batches emit only their ordinary session outcomes.

A completed future remains in coordinator bookkeeping until reconciliation and outcome rendering
finish. If `KeyboardInterrupt` lands during that main-thread work, interruption mode retries the
same outcome. Retry-safe compare-and-set recognizes a desired state that the interrupted attempt
already committed.

## Atomic Reconciliation

Add a database operation that updates session runtime only when the row still matches all prepared
runtime identity fields. It uses one `UPDATE ... WHERE name = ? AND socket_path IS ? AND pid IS ?`
plus the corresponding boot ID and start-ticks predicates, checks `rowcount == 1`, and commits
through the existing transaction policy. After zero affected rows, it reads the row once: an exact
match for the desired state means an earlier interrupted reconciliation already committed and is
idempotent success; any other or missing row is a typed conflict and remains untouched.

`reconcile_dedicated_teardown(db, plan, outcome)` runs on the invoking thread while the lifecycle
lock remains held:

- verified stopped compare-and-sets from the complete prepared identity to the persisted socket,
  stopped PID, null boot ID, and null start ticks;
- failed with a refined fingerprint compare-and-sets from the prepared identity to that complete
  fingerprint before returning the failure; and
- failed without evidence makes no database change.

The function does not overwrite declaration or harness state. Persistence precedes success output. A
compare-and-set conflict or other database error becomes that session's failure even if remote
teardown was verified, because remote success cannot be rolled back or honestly hidden.

The lifecycle lock is the remote-mutation exclusion boundary. Compare-and-set is the persisted-state
fence against a bypass, programming error, or changed or missing row in the open database. The same
lock excludes first-party live-database replacement while a session mutation is active; arbitrary
external file replacement remains outside Agentworks' guarantees.

## Interruption State Machine

The first `KeyboardInterrupt` that escapes future collection becomes the stored primary
interruption. The coordinator then:

1. invokes `future.cancel()` for every unfinished future;
2. reports that active teardowns are being reconciled;
3. continues the five-second wait loop for futures that were already running;
4. reconciles every returned outcome; and
5. re-raises the stored interruption after all running work finishes.

Cancelled futures produce no session failure because no mutation began. The database lifecycle lock
is released only when the whole coordinator exits, and they remain eligible for a future retry.

A later interrupt during reconciliation repeats the notice and returns to the wait loop. Python
cannot terminate running executor threads, and interpreter shutdown joins them. The command
therefore does not offer a fictional immediate escape. Finite per-call timeouts, a finite state
machine, and five-second heartbeats make the mandatory reconciliation wait visible and bounded.

## Integration With Existing Callers

### Named stop

Named stop acquires its lifecycle lock, dispatches legacy synchronously or prepares and executes the
dedicated plan synchronously, reconciles, and retains its single terminal result line. It does not
use an executor and does not gain the batch timeout.

### Batch stop

Batch stop validates filter names, acquires the database lifecycle lock, reloads the selection and
relationships, preserves current VM and status gates, performs the collision check, announces the
count, reconciles the dedicated concurrent lane, then runs the legacy serial lane. It raises the
current aggregate `ExternalError` when any session failed.

### Create, start, restart, and deletion

Create and launch operations gain only the shared lifecycle lock around final status, remote
mutation, and persistence. Restart and direct or cascading deletion call the same synchronous
teardown dispatcher. None enters the concurrent coordinator.

## Tests

### Unit and service tests

- generalized SQLite sidecar exclusion, process-exit release, bounded migration wait, and
  non-blocking lifecycle/restore refusal;
- mutual exclusion among migration, restore, and session lifecycle mutation;
- absent/version-zero initialization reinspection under lock and exclusion against restore;
- initialization and restore when both the database and parent directory are absent;
- fresh post-lock selection and relationship loading for batch and named mutators;
- database restore exclusion and an inventory of first-party whole-database replacement paths;
- collision refusal for socket and complete fingerprint identities;
- dedicated plan validation for admin, agent, force, and stopped rows;
- successful reachable teardown with and without fingerprint refinement;
- current unforced and forced unreachable-runtime behavior;
- exact socket cleanup and exact legacy session targeting;
- distinct transport identity per concurrent task;
- maximum eight active workers and same-VM overlap;
- empty dedicated-plan handling for legacy-only and preparation-failure selections;
- serial legacy execution without the plan model;
- atomic and retry-safe compare-and-set, persistence-before-output, and persistence failure;
- sibling success under worker failure;
- five-second heartbeat behavior;
- first-interrupt queued cancellation and running reconciliation;
- repeated-interrupt reconciliation; and
- named stop, create/start/restart, direct delete, cascade, batch PID repair, workspace rehome, and
  partial-create rollback lock regression coverage.

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
6. retry the failed selection successfully; and
7. exercise lock contention from two local CLI processes without touching production sessions.

The test does not use production sessions or rely on timing alone. Instrumented unit overlap is the
deterministic concurrency proof; live elapsed time is supporting evidence.
