# High-Level Architecture: Session Batch Stop Parallelism

- Status: Draft for design review
- Date: 2026-09-06
- Requirements: [frd.md](./frd.md)
- Detailed design: [session-teardown-lld.md](./session-teardown-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)

## Architectural Result

Batch stop becomes a coordinator-owned mutation pipeline:

```text
CLI and session manager, invoking thread
  validate filters -> select -> hold VMs -> observe -> partition by identity completeness
                                      |
                    validate concurrent plan collisions
                                      |
                    +-----------------+-----------------+
                    | bounded dedicated worker pool     |
                    | current exact teardown, max eight |
                    +-----------------+-----------------+
                                      |
                    invoking thread compare-and-sets state
                    and emits session-labeled outcomes
                                      |
                    serial incomplete and legacy lane
                                      |
                         aggregate failure or success
```

The design extracts phases from the existing teardown authority rather than adding a second teardown
implementation. The dedicated worker pool is specific to session stop mutations. It does not reuse
the read-only status fan-out context manager because mutation has different completion, evidence,
and interruption requirements.

## Components

### Batch stop coordinator

`stop_all_sessions` retains its current filter, relationship, VM activation, PID repair, and status
boundaries. It partitions actionable work by persisted identity completeness. Dedicated rows with a
complete valid fingerprint become immutable concurrent plans. Incomplete dedicated and legacy rows
stay on the existing synchronous dispatcher.

For dedicated work, the coordinator creates one transport per task with an initially 10-second
finite default timeout and submits one future per plan with at most eight active. If submission is
interrupted or fails partway, a batch-local start gate makes every submitted callable exit without
remote mutation, including work enqueued before `submit()` raised without returning its future. Only
after the complete future map exists does the coordinator release the gate for execution. It then
consumes futures in completion order, applies verified stopped evidence to SQLite through
compare-and-set, and emits a labeled line. Ordinary failures accumulate without cancelling siblings.
A five-second quiet wait emits a compact progress heartbeat.

The initial eight-worker, ten-second, one-attempt policy matches the shipped read-only guest
observation policy as precedent. Teardown owns separately named constants and a different executor
loop because mutating work must drain and reconcile running futures rather than abandon them.

Incomplete dedicated and legacy work runs serially on the invoking thread after the concurrent lane
has reconciled. Incomplete dedicated rows keep the current pre-kill fingerprint persistence. Legacy
rows keep exact shared-server `kill-session` behavior. The final aggregate error counts failures
from both lanes.

### Teardown preparation

Preparation is the only worker-adjacent phase allowed to consult the database for ownership and
socket-path validation. It produces a dedicated-only frozen value containing:

- session name;
- canonical database VM name;
- exact validated socket path;
- complete stored boot ID, PID, and start ticks;
- target and sudo policy; and
- force authorization.

The transport is a per-task object. No connection cache or control master is introduced. This
preserves ADR 0015's fresh-authentication behavior and avoids relying on undocumented transport
thread safety.

### Remote teardown engine

The worker performs the current complete-fingerprint protocol without database access: capture the
exact socket's fingerprint, compare every identity field, invoke `kill-server`, prove that process
incarnation absent, remove the exact socket, and verify socket absence. There is no database call or
queueing boundary between capture and destructive use.

The future distinguishes:

- verified stopped;
- an ordinary per-session exception; and
- cancelled before start, which is tracked by the coordinator rather than a worker result.

Ordinary exceptions stay on the future and receive their session attribution from the future-to-plan
map. A worker-level `BaseException` enters coordinator abort-and-drain handling before it
propagates.

### Outcome reconciliation

The coordinator applies one successful future at a time. Verified stopped state clears the runtime
fingerprint using the existing stopped sentinel through an atomic compare-and-set against the plan's
complete prepared identity. A missing or changed row is a conflict and remains untouched. No worker
owns a transaction or a connection.

Persistence precedes success output. A persistence error converts that session to failure. Remote
success cannot be rolled back, so the failure tells the truth rather than pretending the runtime
remains live.

### Interruption controller

The first interrupt changes coordinator state from ordinary collection to reconciliation:

- pending futures are cancelled;
- a visible reconciliation notice is emitted; and
- running futures continue under finite per-call timeouts.

If the interrupt or a coordinator failure occurs before the complete future map exists, the
coordinator marks the submission gate aborted before releasing it, cancels queued work, and waits
for already-running wrappers to take the no-mutation branch. All plans remain untouched and are
reported as not started. Once the complete map exists, the gate is released and the ordinary
cancellation and reconciliation state applies, including an interrupt at the release boundary.

The coordinator reconciles every normally completed future that becomes available, then propagates
the original interrupt. Later interrupts repeat the reconciliation notice and do not abandon running
work. A five-second wait loop also preserves ordinary and interrupted progress heartbeats.

### Database replacement boundary

PR #764 supplies the only new cross-process lock this design needs. A writable `Database` holds its
database-use sidecar shared for the command lifetime; restore holds that same sidecar exclusive
while replacing live state. The coordinator cannot therefore reconcile worker results into a
different database file. The final compare-and-set separately prevents one completed worker from
overwriting a row whose runtime identity changed through another command.

### Collision gate

Before submission, batch stop indexes the selected complete-fingerprint plans by `(VM, socket_path)`
and `(VM, canonical boot ID, positive PID)`. A duplicate key causes a typed whole-batch refusal.
Start ticks still distinguish process incarnations during each worker's exact identity check, but
cannot establish that two plans claiming one live PID are independent. Incomplete rows never enter
the pool, and unselected rows cannot create two overlapping workers.

## Boundaries and Ownership

| Concern                              | Owner                                  |
| ------------------------------------ | -------------------------------------- |
| CLI parsing and confirmation         | existing CLI command                   |
| Filtering and VM activation holds    | existing session manager coordinator   |
| Live-database replacement exclusion  | PR #764 database-use lock              |
| Database reads and writes            | invoking thread only                   |
| Socket-path and ownership validation | invoking thread preparation            |
| Remote tmux/process/socket calls     | one dedicated worker per prepared task |
| Human progress and warnings          | invoking thread only                   |
| Legacy shared-server teardown        | invoking thread, serial                |
| Start/restart launch behavior        | unchanged serial lifecycle path        |

## Concurrency Unit

The unit is one dedicated session runtime, not one VM. A dedicated session's persisted `-S` socket
names an independent tmux server, so sessions on the same VM do not share the mutated runtime.
Concurrency remains globally bounded to protect the operator host, VM SSH service, and guest.

Legacy rows share a default tmux server authority and therefore do not enter the pool. Provider and
VM activation operations remain outside the pool.

## Failure Model

Batch stop remains partially successful. Failures are isolated per session once destructive work
begins. A session can fail because of transport, identity, teardown, verification, socket cleanup,
or persistence. Successful siblings retain their persisted stopped state.

Whole-batch refusal remains appropriate before submission when the selection cannot be made safe,
including concurrent-plan identity collision, unknown actionable status, or failed VM preparation.
There is no attempt to make a remote multi-resource transaction.

## Security Model

The design does not expand authority. Agent sessions reached through the batch admin transport keep
the existing non-interactive sudo boundary. Commands continue to quote the validated exact managed
socket. Fingerprints remain the authority for force recovery and post-kill proof. Numeric PID
signaling remains forbidden.

Each worker has only the minimum prepared values needed for its one session. It receives no
database, resolver, registry, configuration writer, output handler, or interactive policy.

## Alternatives Rejected

### One worker per VM

This would serialize independent dedicated servers on the same VM and leave the common one-VM batch
slow. The socket architecture makes one session the safe and useful unit.

### Shared transport per VM

Transport objects do not promise thread safety, and a future logger or transport implementation may
carry mutable state. A per-task object is cheap because current SSH transport creation does not open
or cache a connection.

### Reuse `cancelling_futures`

That helper serves read-only observation, where abandoning results is safe. Mutation must return and
persist evidence, distinguish queued from running work, and support a visible reconciliation phase.
A mutation-specific coordinator is clearer than flags on a generic helper.

### Parallelize start and restart now

Their per-session operation performs prompts, secret resolution, harness integration state changes,
teardown plus launch, database writes, and shared tmuxinator regeneration. The worker contract in
this design deliberately excludes each of those concerns.

### One compound guest script

Combining all teardown steps could reduce round trips, but it would also rewrite the audited
identity and failure-evidence boundaries. The performance problem can be solved by safe overlap
without changing that safety protocol.

### Add a repository-wide session-runtime lock

Rejected. Current lifecycle commands do not promise cross-process serialization, and complete-row
workers preserve the existing immediate capture-to-kill window. A new lock and token threaded
through every lifecycle caller would turn a bounded batch optimization into a separate global
coordination project. PR #764 already prevents live-database replacement, while compare-and-set
protects the newly introduced worker-to-persistence gap.

## Deployment Shape

The change is an in-place internal refactor and behavior improvement based after PR #764. It adds no
new lock, dependency, schema migration, capability version change, persisted format change, CLI
alias, or configuration rollout. Full code, documentation, and live-test evidence land together in
one PR.
