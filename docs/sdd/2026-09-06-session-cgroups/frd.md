# Session Cgroups: Functional Requirements

- Status: Draft FRD/HLA checkpoint; no merge or implementation intent.
- Date: 2026-09-06.
- Saga: [next-steps](../2026-08-04-next-steps/target-state.md).
- Related evidence: [issue #715](https://github.com/WayfarerLabs/agentworks/issues/715).
- Authority: the operator requested this draft in the session on 2026-09-06, limited the effort to
  agent session workloads, and authorized up to three published feedback/fix rounds. Requirements
  below are proposed for acceptance; review comments are input, not authorization.

## Problem and outcome

An agent session's tools can detach from its terminal and survive after Agentworks reports the
session stopped. Environment variables identify cooperative processes but cannot establish a
security identity for a compromised tool. Operators need one durable association between an active
session execution and its processes, and one reliable way to end that execution completely.

The desired result is agent session containment that survives forks, replacement of executable
images, terminal detachment, and deliberate attempts to evade cleanup. A process's association is
observable by trusted VM infrastructure without trusting that process's environment or claims.

## Scope and threat model

The subject is the workload inside the managed Debian VM: each agent-mode session's tmux server, its
harness or shell, additional panes created in that server, and all processes they launch. The
operator, VM administrator, kernel, system service manager, and installed lifecycle machinery are
trusted. Workload code is untrusted, including tools and shell startup files controlled by the
agent.

Agent identities remain durable Linux users. Retaining the existing user, home, credentials, and
workspace grants is the proposed compatibility goal. Same-user execution channels are part of the
threat analysis, not assumed safe because a caller and target share an agent identity. A requirement
that cannot be met with this goal returns for operator disposition before implementation.

This is process lifetime and execution-context identity, not comprehensive isolation of everything
sessions share. Shared writable source, home files, and credentials remain shared. Identity states
where a process executes, not who influenced its input or authored code it later executes. The
security claim excludes a trusted administrator deliberately moving work, kernel or trusted-manager
compromise, and independently authorized execution on another machine. It does not promise to erase
files or revoke external credentials when a run ends. These exclusions cannot excuse an exposed
local execution service that a workload can use to evade its own lifetime.

## Requirements

### R1. Execution identity

Each logical session has a non-reused identity independent of its reusable display name. Each new
workload incarnation has a distinct run identity. The pair remains associated with every live
process in that execution despite environment edits, fork, exec, reparenting, terminal detachment,
or creation of further tmux panes and sessions inside the owned server.

A workload cannot relabel itself as another run, shed the association, or forge a successful lookup.
Trusted callers can obtain the logical session and run identity, or a clear
unrecognized/indeterminate result. VM boot identity and numeric process IDs are supporting
observations, not substitutes for session/run identity. A restart or delete-and-recreate must not
inherit a prior run's authority.

### R2. Complete termination

Stop, restart, delete, failed-launch rollback, and cascading agent/workspace cleanup use the same
run ownership boundary. Graceful termination gets a bounded interval, followed by forced termination
of the whole owned process set. Ignoring signals, daemonizing, and forking during shutdown must not
leave executing survivors outside the boundary.

Success requires evidence that no live owned process remains. Loss of connectivity or an
indeterminate teardown is not success. A kernel task that cannot finish termination must remain
reported as incomplete; the requirement is not an impossible promise of instantaneous disappearance
from every kernel state. Restart does not launch a replacement until the prior run is proven empty.
VM reboot or destruction can prove the old execution gone when the changed boot or destroyed VM is
independently verified; suspend and transport loss cannot.

### R3. Independent lifetime authority

The VM owns cleanup independently of the workstation CLI and SSH connection. Workloads remain
persistent across ordinary detach, workstation failure, and SSH loss. Ending the owned session
runtime, including loss of its tmux server, triggers cleanup of remaining workload processes without
requiring a connected workstation. Preserve normal pane-exit behavior: when the last workload pane
exits and the server ends, remaining descendants are cleaned up. Temporary retention of a failed
launch pane for diagnosis must not allow a failed run to remain indefinitely alive.

### R4. Resistance to escape and relaunch

Untrusted workload code cannot administer its enclosing boundary, move itself out, join another run,
replace the trusted lifecycle machinery, or ask that machinery to launch an unowned process.
Containment applies before any agent-controlled code executes.

Local indirect execution must also be addressed: sibling tmux servers, agent login paths, user
service managers, schedulers, process injection, container daemons, and inherited control handles.
Supported channels either execute inside the originating run or are inaccessible to its workload. A
documented warning alone does not satisfy this requirement. No background task or restart policy
registered by the workload may recreate its execution after the run has ended.

### R5. Usable sessions and explicit boundaries

Normal supported harness operation, terminal attachment, detached persistence, workspace grants, and
operator inspection continue to work. Agent-side companion shells associated with a session must
have an explicit lifetime owner; they cannot silently launch outside its protection. Admin shells
and admin-mode sessions receive no new containment guarantee in this effort.

Administrator actions remain privileged and cannot be impersonated by agent workloads. No broad
sudo, service-manager, or cgroup-management grant is made to an agent to enable session launch.
Compatibility restrictions needed to satisfy R4 are review decisions, with their concrete effects on
normal development workflows demonstrated before the design is accepted for implementation.

### R6. Supported environments and migration

Both Debian Bookworm and Trixie remain supported. Capability checks examine the actual running
kernel and VM setup, including WSL2; the release name alone is insufficient. An unsupported or
unsafe setup refuses a new protected launch with actionable diagnosis rather than silently falling
back to terminal-only cleanup.

Existing running sessions are not retroactively certified by moving a known parent process. The
migration must identify legacy executions honestly, preserve control of them, and require proof that
old work is gone before establishing the new guarantee. Reinitialization installs prerequisites
idempotently; a session operation diagnoses missing setup without repairing VM/agent scope itself.

### R7. Foundation for permission checks

Trusted VM-side code can resolve a live caller's execution identity without accepting a supplied
session name or environment value as proof. The lookup contract handles process exit, PID reuse,
namespace-relative identifiers, stale runs, and unknown membership by refusing to authenticate.

A test consumer must demonstrate the lookup at a Unix-domain-socket boundary. This effort does not
ship a general permissions service, permission vocabulary, or user-facing grant commands. A future
service must define per-request authentication, connection/descriptor transfer, and revocation;
connection-time credentials alone are not a complete authorization protocol.

## Acceptance evidence

Implementation acceptance requires live evidence on Bookworm and Trixie for:

| Case                                                                      | Required observation                                                        |
| ------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Fork/exec, environment removal, double fork, new terminal session         | All processes retain the originating run association.                       |
| Added tmux panes and session-associated agent shells                      | Each workload has the correct run owner.                                    |
| TERM ignored, concurrent forks, tmux server killed                        | Forced cleanup leaves the group empty; completion is reported truthfully.   |
| CLI death, SSH loss, detach/reattach                                      | The remote run persists and remains independently manageable.               |
| Stop/restart/delete and cascading cleanup                                 | Exactly the selected runs stop; unrelated runs remain alive.                |
| Reused names, stale process IDs, stale run records                        | No old identity grants authority over a replacement run.                    |
| Direct migration and every reachable local execution channel              | Escape/relaunch is denied or the resulting process belongs to the same run. |
| Identity socket, exiting peers, transferred descriptors                   | Recognized callers resolve correctly; ambiguous attribution fails closed.   |
| Partial launch, persistence failure, VM reboot, unsupported prerequisites | No false success, untracked protected run, or silent downgrade.             |
| Legacy detached child                                                     | Migration does not certify safety merely because tmux exited.               |

Tests assert observed behavior, not wording of these documents. The current checkpoint supplies no
live evidence and makes no implementation-completion claim.

## Out of scope and decisions requested

Workstation attachment cleanup, admin-session hardening, resource quotas/accounting, full
cross-session data isolation, a general sandbox platform, and a permissions product are out of
scope. Existing external credentials are not made safe to expose merely by adding cgroups.

Review should determine whether the proposed same-user boundary can meet R4 without unacceptable
workflow changes, approve the lifetime treatment of companion shells, and settle the migration
interruption policy. These are unresolved design decisions, not permission to weaken R1-R7. The
[HLA](hla.md) names the proof work needed to answer them.
