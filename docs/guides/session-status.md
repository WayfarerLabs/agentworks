# Session status internals

Agentworks derives session status live. The database does not cache a running/stopped flag. Each
current session row instead carries the dedicated tmux socket path, server PID, VM boot ID, and
Linux process start time captured when the server is created.

The persisted PID has three meanings:

- `NULL`: runtime identity evidence is incomplete. A reachable exact session is `RUNNING` only after
  its native launch metadata matches the recorded session and run identities. Otherwise it remains
  `UNKNOWN`. A confirmed different VM boot proves that the earlier runtime cannot survive.
- `-1` (`PID_STOPPED`): supporting evidence for stopped after the exact session and its dedicated
  server are authoritatively absent. It never overrides a resurrected live runtime.
- A positive PID paired with a boot ID and process start time: the last observed tmux server
  identity. The start time prevents PID reuse from matching an older server.

Status checking asks the stored dedicated server for the exact canonical session name. For a row
with a recorded positive PID, success is `RUNNING`. If that session is absent but the server still
responds, the status is `RESIDUAL`: managed windows or panes remain but the canonical session cannot
be attached. If the server does not respond, a changed VM boot ID or a provably absent fingerprint
is `STOPPED`; a live matching fingerprint with an unreachable socket is `BROKEN`. An SSH transport
failure or indeterminate identity is `UNKNOWN`, never evidence that the session stopped. A stopped
row with a present unrecorded server is also `UNKNOWN`: a socket path locates a server but does not
establish ownership.

Inspection is read-only. Existing positive-PID rows retain their ordinary presence checks without
eager identity repair. Batched checks combine the normal probes into one remote request per
reachable VM. A pending launch with no recorded PID requires additional bounded reads to verify its
native attempt identity and stable process fingerprint. These reads do not adopt the runtime or
repair its row. Incomplete or indeterminate evidence stays `UNKNOWN`. Probes elevate only where
needed to inspect agent-owned runtimes. Each guest call is non-interactive, makes one attempt, and
has a 10-second timeout. Calls without fact input explicitly close stdin so Windows SSH does not
retain the operator's console handle after returning the complete result.

Plain `agw session list` is local inventory and does not run these probes. Add `--status` to request
them; the table adds `STATUS`, reports progress before SSH begins, and appends compact uptime to a
running status when the session has a known start time. A running session without one remains
`running`. `agw session describe NAME` always performs one non-activating observation before
rendering its configured facts. A stopped or unreachable VM remains stopped and yields `UNKNOWN`
rather than being started for inspection.

A launch records the current boot after socket checks, immediately before dispatching the
spawn-capable command. A failed checkpoint prevents dispatch; a lost acknowledgement afterward
preserves an uncertain outcome. Both admin and agent launches retain their dedicated socket address.
A lifecycle command may recover an incomplete fingerprint only when native metadata matches the
existing session and run identities and the process fingerprint remains stable. Complete recorded
fingerprints retain their authority without requiring that metadata. A row missing its process start
time cannot gain destructive authority merely by observing a live PID on the same boot.
Indeterminate or mismatched live identity fails closed without adopting or killing the server.

During teardown, completing an incomplete fingerprint writes only if the original session, run,
socket, and runtime fields still match. Preparation then rereads that identity and captures the
native fingerprint afresh before destruction. A concurrent replacement cannot be adopted or
overwritten by that upgrade.

Mutating PID repair likewise copies the original session, run, and nullable runtime fields before
native observations. Learning a boot, recovering a fingerprint, and recording absence use
conditional writes with the existing runtime-shape validation. A changed or removed row produces a
conflict without overwriting it or announcing successful repair. Batch preparation stops on that
conflict before status or teardown proceeds. A failed repair database write also stops preparation
and preserves its original driver error, including a stale SQLite snapshot inside an enclosing
transaction; ordinary unrecoverable or unreachable repairs retain their existing refusal behavior.
This is a persistence fence, not whole-command serialization: another command can still change a row
after a successful repair and before the next refetch or phase.

A provably absent runtime can be recorded as stopped. Older failed admin rows with no socket address
can recover after exact-session absence, dedicated-server absence and evidence that the old runtime
cannot survive; unrelated sessions on the default server do not prevent that repair. Recovery uses
the ordinary per-session lifecycle commands and does not require deleting a workspace.

Keep manually created tmux servers on separate sockets. A native server created at a managed socket
between the last startup check and dispatch can still race initial creation. These checks do not
isolate processes running as the same Linux user.

`session start` can finish an interrupted startup on the existing runtime after recovering its
identity. It completes access sharing, checks the recorded launch pane and releases startup
protection; it does not create a second workload. Native launch-pane metadata identifies that pane
even when the workload opens another window. Completing startup on an older runtime without that
metadata requires exactly one unambiguous pane in its named session. Healthy older runtimes retain
ordinary no-op start and active-pane log capture. Malformed metadata is refused; the legacy log
fallback does not establish identity or authorize recovery mutations.

If no process identity can be recovered and the same VM boot is still running, preserve the row and
inspect the guest. `--force` cannot turn missing identity into evidence of absence. A mutating
repair attempt can record the observed boot for an older row that lacks one. An actual VM reboot
followed by a lifecycle retry can then establish absence from the changed boot. Rebooting interrupts
every workload on the VM and requires the operator's approval; when that impact is unsuitable, leave
the row intact and investigate the runtime instead. Deleting the whole VM is not required for this
recovery.

Reachable dedicated runtimes stop with tmux `kill-server`, which removes the canonical session and
any operator-added sessions, windows, or panes on that dedicated server. Older shared-server rows
use an exact-name `kill-session` and verify that exact session is absent. Agentworks never signals a
stored numeric PID.

`agw session stop --all` runs complete-fingerprint dedicated teardown operations concurrently, with
a fixed ceiling of eight active sessions. Each worker owns one fresh transport and performs only
remote identity validation, tmux teardown, absence proof, and exact socket cleanup. The invoking
thread retains database and output ownership, persists verified results before announcing success,
and runs incomplete dedicated or shared-server compatibility work serially after the concurrent
lane. A failure in one session does not cancel its siblings.

Before submission, an actionable unknown runtime refuses the entire destructive batch. Stopped
intent accepted by the command remains recorded for retry. Safe PID repair and fresh observation
precede that decision after shared workload preparation; a failed repair write prevents status or
teardown from continuing with stale identity. Named stop remains synchronous, and an off parent is
never activated solely to stop its children.

Named and batch teardown share the same remote protocol for complete-fingerprint runtimes. Every
teardown stopped-state update, including incomplete-runtime recovery, stopped-row socket cleanup,
and legacy exact-session teardown, compares the original session, run, and nullable runtime fields
copied before remote observations. A changed identity causes a conflict instead of overwriting the
newer state. Recording incomplete or absent identity for persistence does not authorize native
cleanup or make a row eligible for parallel teardown.

An interrupt cancels teardown that has not started but cannot terminate a running worker safely.
Agentworks visibly drains bounded in-flight work, persists every verified result, and then exits as
interrupted. During a quiet concurrent wait it reports completed, active, and queued counts every
five seconds.

`--force` is intentionally narrow. It permits stale dedicated-socket cleanup only after the stored
boot/PID/start-time fingerprint proves the old server process is absent. It does not turn unknown
transport state, missing identity, PID reuse, or a matching live process into cleanup authority.

The implementation is split between `cli/agentworks/sessions/manager/_status.py` (status
derivation), `cli/agentworks/sessions/manager/_pids.py` (repair),
`cli/agentworks/sessions/manager/_lifecycle.py` (command policy),
`cli/agentworks/sessions/manager/_teardown.py` (dedicated teardown and batch coordination), and
`cli/agentworks/sessions/tmux.py` (exact tmux operations and fingerprint capture). The central
regression suites are `cli/tests/test_session_liveness.py`,
`cli/tests/sessions/test_singular_batch_orchestrated.py`, and the focused batch teardown tests.
