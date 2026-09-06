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

At source baseline `d7dfd6986d03daaa011f1c8a1d390cf25efb04fd`:

- batch stop performs VM gating and status observation before mutation;
- it creates one admin transport per VM and reuses it across selected sessions;
- `_execute_stop` calls `_teardown_session` serially;
- `_teardown_session` mixes remote probes and mutations with SQLite writes;
- dedicated sessions use independent persisted tmux socket paths;
- legacy rows may share the default tmux server;
- session runtime mutators have no cross-process exclusion boundary;
- runtime persistence updates by name without comparing prepared identity;
- named stop, restart, and deletion reuse the same teardown authority; and
- batch start and restart remain serial orchestration with interaction and shared-file mutation.

## Cutover Sequence

### 1. Add lifecycle exclusion and state fencing

Generalize the existing SQLite sidecar migration lock into one database mutation lock and add atomic
runtime compare-and-set. Route absent/version-zero initialization, migration, every session runtime
mutator, and live-database restore through that boundary before introducing worker threads.
Initialization rechecks absence under the lock. After lifecycle acquisition, reload named or
filtered session rows and their relationships before deriving status or teardown plans. Add the
affected-VM socket and fingerprint collision gate. The generalized helper creates a missing database
parent with the existing owner-private policy before opening the sidecar, preserving first-open and
restore support for fully absent destinations.

### 2. Extract value-based dedicated teardown

Introduce one immutable dedicated plan and outcome that reuses the existing tmux fingerprint. Split
database preparation and reconciliation from remote execution while preserving the current
synchronous dispatcher and untouched legacy helper. Move all existing dedicated callers through that
dispatcher before introducing parallelism.

At this point behavior remains serial. Existing teardown regression tests must be green.

### 3. Add batch-only transport policy

Create one transport per dedicated batch task with an initially 10-second finite default timeout and
the existing one-attempt policy. Confirm or adjust the value from live teardown evidence before
lock. Keep named and cascading transports unchanged.

### 4. Add the dedicated concurrent lane

Submit prepared dedicated work to the fixed worker pool, consume results in completion order, and
reconcile each on the invoking thread. Run legacy rows through the existing synchronous helper
afterward.

### 5. Add interruption reconciliation and heartbeat

Implement queued cancellation, first-interrupt draining, repeated-interrupt reconciliation, and a
five-second quiet-wait heartbeat before considering the concurrent path complete.

### 6. Update permanent collateral

Update the active command reference and session lifecycle guide to say that batch stop overlaps
independent dedicated teardowns, remains partially successful, and treats interruption as a
reconciliation boundary. Historical locked SDDs remain historical. The final locked record for this
effort captures the implemented head after live validation.

## Compatibility

| Surface                        | Before 0.19                 | 0.19 result                   |
| ------------------------------ | --------------------------- | ----------------------------- |
| `session stop NAME`            | synchronous exact teardown  | unchanged                     |
| `session stop --all [filters]` | serial exact teardown       | bounded dedicated parallelism |
| `session start --all`          | serial                      | unchanged                     |
| `session restart --all`        | serial                      | unchanged                     |
| legacy shared-server stop      | serial exact `kill-session` | unchanged                     |
| schema and runtime fingerprint | current persisted fields    | unchanged                     |
| harness and VM capability APIs | version 1                   | unchanged                     |
| Python runtime dependencies    | current set                 | unchanged                     |

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

- dedicated rows enter the concurrent lane;
- legacy rows retain the serial compatibility lane; and
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

Control: one attempt only, no blind retry, fail closed, retain learned fingerprint evidence, and
direct the operator to status inspection and an idempotent retry.

### Shared tmux state is mutated concurrently

Control: a pre-mutation collision scan proves unique socket and live fingerprint ownership for the
affected VMs. Only dedicated socket plans enter the pool. Legacy default-server work remains serial.

### A concurrent lifecycle command replaces the prepared runtime

Control: initialization, migration, every session runtime mutator, and Agentworks database restore
hold the same database-scoped SQLite sidecar lock across their mutation boundary. Initialization
rechecks absent/version-zero state after acquisition. Reconciliation also compare-and-sets the
prepared identity and recognizes an already-committed desired state after an interrupted attempt.

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
