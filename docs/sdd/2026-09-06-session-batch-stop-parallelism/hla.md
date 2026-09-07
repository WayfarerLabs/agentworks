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
  validate filters -> acquire session-runtime lock -> reload selection and relationships
             -> hold VMs -> observe -> collision check
                                      |
                         prepare dedicated work
                                      |
                    +-----------------+-----------------+
                    | bounded dedicated worker pool     |
                    | probe, checkpoint, then teardown  |
                    | max eight remote tasks active     |
                    +-----------------+-----------------+
                                      |
                       structured probe outcomes
                                      |
                    invoking thread durably refines any
                    missing start ticks before mutation
                                      |
                       structured teardown outcomes
                                      |
                    invoking thread applies stopped state
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

`stop_all_sessions` validates filter names, acquires the state database's session-runtime lock, then
loads the filtered rows and their VM/workspace relationships fresh under the lock. It retains the
outer VM activation boundary. After the current PID repair, status-observation, and new collision
gates, it prepares dedicated work while leaving legacy work on the existing synchronous path.

For dedicated work, the coordinator creates one transport per task with an initially 10-second
finite default timeout and submits at most eight remote tasks at once. Each session first receives a
non-destructive probe. The coordinator consumes probe futures in completion order, durably
compare-and-sets any missing start ticks, then submits destructive work for that session. It later
applies each teardown outcome through compare-and-set and emits a labeled line. Ordinary failures
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

### Remote probe and teardown engine

The remote engine splits the current exact protocol at its durability checkpoint. Its probe phase
captures and validates current identity without mutation. The coordinator persists newly learned
start ticks before it makes the session eligible for the teardown phase. Teardown then performs the
server kill, absence proof, and socket cleanup using only immutable values. Both phases return typed
results rather than mutating the database.

The results distinguish:

- a validated reachable fingerprint;
- unreachable or indeterminate initial state for the existing absence-proof path;
- verified stopped;
- failed teardown; and
- cancelled before start, which is tracked by the coordinator rather than a worker result.

The engine catches ordinary `Exception` so per-session failure remains structured. It does not catch
`BaseException` inside the worker. Because a complete reachable fingerprint is already durable
before destructive submission, an escaped worker-level `BaseException` leaves conservative,
actionable identity in SQLite while the coordinator drains sibling work and propagates the failure.

### Outcome reconciliation

The coordinator applies one checkpoint or teardown outcome at a time. A reachable probe with missing
stored start ticks first compare-and-sets the observed complete fingerprint and updates the
immutable expected identity used by destructive work. The teardown future is not submitted unless
that write succeeds. Verified stopped state later clears the runtime fingerprint using the existing
stopped sentinel. Each write is an atomic compare-and-set against the stage's prepared runtime
identity. A missing or changed row is a conflict and remains untouched. No worker owns a transaction
or a connection.

Checkpoint persistence precedes destructive submission, and stopped-state persistence precedes
success output. A checkpoint persistence error prevents that session's remote mutation. A final
persistence error converts that session to a failed outcome. Remote success cannot be rolled back,
so the failure tells the truth rather than pretending the runtime remains live.

### Interruption controller

The first interrupt changes coordinator state from ordinary collection to reconciliation:

- pending futures are cancelled and no new destructive follow-up is submitted;
- a visible reconciliation notice is emitted; and
- running futures continue under finite per-call timeouts.

The coordinator consumes and persists every outcome that becomes available, then propagates the
original interrupt. Later interrupts repeat the reconciliation notice and do not abandon running
work. A five-second wait loop also preserves ordinary and interrupted progress heartbeats.

### Session lifecycle exclusion

Every Agentworks runtime mutation for any session acquires the same cross-process session-runtime
mutation lock for the resolved state-database path. The design generalizes the existing migration
lock, which holds a `BEGIN IMMEDIATE` write transaction on a dedicated SQLite sidecar. Lifecycle
acquisition is non-blocking and contention fails before mutation. This preserves all worker
parallelism inside one batch while excluding a second CLI process from changing selected or
unselected session runtime state during the collision scan and remote work. It is deliberately not a
universal SQLite writer lock.

The lock is held from the fresh pre-mutation load through remote mutation and persistence. Closing
the sidecar connection releases it, including when process exit closes the connection. Create,
start, restart, stop, direct delete, workspace/agent/VM cascading teardown, runtime fingerprint
repair, `last_started_at` persistence, failed-launch cleanup, and rollback deletion all use this
boundary. Absent/version-zero initialization and schema migration use the current bounded wait;
initialization rechecks the database state under the lock. Compare-and-set persistence remains a
defense against a missing or changed row in the open database.

PR #764 supplies a separate database-use lock. A writable `Database` holds that lock shared for its
lifetime, including while it acquires and holds the session-runtime mutation lock. Restore holds the
database-use lock exclusive and does not acquire the session-runtime lock. It therefore cannot
replace live state during any writable lifecycle command, while unrelated writable commands remain
outside session-runtime serialization. The mutation token is bound to the same canonical database
path and live sidecar handle as the `Database` using it.

The neutral session-runtime helper securely creates a missing database parent before opening its
sidecar, using the existing owner-private directory policy and typed filesystem failures. This keeps
first open into a fully absent destination supported; PR #764 independently preserves absent-target
restore through the database-use lock.

### Collision gate

With the session-runtime lock held and before mutation, batch stop inspects every session row on the
affected VMs. Two rows claiming one non-null socket path or one positive
`(VM, canonical boot ID, PID)` identity cause a typed whole-batch refusal. Start ticks remain part
of exact validation, but missing or differing ticks cannot turn shared boot/PID ownership into
distinct runtimes. This proves that every submitted plan owns a distinct persisted runtime rather
than treating path shape as uniqueness evidence.

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

The change is an in-place internal refactor and behavior improvement based after PR #764. It
generalizes the existing SQLite sidecar migration lock for session-runtime exclusion while retaining
PR #764's separate database-use lock. It adds no dependency, schema migration, capability version
change, persisted format change, CLI alias, or configuration rollout. Full code, documentation, and
live-test evidence land together in one PR.
