# Session status internals

Agentworks derives session status live. The database does not cache a running/stopped flag. Each
current session row instead carries the dedicated tmux socket path, server PID, VM boot ID, and
Linux process start time captured when the server is created.

The persisted PID has three meanings:

- `NULL`: runtime identity evidence is incomplete, so the status is `UNKNOWN` until safe repair.
- `-1` (`PID_STOPPED`): the session is known to be stopped.
- A positive PID paired with a boot ID and process start time: the last observed tmux server
  identity. The start time prevents PID reuse from matching an older server.

Status checking asks the stored dedicated server for the exact canonical session name. Success is
`OK`. If that session is absent but the server still responds, the status is `RESIDUAL`: managed
windows or panes remain but the canonical session cannot be attached. If the server does not
respond, a changed VM boot ID or a provably absent fingerprint is `STOPPED`; a live matching
fingerprint with an unreachable socket is `BROKEN`. An SSH transport failure or indeterminate
identity is `UNKNOWN`, never evidence that the session stopped. Batch checks preserve the same
derivation while combining sessions on a VM into one SSH request.

Before a command checks status, an incomplete reachable row may be repaired from the server's
reported PID and a stable double-read of its boot ID and process start time. A provably absent
server is recorded as stopped. Indeterminate or mismatched live identity fails closed. Batched
checks use the VM admin connection and elevate only the tmux and process probes needed to inspect an
agent-owned runtime; this prevents Linux `/proc` visibility policy from masquerading as process
absence. The ordinary list view reports the resulting status in its table without narrating each
internal repair or normalization first; one aggregate status-check line explains the remote work
before the table appears.

Reachable dedicated runtimes stop with tmux `kill-server`, which removes the canonical session and
any operator-added sessions, windows, or panes on that dedicated server. Older shared-server rows
use an exact-name `kill-session` and verify that exact session is absent. Agentworks never signals a
stored numeric PID.

`--force` is intentionally narrow. It permits stale dedicated-socket cleanup only after the stored
boot/PID/start-time fingerprint proves the old server process is absent. It does not turn unknown
transport state, missing identity, PID reuse, or a matching live process into cleanup authority.

The implementation is split between `cli/agentworks/sessions/manager/_status.py` (status
derivation), `cli/agentworks/sessions/manager/_pids.py` (repair),
`cli/agentworks/sessions/manager/_lifecycle.py` (command policy), and
`cli/agentworks/sessions/tmux.py` (exact tmux operations and fingerprint capture). The central
regression suites are `cli/tests/test_session_liveness.py` and
`cli/tests/sessions/test_singular_batch_orchestrated.py`.
