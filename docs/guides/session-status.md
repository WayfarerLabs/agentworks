# Session status internals

Agentworks derives session status live. The database does not cache a running/stopped flag. Each
session row instead carries the tmux server PID and the VM boot ID captured when the server is
created or resumed.

The persisted PID has three meanings:

- `NULL`: PID or boot-ID evidence is incomplete, so the status is `UNKNOWN` until auto-repair.
- `-1` (`PID_STOPPED`): the session is known to be stopped.
- A positive PID paired with a boot ID: the last observed tmux server identity.

Status checking tries `tmux has-session` first. Success is `OK`. After failure, a changed VM boot ID
or a missing `/proc/<pid>` is `STOPPED`; a live PID from the same boot is `BROKEN`, meaning the tmux
server exists but its socket is unreachable. An SSH transport failure or inability to establish the
boot cycle is `UNKNOWN`, never evidence that the session stopped. Batch checks preserve the same
derivation while combining all sessions on a VM into one SSH request.

Before a command checks status, incomplete rows are repaired automatically. A reachable tmux session
supplies a fresh PID and boot ID. A missing server is recorded as `PID_STOPPED`. If the socket
exists, Agentworks probes it with elevated access: a stale socket is marked stopped, while a live
but inaccessible server remains unresolved and produces an error for manual investigation.

`--force` is intentionally narrow. It authorizes PID-based termination only after status is
`BROKEN`, which proves that the PID is alive in the current boot. Agentworks sends SIGTERM,
escalates to SIGKILL if necessary, and removes a stale socket only after the process is gone.
`--force` does not turn unknown transport state into permission to kill, and it does not replace
confirmation semantics for an ordinarily running session.

The implementation is split between `cli/agentworks/sessions/manager/_status.py` (status
derivation), `cli/agentworks/sessions/manager/_pids.py` (repair),
`cli/agentworks/sessions/manager/_lifecycle.py` (command policy), and
`cli/agentworks/sessions/tmux.py` (PID capture and force-kill mechanics). The central regression
suites are `cli/tests/test_session_liveness.py` and
`cli/tests/sessions/test_singular_batch_orchestrated.py`.
