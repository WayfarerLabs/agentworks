# Session Batch Stop Parallelism: Prior-Art Research

- Status: Design
- Date: 2026-09-06
- Research basis: official documentation and current Agentworks source inspected on 2026-09-06

## Executive Summary

Python's futures API supports the desired bounded worker pool and completion-order collection, but
its cancellation contract is narrower than it first appears: queued futures can be cancelled while
running futures cannot, and executor threads are joined before Python exits. That makes explicit
reconciliation, finite remote calls, and honest interruption output requirements rather than
implementation details.

Python's SQLite contract and Agentworks' connection usage support keeping all persistence on the
invoking thread. tmux's independent server sockets support session-level concurrency for modern
rows, while its shared default server argues for preserving a serial legacy lane. Agentworks ADR
0015 rules out connection caching as the optimization mechanism.

## Findings and Design Consequences

### 1. Futures bound concurrency, not task duration

The
[Python `concurrent.futures` documentation](https://docs.python.org/3/library/concurrent.futures.html)
defines `ThreadPoolExecutor(max_workers=N)` as a bounded asynchronous execution pool and
`as_completed` as completion-order iteration.

Design consequence: use a fixed maximum of eight independent teardown tasks and consume outcomes as
they complete. Per-call transport timeouts, not the executor, provide time bounds.

### 2. Cancellation cannot stop a running teardown

The same futures documentation states that `Future.cancel()` does not cancel a call that is already
executing. `Executor.shutdown(cancel_futures=True)` cancels only pending work; running futures
finish. Python also waits for pending executor futures before interpreter exit, and thread-pool
workers are joined before interpreter shutdown.

Design consequence: the first interrupt cancels queued work but reconciles running mutations. The
CLI must not claim active teardown was cancelled or offer a second-interrupt escape that the Python
runtime cannot provide. Finite remote calls and visible heartbeats make draining bounded and
observable.

### 3. SQLite connection ownership should not cross workers

The [Python `sqlite3` documentation](https://docs.python.org/3/library/sqlite3.html) documents
`check_same_thread=True` as the default: using a connection from another thread raises
`ProgrammingError`. Disabling it places serialization responsibility on the application. The
[SQLite threading documentation](https://www.sqlite.org/threadsafe.html) separately describes the
library's compile-time and connection threading modes.

Design consequence: do not disable Python's guard and do not add one connection per worker. Remote
tasks return values; the existing connection is used only by the invoking thread. This preserves a
single persistence order and avoids inventing lock or transaction policy for a performance change.

### 4. tmux socket selection creates independent servers

The [tmux manual](https://man7.org/linux/man-pages/man1/tmux.1.html) documents `-S socket-path` as a
full alternative server socket and `-L socket-name` as selecting a separate server socket. One tmux
server manages its own sessions, windows, and panes.

Design consequence: current sessions with distinct validated persisted `-S` sockets are independent
mutation units, including when hosted on the same VM. Legacy rows on the default socket may share
one server and remain serial with exact session targeting.

### 5. The existing SQLite sidecar lock already supplies exclusion

The [SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html) states that
`BEGIN IMMEDIATE` starts the write transaction immediately and fails with `SQLITE_BUSY` when another
write transaction is active. Agentworks already uses that operation on a dedicated sidecar database
to serialize migration across processes, with controlled timeout and release by rollback/close.

Design consequence: generalize that existing helper into one database mutation lock rather than add
a second lock package. Migration retains its bounded wait; session lifecycle and restore acquire the
same sidecar non-blocking. Every runtime mutator and Agentworks live-database replacement must
participate because an atomic database update alone cannot stop stale remote work from touching a
replacement socket. One shared sidecar also prevents schema migration and lifecycle mutation from
mistakenly proceeding under independent locks.

### 6. Fresh transports preserve current authentication behavior

[ADR 0015](../../adrs/0015-abandon-ssh-controlmaster.md) removed SSH ControlMaster reuse because
cached connections made current PAM, NSS, and group membership unreliable after user changes.

Design consequence: parallel speedup comes from overlapping fresh non-interactive commands, not from
cached SSH control connections. Each concurrent task receives a distinct transport object and opens
ordinary fresh SSH subprocesses.

## Refuted or Deferred Approaches

### "ThreadPoolExecutor cancellation makes Ctrl-C immediate"

Refuted. Python cannot cancel already-running thread calls, and executor shutdown does not terminate
them. Immediate exit after mutation begins would abandon reconciliation or promise more than the
runtime can do.

### "A compare-and-set alone prevents stale remote mutation"

Refuted. It can reject a stale database write after remote work, but it cannot prevent that worker
from killing or unlinking a replacement at the same socket. Lifecycle exclusion must span final
status, remote work, and persistence; compare-and-set is a second fence.

### "SQLite serialized mode makes the shared connection safe"

Refuted as an application design. Even if the linked SQLite library is serialized, Python's
connection guard rejects cross-thread use by default. Disabling it would still require explicit
write serialization and failure ordering.

### "Same VM means same mutation lock"

Refuted for current dedicated rows. The persisted alternative socket is the tmux server boundary.
Same-VM sessions do share host resources, which is why the pool remains globally bounded, but they
do not share the tmux object being destroyed.

### "Parallel start and restart are the same optimization"

Deferred. Their session-level path owns interactive and shared mutable concerns absent from stop.
The safe teardown split is useful prior art for a future design, not permission to reuse its worker
contract unchanged.

### "Batch all remote steps into one shell script"

Deferred. It might improve named-stop latency, but it changes the audited identity,
partial-evidence, and diagnostic boundaries. Safe overlap solves issue #730's primary cost without
that rewrite.

## Sources

| Source                                                                      | Quality                  | Design use                                 |
| --------------------------------------------------------------------------- | ------------------------ | ------------------------------------------ |
| [Python futures](https://docs.python.org/3/library/concurrent.futures.html) | Primary language docs    | pool bound, completion, cancellation, exit |
| [Python SQLite module](https://docs.python.org/3/library/sqlite3.html)      | Primary language docs    | default same-thread connection rule        |
| [SQLite threading mode](https://www.sqlite.org/threadsafe.html)             | Primary upstream docs    | library-level threading distinction        |
| [tmux manual](https://man7.org/linux/man-pages/man1/tmux.1.html)            | Primary upstream manual  | independent alternative server sockets     |
| [SQLite transactions](https://www.sqlite.org/lang_transaction.html)         | Primary upstream docs    | `BEGIN IMMEDIATE` exclusion and contention |
| [Agentworks ADR 0015](../../adrs/0015-abandon-ssh-controlmaster.md)         | Current project decision | no cached SSH connection optimization      |

## Questions Left to Implementation Evidence

1. Does the 10-second per-call limit cover every supported transport in representative live
   teardown, or should the batch-only constant be increased before lock?
2. How much wall-clock improvement does session-level overlap provide for four, eight, and larger
   selections on one VM and across VMs?
3. Does live interruption complete reconciliation promptly enough under the five-second heartbeat
   contract?
