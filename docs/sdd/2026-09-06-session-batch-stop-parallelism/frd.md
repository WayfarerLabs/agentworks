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
completion to the coordinator through futures.

The implementation builds on PR #764's database-use lock, which prevents live-database replacement
while the command owns a writable database. It adds no second cross-process lock.

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

- Parallelizing `session start --all` or `session restart --all`; issue #778 tracks that design.
- Changing named `session stop` semantics or making it asynchronous; issue #777 tracks measured
  single-stop optimization.
- Adding `--workers`, configuration, environment variables, or capability API changes.
- Adding machine-readable batch-stop output or changing a machine contract.
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
dedicated tmux socket and has a complete, valid boot ID, positive PID, and positive start ticks.
Eligible sessions MAY run concurrently even when they share a VM because each `-S SOCKET` address
selects an independent tmux server.

Before mutation, Agentworks MUST inspect the concurrent plans and fail closed if two selected plans
claim the same dedicated socket or the same VM/boot/PID process key. Start ticks remain part of each
worker's exact incarnation check, but differing stored start ticks do not make one live PID safe for
two workers to mutate. The parallelism invariant MUST be proved from validated persisted facts, not
inferred from a non-null socket.

A dedicated row with an incomplete fingerprint MUST keep the existing synchronous teardown path,
including its pre-kill persistence of newly observed start ticks. A legacy row with no dedicated
socket MUST use the existing exact `kill-session -t =NAME` behavior. Both categories MUST run
serially after the concurrent lane. The implementation MUST NOT migrate or widen either
compatibility path as part of this effort.

### R3: Fixed bounded fan-out

The concurrent lane MUST use a small fixed global ceiling of eight workers and MUST use fewer when
fewer dedicated sessions are selected. It MUST NOT create a worker per session without a bound.

The initial ceiling deliberately matches the existing fixed eight-worker read-only guest-observation
pool in `agentworks.status_observation`. The mutating lifecycle path MUST name its policy separately
because its mandatory drain behavior cannot depend on that module's cancellation helper.

The fixed value is implementation policy, not a public contract. Adding operator configuration
requires measured evidence that the fixed bound is inadequate and is outside this effort.

### R4: One-thread state ownership

Only the invoking coordinator thread MAY read or write the shared `Database` during concurrent
teardown. Workers MUST NOT receive the database object, call output helpers, prompt, resolve
secrets, activate VMs, alter provider state, or regenerate shared configuration.

Before submission, the coordinator MUST convert each eligible selected session into immutable work
containing only its complete validated runtime identity, target, privilege policy, and teardown
policy. After completion, the coordinator MUST apply returned evidence and render output.

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
mutable transport between worker tasks. Each eligible worker MUST preserve the current immediate
capture, identity comparison, `kill-server`, absence-proof, and socket-cleanup sequence without a
database or queueing checkpoint between capture and destructive use. PR #764's database-use lock
MUST remain held shared for the writable `Database` lifetime, while restore holds it exclusive
across replacement.

### R6: Bounded remote calls

Every transport created for a concurrent teardown MUST use an internal finite default timeout and
one attempt for each non-interactive remote call. The initial implementation value is 10 seconds and
MUST be confirmed or increased from supported-environment teardown evidence before lock. This bounds
one remote step, not the whole session: the complete teardown may execute several sequential steps.

The initial timeout and attempt count deliberately match `GUEST_OBSERVATION_TIMEOUT_SECONDS` and
`GUEST_OBSERVATION_ATTEMPTS`. The teardown constants MUST remain independently named because an
uncertain destructive call cannot inherit read-only retry or cancellation semantics.

Timeout is a failure for that session. Its complete persisted fingerprint MUST remain unchanged, and
the coordinator MUST NOT automatically repeat a destructive command whose outcome is uncertain.

The named synchronous stop path keeps its current transport policy. The concurrent timeout is a
batch worker safety boundary, not a global transport-policy change.

### R7: Completion and persistence

Each eligible worker MUST return verified stopped evidence on success. Ordinary worker exceptions
MUST remain future exceptions; the coordinator's future-to-plan mapping provides session attribution
without a second error wrapper.

The coordinator MUST consume completed futures as they become available and atomically
compare-and-set a verified stopped result before announcing success. A database-write failure MUST
be reported as failure even if the remote runtime was already stopped. A worker-level
`BaseException` drains sibling work and then propagates; it MUST NOT be misreported as a reconciled
stopped outcome.

One session failure MUST NOT cancel sibling sessions. The final command failure MUST retain the
existing aggregate behavior after all eligible work and reconciliation complete.

### R8: Operator output

The human command MUST report the batch count before submitting remote mutations. Each completed
session MUST then receive a session-labeled success or warning from the coordinator. Completion
lines MAY appear in completion order; they MUST NOT rely on adjacency to identify their session.

If no concurrent teardown completes for five seconds, the coordinator MUST emit a compact heartbeat
containing completed, active, and queued counts. It MUST repeat at no more than five-second
intervals while that concurrent wait remains quiet, including interruption reconciliation. A
normally fast concurrent lane therefore has no heartbeat noise, while its slow transport work never
leaves the operator with unexplained silence. Existing pre-submission preflight, activation, repair,
and status progress remains outside this heartbeat contract.

Workers MUST emit no output.

### R9: Interruption

On the first operator interrupt after submission, the coordinator MUST:

1. cancel futures that have not started;
2. report that active teardowns are being reconciled;
3. continue consuming already-completed and in-flight bounded futures;
4. apply successful stopped evidence on the main thread; and
5. propagate interruption after reconciliation.

Running thread work cannot be cancelled safely and MUST NOT be described as cancelled. A second or
later interrupt MUST repeat the visible reconciliation notice and continue draining. The command
MUST NOT promise an immediate escape that Python's executor shutdown cannot deliver.

### R10: Existing batch boundary and state fencing

Agentworks MUST retain the current filter validation, selection, VM graph validation, activation
holds, PID repair, batch status observation, broken-state selection, and `--force` gate before
destructive work. An unknown actionable session MUST retain the current whole-batch refusal before
mutation. The VM activation boundary and writable `Database` MUST remain held until every submitted
worker has completed and its result has been reconciled.

Every runtime update performed after remote work MUST use an atomic compare-and-set against the
prepared complete socket and process fingerprint. A changed or missing row MUST remain untouched and
become a typed conflict failure. If an interrupted reconciliation already committed the exact
desired state, retry MUST recognize it as idempotent success rather than a stale conflict.

### R11: Start and restart disposition

`session start --all` and `session restart --all` remain serial. Their launch path mixes
interaction, secret resolution, harness state, lifecycle state transitions, shared tmuxinator
regeneration, and database writes. Parallelizing them requires a separate design and MUST NOT be
smuggled into a generic helper built for stop.

### R12: Verification

Automated tests MUST prove:

- dedicated remote phases overlap under the fixed bound;
- same-VM dedicated sessions may overlap;
- incomplete dedicated and legacy work stay serial after the concurrent lane;
- workers do not access SQLite or output;
- duplicate socket or VM/boot/PID process keys among concurrent plans refuse before mutation,
  including when duplicate process keys carry different stored start ticks;
- successful results persist despite sibling failure;
- incomplete-fingerprint teardown retains the synchronous pre-kill persistence checkpoint;
- queued work is cancelled and running work reconciled on first interrupt;
- repeated interrupts cannot abandon reconciliation;
- stale persistence is rejected atomically;
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
2. Should serial compatibility teardown run before or after the concurrent lane? The proposed design
   runs it afterward so incomplete or shared-server work cannot delay independent modern runtimes.
