# Session Cgroups: High-Level Architecture

- Status: Draft response to the [FRD](frd.md); review checkpoint, no merge intent.
- Saga: [next-steps](../2026-08-04-next-steps/target-state.md).
- Baseline inspected: `b22cc49c984aa91e8205e035c5766b5a2aede90a` (2026-09-06).

## Architectural choice

Use cgroup v2 with one system-owned transient service per agent session run. The system service
manager owns process lifetime; tmux owns terminals and presentation. Run tmux and the workload as
the existing agent Linux user. Do not delegate management of the enclosing run boundary to that
user, and do not rely on a user service manager, login persistence, or a workstation process to own
cleanup. [Prior-art research](prior-art-research.md) gives the sources and rejected alternatives.

This is a proposed foundation, not a claim that system-owned groups alone meet FRD R4. The same-user
execution channels below are an implementation gate. The draft deliberately does not prescribe an
untested sandbox profile or silently relax containment to best-effort cleanup.

## Current seams

| Concern                 | Current code at the inspected baseline                      | Consequence                                                                                                                              |
| ----------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Launch                  | `cli/agentworks/sessions/tmux.py:681`, `:792`               | A dedicated server is created through the owning user's transport. Launch must enter containment before executing agent-controlled code. |
| Teardown                | `cli/agentworks/sessions/manager/_lifecycle.py:190`, `:269` | Killing and proving absence of tmux does not prove absence of detached descendants.                                                      |
| Runtime record          | `cli/agentworks/db/models.py:149`                           | Stores socket, PID, boot ID, and start ticks; no logical session UUID or run ID yet.                                                     |
| Direct agent transport  | `cli/agentworks/sessions/manager/_pids.py:38`               | Lifecycle privilege assumptions change; preserve least privilege without granting the workload management authority.                     |
| Agent SSH authorization | `cli/agentworks/vms/initializer/ssh_keys.py:108`, `:122`    | Agent owns both `.ssh` and `authorized_keys`; a self-installed key can open an outside login where SSH is reachable.                     |
| Console attachment      | `cli/agentworks/sessions/multi_console/attach.py:150`       | A persistent wrapper attaches to the workload's separate server. Attachment and workload ownership must remain distinct.                 |
| Companion shells        | `cli/agentworks/sessions/multi_console/tmux_build.py:360`   | Shells are composed with console windows; session-associated agent shells need an explicit contained launch path.                        |

## Components and trust boundary

The existing workstation orchestrator remains responsible for validation, the whole secret
resolution boundary, and composing operations over resource nodes. VM readiness checks verify that
the installed session runtime supports the required containment contract. VM and agent init/reinit
own prerequisite installation, per the saga's scope participation model.

A narrow, root-owned VM launch/control entrypoint bridges authenticated operator control to systemd.
It supports starting, observing, and stopping a specifically identified run. It is not a root shell
or an API for arbitrary unit properties. Workload launch commands execute only after the service has
set the agent user and containment; neither shell startup nor an integration command runs as root.
The installed entrypoint, service definitions, and run registry are not writable by agent users.

The initial transport proposal uses the existing authenticated admin SSH control path to invoke this
entrypoint. Agent workloads do not receive the administrator's credentials or invocation rights.
This changes the present direct-agent-SSH lifecycle implementation and must be recorded as such; it
does not change the user that executes the workload. The LLD must specify argument and identity
validation, privilege dropping, supplementary groups, secret delivery, and handle closure. Secrets
must not become world-readable service properties, command-line metadata, logs, or files. An
executable name or path controlled by the agent must never select a privileged program.

Systemd is the single cgroup manager. A foreground tmux server, or a minimal supervised runtime
whose lifecycle is tied to that server, anchors service lifetime. Prefer the foreground server if it
supports the required startup and retained-pane behavior; prove this on both releases before
choosing a wrapper. A service whose lifetime is simply "while any process remains" would let a rogue
descendant prolong a dead session, so that is not the lifetime rule.

## Identity and registration

Use the saga's existing logical model: `session_uuid` identifies a logical session and `run_id`
identifies each workload incarnation. See the
[scope participation contract](../2026-08-04-next-steps/scope-participation-contract.md#session-and-run-identity).
Its historical reference to "resume" maps here to actual new incarnations from create, start, or
restart; attaching or observing does not create a run. These fields are not implemented at the
inspected baseline. Coordinate their introduction with the saga's observability work instead of
adding a competing generation identifier. No event-stream implementation is required here.

Service names are derived from validated immutable identities, with the display name available for
operator readability. Names are not credentials. A protected VM record binds the logical session,
run, owning UID, VM boot identity, and systemd unit to the observed cgroup. The workstation persists
the reference for lifecycle operations. Raw paths or cgroup inode numbers are not everlasting
identities; deletion/reuse and reboot invalidate a run's live binding.

Reserve a run identity before remote launch. The VM registers an independently discoverable pending
run before starting it and records the resulting service association. A disconnected client can
reconcile that exact request without launching a duplicate. A partial launch either leaves a
recoverable owned record or is stopped and proven empty. Database failure must not strand an
unaddressable workload. The LLD defines serialization of competing start/stop requests and recovery
from crashes between each step; workload callers cannot reserve identities or adopt arbitrary units.

## Lifecycle and observation

1. Validate scope, resolve inputs, and probe required runtime features before mutation. Where the
   target must be activated, use the existing activation gate and runup checks.
2. Reserve/register the run, then ask systemd to start its protected service as the agent UID.
   Establish all restrictions before tmux or user-controlled startup code runs.
3. Confirm the service's identity and workload readiness. A successful unit start alone does not
   prove the harness is usable; preserve the current dead-pane diagnosis and secret-safe errors.
4. Attach clients to the contained tmux server. Extra commands that create workload processes use
   that server or the trusted run entrypoint; they cannot be forked in a console server and merely
   labeled with environment variables.
5. On stop, first close admission for new launches and invalidate live authorization for that run.
   Request whole-group TERM, wait a bounded interval, then force termination of all remaining
   members. Service-manager cleanup also runs when the runtime anchor dies.
6. Confirm the group is empty before reporting completion, deleting its live registration, or
   starting a replacement. Preserve enough identity to retry when observation is unavailable.

Use systemd's group termination semantics, including KILL escalation, and verify the actual behavior
against concurrent forks on both supported versions. The kernel's `cgroup.kill` is the whole-tree
primitive; this document does not assume every systemd version uses it on every stop path. If the
chosen service-manager path cannot meet the adversarial test, resolve that before implementation
acceptance rather than adding an uncoordinated second cgroup manager.

Status distinguishes workload readiness from ownership: an empty or dead pane, missing socket, or
missing tmux PID is not proof that the owned group is empty. A run still being terminated remains
incomplete, even when no terminal can be attached. Detached persistence survives CLI/SSH loss. After
VM reboot, verified boot change invalidates the old live binding; suspended VMs retain it. Do not
configure automatic resurrection of a stopped session through systemd restart policies.

## Same-user escape analysis and proof gate

The SSH path is a demonstrated design gap in the current permissions, not a hypothetical stolen
operator key: agent-owned authorization files allow the workload to install its own key. The proof
must cover the file and replaceable parent directories, sshd policy, and alternate authentication
routes while preserving authorized operator access. Likewise, sibling socket access is present in
`cli/agentworks/sessions/tmux.py:300`; other service exposure must be inventoried on the target VM.

A root-owned cgroup removes direct administration from the agent UID. It does not by itself close
these execution channels:

| Channel                                                  | Required treatment before claiming R4                                                                                                     |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Sibling tmux/control sockets                             | The workload cannot submit commands to an outside server; operator attachment remains possible through a trusted path.                    |
| User systemd, cron/at, or another local execution daemon | Deny submission from the workload, or execute submitted work inside the originating run with the same stop boundary.                      |
| SSH into the same VM                                     | Prevent a workload from creating an uncontained login using credentials or agent forwarding it can reach.                                 |
| Process injection and inherited handles                  | Prevent access to outside same-user execution contexts, including alternate proc paths and inherited control descriptors.                 |
| Container APIs and nested runtimes                       | No host daemon may create work outside the run. A nested runtime is supported only if its processes remain within the enclosing boundary. |
| Writable startup/configuration consumed outside the run  | Identify automatic launch paths, including user units and scheduled startup, and prevent them from reconstituting stopped work.           |

Investigate a restricted execution view using Linux namespaces and access controls while preserving
the host agent UID and authorized workspace access. A cgroup namespace alone changes the view, not
all the permissions. Hiding one socket path is insufficient if another mount, abstract socket,
network endpoint, inherited handle, or shared configuration reaches the same service. Namespace
creation and entry must not reopen the boundary. This needs a concrete access map and adversarial
experiments, not a list of hardening settings taken on faith.

The gate must price the compatibility effects on supported harnesses, containers, direct agent SSH,
companion shells, and shared agent homes. If a same-user design cannot close the relevant channels,
return a concrete alternative and its costs for operator decision, such as stronger per-run
isolation. Per-session Linux users, broad egress restrictions, and a general container platform are
not implicitly approved by this draft. Shared-data poisoning of a later independently authorized run
is outside the execution-identity claim; automatic outside execution triggered by a current workload
is not.

## Unix socket identity seam

Provide one trusted lookup that binds a kernel-observed live process to a registered active run. The
verifier uses its own trusted PID/cgroup view and checks identity consistency across lookup; unknown
membership, exited/reused processes, unregistered groups, and stopping runs are refusals. A
workload-supplied path, name, environment, or PID is not proof.

A test socket consumer exercises this seam without introducing a general authorization daemon.
Connection-time peer credentials may describe the process that opened a socket rather than its
current writer after descriptor passing or inheritance. The identity LLD must select a protocol that
validates the actual requester and closes PID-reuse races on Bookworm, including refusal when it
cannot do so. Do not assume newer peer-pidfd socket options exist on that baseline. Authority cannot
survive run invalidation solely because a connection remains open.

## Integration, migration, and review boundaries

[Migration strategy](migration-strategy.md) covers old runs and identity introduction. Agent
sessions use one containment path once converted; admin sessions retain their existing lifecycle
without receiving a new guarantee. Existing commands own the behavior, not a new family of cgroup
commands or optional containment backends. Permanent docs, diagnostics, and any affected
guide/completion surface ship with the code that changes them, not with this draft.

The saga retains ownership of its ledger and shared contracts. This child references those contracts
and requests coordination through its labeled draft; it does not edit the saga's artifacts. The
[plan](plan.md) records the combined FRD/HLA review vehicle and the proof gates before an
implementation plan can be accepted.
