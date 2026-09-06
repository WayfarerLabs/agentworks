# Functional Requirements: Session Batch Stop Parallelism

- Status: Draft for requirements review
- Date: 2026-09-06
- Scope: bounded parallel teardown for `agw session stop --all`
- Tracks: issue #730

## Summary

`agw session stop --all` currently discovers session state in batches, then tears down every
selected runtime serially. A large selection pays the complete remote teardown latency once per
session even though current sessions use independent tmux servers and independent socket paths.

This effort makes batch stop perform safe dedicated-session teardown concurrently. It does not add
new command grammar, a configurable worker count, or parallel start and restart. The main thread
continues to own prompts, database access, state reconciliation, aggregate failure, and operator
output. A bounded worker pool owns only prepared remote teardown work and returns structured
evidence to the coordinator.

Legacy rows that still use a shared default tmux server remain serial. A single named stop keeps its
current synchronous behavior while sharing the same extracted teardown phases.

## Goals

1. Make the elapsed time of a multi-session batch stop approach the slowest bounded wave of remote
   teardowns instead of the sum of every teardown.
2. Preserve the exact tmux identity, verification, stale-socket, and fail-closed guarantees of the
   current lifecycle contract.
3. Keep SQLite access and operator output on the invoking thread.
4. Preserve successful teardown evidence when sibling sessions fail or the operator interrupts the
   batch.
5. Give the operator immediate, session-labeled progress during a concurrent batch.
6. Avoid a speculative concurrency framework for lifecycle operations whose safety contracts differ.

## Non-goals

- Parallelizing `session start --all` or `session restart --all`.
- Changing named `session stop` semantics or making it asynchronous.
- Adding `--workers`, configuration, environment variables, or capability API changes.
- Combining several session teardowns into one remote shell program.
- Changing tmux signal policy, dedicated socket identity, force recovery, or descendant containment.
- Providing a transactional all-or-nothing batch operation across remote runtimes and SQLite.
- Solving process escape from a tmux server. Issue #715 owns systemd/cgroup containment.

## Users and Stories

### Operator stopping several sessions

The operator runs `agw session stop --all` with zero or more existing filters. Agentworks reports
the selected count before remote mutation, runs independent dedicated-runtime teardowns with bounded
parallelism, reports each completed outcome with its session name, and returns after reconciling all
completed work.

### Operator encountering one unhealthy target

One session's remote teardown times out or fails identity verification. Other already-selected
sessions continue. Successful teardowns are persisted and reported, the failed session retains the
strongest truthful runtime evidence available, and the command ends with aggregate failure.

### Operator interrupting a batch

The operator presses Ctrl-C after remote mutations have started. Agentworks cancels work that has
not started, says that in-flight teardowns are being reconciled, waits for their bounded remote
calls, applies every returned result on the main thread, and then exits as interrupted. It does not
discard completed evidence or claim that a running worker was cancelled. Additional interrupts
repeat the reconciliation notice; Python cannot safely terminate running thread work.

## Requirements

### R1: CLI compatibility

The existing command, filters, confirmation behavior, `--force` policy, output mode, and exit
semantics MUST remain the public surface. No new option or configuration key is added. A selection
of zero actionable sessions MUST retain the current successful no-op result.

This feature targets 0.19.0 and introduces no deprecation or compatibility alias.

### R2: Concurrency eligibility

A selected session MAY run in the concurrent lane only when its persisted runtime uses a validated
dedicated tmux socket. Dedicated sessions MAY run concurrently even when they share a VM because
each `-S SOCKET` address selects an independent tmux server.

Before mutation, Agentworks MUST inspect every session row on the affected VMs and fail closed if
two rows claim the same dedicated socket or the same live boot/PID/start-time fingerprint. The
parallelism invariant MUST be proved from current persisted facts, not inferred from a non-null
socket.

A legacy row with no dedicated socket MUST use the existing exact `kill-session -t =NAME` behavior
and MUST remain serial because it may share the default tmux server with another row. The
implementation MUST NOT migrate, parallelize, or widen legacy teardown as part of this effort.

### R3: Fixed bounded fan-out

The concurrent lane MUST use a small fixed global ceiling of eight workers and MUST use fewer when
fewer dedicated sessions are selected. It MUST NOT create a worker per session without a bound.

The fixed value is implementation policy, not a public contract. Adding operator configuration
requires measured evidence that the fixed bound is inadequate and is outside this effort.

### R4: One-thread state ownership

Only the invoking coordinator thread MAY read or write the shared `Database` during concurrent
teardown. Workers MUST NOT receive the database object, call output helpers, prompt, resolve
secrets, activate VMs, alter provider state, or regenerate shared configuration.

Before submission, the coordinator MUST convert each selected session into immutable work containing
only the validated runtime identity, target, privilege policy, and teardown policy required by the
remote phase. After completion, the coordinator MUST apply returned evidence and render output.

### R5: Remote teardown invariants

Parallel execution MUST preserve the session lifecycle contract:

- validate the exact persisted managed socket before destructive use;
- capture and compare boot ID, server PID, and process start time;
- use `tmux -S SOCKET kill-server` for a reachable dedicated runtime;
- verify that the fingerprinted server process is absent;
- remove only the exact validated managed socket and verify it is absent;
- never signal a numeric PID directly; and
- clean broken runtime state only after proving the stored runtime absent, preserving current
  unforced safe-recovery behavior.

Concurrency MUST NOT weaken target identity, permit best-effort destructive guesses, or share a
mutable transport between worker tasks. Every Agentworks session runtime mutator MUST hold the same
cross-process database-scoped session-lifecycle lock from its final pre-mutation status check
through remote work and persistence. The lock MUST be released automatically if the process exits,
and its implementation MUST reuse the existing SQLite sidecar mutation lock rather than add an
independent exclusion mechanism. Replacing the live state database from backup MUST acquire the same
lock across the replacement so it cannot install new runtime identities beneath in-flight remote
work.

### R6: Bounded remote calls

Every transport created for a concurrent teardown MUST use an internal finite default timeout and
one attempt for each non-interactive remote call. The initial implementation value is 10 seconds and
MUST be confirmed or increased from supported-environment teardown evidence before lock. This bounds
one remote step, not the whole session: the complete teardown may execute several sequential steps.

Timeout is a failure for that session. The coordinator MUST retain partial evidence from steps that
completed before the failure and MUST NOT automatically repeat a destructive command whose outcome
is uncertain.

The named synchronous stop path keeps its current transport policy. The concurrent timeout is a
batch worker safety boundary, not a global transport-policy change.

### R7: Completion and persistence

Each worker MUST return one structured outcome containing the strongest runtime evidence learned and
either verified stopped state or a failure. The coordinator already owns the future-to-session
mapping. A worker MUST catch ordinary per-session exceptions so evidence learned before a later
failure is not lost.

The coordinator MUST consume outcomes as they complete. It MUST apply a verified stopped result
before announcing success. If a missing persisted start time was safely refined before a later
failure, it MUST persist that refined fingerprint before reporting failure. A database-write failure
MUST be reported as failure even if the remote runtime was already stopped.

One session failure MUST NOT cancel sibling sessions. The final command failure MUST retain the
existing aggregate behavior after all eligible work and reconciliation complete.

### R8: Operator output

The human command MUST report the batch count before submitting remote mutations. Each completed
session MUST then receive a session-labeled success or warning from the coordinator. Completion
lines MAY appear in completion order; they MUST NOT rely on adjacency to identify their session.

If no session completes for five seconds, the coordinator MUST emit a compact heartbeat containing
completed, active, and queued counts. It MUST repeat at no more than five-second intervals while the
wait remains quiet, including interruption reconciliation. A normally fast batch therefore has no
heartbeat noise, while a slow transport never leaves the operator with unexplained silence.

Workers MUST emit no output. Machine-readable output, if introduced independently, MUST remain free
of progress prose; this effort does not add a batch-stop JSON result.

### R9: Interruption

On the first operator interrupt after submission, the coordinator MUST:

1. cancel futures that have not started;
2. report that active teardowns are being reconciled;
3. continue consuming already-completed and in-flight bounded outcomes;
4. apply their evidence on the main thread; and
5. propagate interruption after reconciliation.

Running thread work cannot be cancelled safely and MUST NOT be described as cancelled. A second or
later interrupt MUST repeat the visible reconciliation notice and continue draining. The command
MUST NOT promise an immediate escape that Python's executor shutdown cannot deliver.

### R10: Existing batch boundary and selection

Filter-name validation MUST precede non-blocking acquisition of the state database's one
session-lifecycle lock. A read-only pre-lock query MAY return the existing successful no-op when the
selection is already empty. When candidates exist, contention MUST refuse the operation before
mutation. With the lock held, Agentworks MUST reapply the filters and load the selected session rows
and their current VM/workspace relationships fresh, then perform VM graph validation, activation
holds, PID repair, batch status observation, collision checks, broken-state selection, and the
`--force` gate before concurrent destructive work begins. The fresh locked selection MAY also become
empty and return the same successful no-op. Named mutators MUST likewise reload their target row and
required relationships after acquiring the lock. An unknown actionable session MUST retain the
current whole-batch refusal before mutation.

The lifecycle lock and VM activation boundary MUST remain held until every submitted worker has
completed and its outcome has been reconciled. Named create, start, restart, stop, and delete plus
cascading teardown, PID/fingerprint repair, and rollback row deletion MUST use the same lock
boundary so a stale worker cannot kill, unlink, repair, delete, or mark a replacement runtime.
Absent/version-zero database initialization, schema migration, database restore, and any other
first-party whole-database replacement MUST also use this one boundary. Initialization MUST recheck
absence after acquiring the lock so it cannot race restore into an absent destination. When the
database parent is absent, lock acquisition MUST first establish it with the existing
private-directory policy so initialization and restore retain their supported absent-parent path.

Every runtime update performed after remote work MUST use an atomic compare-and-set against the
prepared socket and process fingerprint. A changed or missing row MUST remain untouched and become a
typed conflict failure even while its lifecycle lock is held. If an interrupted reconciliation
already committed the exact desired state, retry MUST recognize it as idempotent success rather than
a stale conflict.

### R11: Start and restart disposition

`session start --all` and `session restart --all` remain serial. Their launch path mixes
interaction, secret resolution, harness state, lifecycle state transitions, shared tmuxinator
regeneration, and database writes. Parallelizing them requires a separate design and MUST NOT be
smuggled into a generic helper built for stop.

### R12: Verification

Automated tests MUST prove:

- dedicated remote phases overlap under the fixed bound;
- same-VM dedicated sessions may overlap;
- legacy work is serial and does not overlap any other legacy mutation;
- workers do not access SQLite or output;
- duplicate socket or process identities refuse before mutation;
- successful results persist despite sibling failure;
- refined fingerprint evidence persists before a later failure;
- queued work is cancelled and running work reconciled on first interrupt;
- repeated interrupts cannot abandon reconciliation;
- concurrent lifecycle mutation is excluded and stale persistence is rejected atomically;
- interrupted post-commit reconciliation is retry-safe;
- single named stop retains current behavior; and
- force, residual, broken, timeout, and aggregate-failure behavior remains fail closed.

Tests MUST assert behavior and state, not authored prose.

## Success Criteria

1. A controlled batch of at least four independent dedicated sessions demonstrates overlapping
   remote teardown rather than serial execution.
2. No database object or output call crosses into a teardown worker.
3. One failed worker cannot erase or suppress a sibling's verified stopped state.
4. Interrupt tests prove the documented queued, in-flight, repeated-interrupt, and reconciliation
   boundaries.
5. Full local gates, private project review, Muntz review, cold correctness/security review, and
   exact-head shipped-CLI validation are clean.
6. Capability-appropriate live validation stops disposable dedicated sessions on one VM and across
   VMs, including at least one injected failure if the authorized environment permits it.

## Open Questions for Review

1. Is a 10-second per-call batch timeout too aggressive for any supported transport or teardown
   step?
2. Should legacy teardown run before or after the concurrent lane? The proposed design runs it after
   dedicated reconciliation so shared-server compatibility work cannot delay independent modern
   runtimes.
