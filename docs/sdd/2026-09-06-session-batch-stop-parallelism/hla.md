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
  validate filters -> acquire database lifecycle lock -> reload selection and relationships
             -> hold VMs -> observe -> collision check
                                      |
                         prepare dedicated work
                                      |
                    +-----------------+-----------------+
                    | bounded dedicated worker pool     |
                    | remote teardown only, max eight   |
                    +-----------------+-----------------+
                                      |
                         structured remote outcomes
                                      |
                    invoking thread applies DB evidence
                    and emits session-labeled outcomes
                                      |
                    serial exact legacy kill-session lane
                                      |
                         aggregate failure or success
```

The design extracts phases from the existing teardown authority rather than adding a second teardown
implementation. The dedicated worker pool is specific to session stop mutations. It does not reuse
the read-only status fan-out context manager because mutation has different completion, evidence,
and interruption requirements.

## Components

### Batch stop coordinator

`stop_all_sessions` validates filter names, acquires the state database's session-lifecycle lock,
then loads the filtered rows and their VM/workspace relationships fresh under the lock. It retains
the outer VM activation boundary. After the current PID repair, status-observation, and new
collision gates, it prepares dedicated work while leaving legacy work on the existing synchronous
path.

For dedicated work, the coordinator creates one transport per task with an initially 10-second
finite default timeout and submits at most eight tasks. It consumes futures in completion order,
applies each outcome to SQLite through compare-and-set, and emits a labeled line. Ordinary failures
accumulate without cancelling siblings. A five-second quiet wait emits a compact progress heartbeat.

Legacy work runs serially on the invoking thread after the dedicated lane has reconciled. It keeps
the exact shared-server `kill-session` behavior. The final aggregate error counts failures from both
lanes.

### Teardown preparation

Preparation is the only worker-adjacent phase allowed to consult the database for ownership and
socket-path validation. It produces a dedicated-only frozen value containing:

- session name;
- exact validated socket path;
- stored boot ID, PID, and start ticks;
- target and sudo policy; and
- force authorization.

The transport is a per-task object. No connection cache or control master is introduced. This
preserves ADR 0015's fresh-authentication behavior and avoids relying on undocumented transport
thread safety.

### Remote teardown engine

The remote engine performs the current exact probe, identity comparison, server kill, absence proof,
and socket cleanup using only the immutable work value. It returns a typed result rather than
mutating the database.

The result distinguishes:

- verified stopped;
- failed with no stronger evidence;
- failed after learning a complete current fingerprint; and
- cancelled before start, which is tracked by the coordinator rather than a worker result.

The engine catches ordinary `Exception` so a verified fingerprint can survive a later failure. It
does not catch `BaseException` inside the worker.

### Outcome reconciliation

The coordinator applies one outcome at a time. Verified stopped state clears the runtime fingerprint
using the existing stopped sentinel. A refined fingerprint updates the runtime identity before its
failure is rendered. Each write is an atomic compare-and-set against the plan's prepared runtime
identity. A missing or changed row is a conflict and remains untouched. No worker owns a transaction
or a connection.

Persistence precedes success output. A persistence error converts that session to a failed outcome.
Remote success cannot be rolled back, so the failure tells the truth rather than pretending the
runtime remains live.

### Interruption controller

The first interrupt changes coordinator state from ordinary collection to reconciliation:

- pending futures are cancelled;
- a visible reconciliation notice is emitted; and
- running futures continue under finite per-call timeouts.

The coordinator consumes and persists every outcome that becomes available, then propagates the
original interrupt. Later interrupts repeat the reconciliation notice and do not abandon running
work. A five-second wait loop also preserves ordinary and interrupted progress heartbeats.

### Session lifecycle exclusion

Every Agentworks runtime mutation for any session acquires the same cross-process database mutation
lock for the resolved state-database path. The design generalizes the existing migration lock, which
holds a `BEGIN IMMEDIATE` write transaction on a dedicated SQLite sidecar. Lifecycle acquisition is
non-blocking and contention fails before mutation. This preserves all worker parallelism inside one
batch while excluding a second CLI process from changing selected or unselected session runtime
state during the collision scan and remote work.

The lock is held from the fresh pre-mutation load through remote mutation and persistence. Closing
the sidecar connection releases it, including when process exit closes the connection. Create,
start, restart, stop, direct delete, cascading teardown, runtime fingerprint repair, and rollback
deletion all use this boundary. Absent/version-zero initialization and schema migration use the
current bounded wait; initialization rechecks the database state under the lock. Session lifecycle
and live-database restore use fail-fast acquisition. Compare-and-set persistence remains a defense
against a missing or changed row in the open database.

The neutral lock helper securely creates a missing database parent before opening the sidecar, using
the existing owner-private directory policy and typed filesystem failures. This keeps first open and
restore into a fully absent destination supported.

### Collision gate

With the lifecycle lock held and before mutation, batch stop inspects every session row on the
affected VMs. Two rows claiming one non-null socket path or one positive live fingerprint cause a
typed whole-batch refusal. This proves that every submitted plan owns a distinct persisted runtime
rather than treating path shape as uniqueness evidence.

## Boundaries and Ownership

| Concern                              | Owner                                  |
| ------------------------------------ | -------------------------------------- |
| CLI parsing and confirmation         | existing CLI command                   |
| Filtering and VM activation holds    | existing session manager coordinator   |
| Cross-process lifecycle exclusion    | session runtime lock helper            |
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
including lock contention, identity collision, unknown actionable status, or failed VM preparation.
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

### Database-only conflict detection

A compare-and-set can prevent stale persistence but cannot prevent a stale worker from addressing or
unlinking a replacement runtime at the same socket. Cross-process lifecycle exclusion is required
across remote mutation and persistence; compare-and-set remains the final state fence.

## Deployment Shape

The change is an in-place internal refactor and behavior improvement. It generalizes the existing
SQLite sidecar migration lock but adds no dependency, schema migration, capability version change,
persisted format change, CLI alias, or configuration rollout. Full code, documentation, and
live-test evidence land together in one PR.
