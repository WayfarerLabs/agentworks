# Transport Improvements: Functional Requirements

- Status: First draft for operator review; implementation is not authorized by this document.
- Started: 2026-09-12
- Effort: `transport-improv`
- Companion: [High-level architecture](hla.md)

## Purpose

Make executing work on a managed VM predictable for Agentworks developers and operators, regardless
of how the VM is reached. A developer should be able to run a command or script, select ordinary or
elevated execution, transfer files, and start detached work without assembling transport-specific
shell wrappers or knowing provider mechanics.

Every supported VM platform must provide the operations needed for Agentworks provisioning, normal
operation, and native recovery. Optional conveniences may differ by channel. In particular, a native
interactive shell is useful where available but cannot be a prerequisite for recovery.

## Direction and scope

The operator requested a new SDD with a first-draft FRD and HLA, extending the design through
`RunContext`. The operator explicitly asked that the ideal API be designed independently of ongoing
SSH work, with reconciliation afterward. This effort has no designated parent saga.

The requirements below are proposed details of that direction, pending review. The current
deliverable is a draft PR for design discussion. It changes no runtime behavior and neither merges
nor closes [issue #788](https://github.com/WayfarerLabs/agentworks/issues/788) or
[PR #789](https://github.com/WayfarerLabs/agentworks/pull/789).

In scope for the eventual implementation:

- The shared execution contract, all existing VM transport adapters, and native transport delivery
  by VM platforms.
- Commands, scripts, finite stdin, output, environment, working directory, identity, privilege,
  files, detached work, and optional terminal or streaming features.
- Delivery of execution targets through `RunContext`, including context construction and consumers.
- Existing helper and caller migration, including setup runners, initialization, backup, recovery,
  VM/agent execution, and session/console attachment.

Outside this effort:

- Replacing Agentworks orchestration or provisioning with a configuration-management framework.
- New cloud execution carriers, such as replacing public-IP SSH with a provider command service.
- A general scheduler, durable workflow engine, or replacement for tmux-backed sessions.
- A new plugin sandbox or general requester-permission system.
- Requiring native interaction on a platform that cannot supply it.

## Users and outcomes

| User                                  | Desired outcome                                                                                                 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Core developer                        | Use the same execution vocabulary across initialization, recovery, and ordinary operations.                     |
| Capability author                     | Receive an already-bound target through the operation context, without resolving infrastructure or credentials. |
| Operator recovering a VM              | Execute repairs through the explicit native route when canonical connectivity is broken.                        |
| Operator using an interactive channel | Retain streaming commands, shells, and session attachment where the selected channel supports them.             |
| Platform author                       | Implement a small delivery contract and prove required behavior through shared conformance checks.              |

## Requirements

### R1. Required operations and optional features

Every native VM target and every canonical VM target must support the common operations in R2-R8.
Provider limitations must be handled inside the implementation through a supported mechanism; they
cannot become an "unsupported" escape for an operation Agentworks needs to function or recover.

Interactive terminals and direct live stdio streaming are optional channel features. Their
availability must be discoverable without executing guest work. An explicit request for an absent
feature fails with a typed, actionable refusal, before credentials or mutation where its absence is
already knowable. No argument may be accepted and silently ignored.

Availability describes what the channel implements, not current connectivity, authentication,
permission, or guest health. A temporarily broken required operation is an operational failure, not
an unsupported feature. Canonical channels must retain the interaction needed by Agentworks sessions
and consoles. Required core workflows cannot depend on native optional features.

### R2. Commands and scripts

Callers can execute a program with literal arguments or explicitly submit a script. Argument
boundaries, empty arguments, quotes, whitespace, and shell special characters are preserved. Literal
arguments are never implicitly interpreted as a script.

Scripts support compound commands, pipelines, redirects, and multiple lines with one documented
shell and startup policy. Script delivery owns temporary artifacts and cleanup. A script and its
stdin payload remain separate inputs; reading stdin inside a script must not consume its source.
Existing provisioning scripts must work without provider-specific staging at their call sites.

### R3. Identity, privilege, environment, and directory

Ordinary execution uses the target's bound guest identity. Explicit elevation applies to the whole
operation, including scripts, redirects, file placement, and detached children. An agent target
cannot gain administrative authority by selecting native access or another identity through the
execution API. Elevation unavailable to that identity is a permission failure.

Environment and working directory have identical meaning across carriers and apply inside the
selected execution identity. Environment covers the entire script, not only its first command. The
execution layer preserves the environment policy composed by the owning operation, including
protected Agentworks identity values. It does not independently resolve config or secrets.

Programmatic execution has deterministic shell-startup behavior. Interactive login behavior remains
explicitly separate. Default authority must not depend on whether a provider happens to execute its
guest agent as root.

### R4. Input, output, and results

Finite ordinary and sensitive stdin are supported on every target. Absent input means immediate EOF.
Bytes survive delivery without workstation newline conversion; text has a documented encoding.
Buffered execution returns distinct stdout and stderr plus the remote completion status. Direct
streaming and terminal operations retain their explicitly documented stream semantics.

Sensitive input, environment, or script content must not appear in command arguments, logs,
exceptions, or persisted job references. Output that could reflect sensitive values is suppressed by
default in results, logs, and guest artifacts. Required diagnostics use non-sensitive calls.

Provider payload and output limits cannot silently truncate results or impose an unusable ceiling on
core scripts and files. The implementation uses bounded transfer or spooling where needed, reports
incomplete output honestly, and does not require unbounded workstation memory. Transport metadata
and errors use mechanism-neutral names.

### R5. Foreground execution and failures

Foreground calls wait for completion and report remote command failure separately from failure to
deliver or observe the command. Callers can choose whether a nonzero guest exit returns a result or
raises a checked-command error. Errors retain safe target identity and execution phase.

A deadline bounds the requested local wait. Expiry or loss of contact does not imply guest
termination. Results distinguish known completion from uncertain dispatch or still-unknown state.
The CLI maps known guest signals and exit statuses consistently without inventing a successful exit
for missing evidence.

An ambiguous operational command is never automatically dispatched again. Repetition requires
caller-owned evidence of safety, such as an explicitly idempotent probe. Connection establishment
may retry only while the implementation knows execution has not begun.

### R6. Detached work

Callers can start a command or script detached and obtain a job reference after acknowledged launch.
Detachment disconnects stdin and output from the initiating channel so connection teardown does not
terminate the work or block the launch response. Completion is distinct from launch acknowledgement.

The reference supports observation, incremental output retrieval, waiting, cancellation requests,
and explicit release of retained artifacts. A subsequent authorized operation can resume observation
without resubmitting the command. Repeated independent launches do not reuse an earlier result just
because their commands or filesystem paths match.

Cancellation targets the owned workload, including its ordinary descendants, under its original
identity. It reports confirmed termination, a failure, or uncertainty; it never claims cancellation
from a failed status read. PID reuse and stale records cannot select unrelated work. This is an
operational cancellation contract, not containment of a malicious process that escapes its group.

Disconnect survival assumes the VM remains running. Detached work does not promise survival of
reboot, VM stop, or host shutdown, and cannot override an operator's explicit stop. Platform holds
must cover an active operation; releasing a client context must not masquerade as stopping its job.

### R7. Files

Every target supports finite byte-exact file upload and download, file-content writes, and the
directory movement required by current workflows. Callers select ordinary or elevated placement
without hand-writing copy, chmod, or sudo wrappers. Transfer may be slower on a native API channel,
but native recovery must be able to deliver scripts and retrieve diagnostic files.

Atomic file replacement means staging and replacement on the destination filesystem, with the
requested access mode applied before publication. It does not promise an atomic directory-tree
replacement. Partial-transfer cleanup, ownership, symlink handling, and overwrite behavior must be
specified before implementation. Cleanup is restricted to artifacts the operation owns.

### R8. Bootstrap and native recovery

Native execution remains explicitly selected. Ordinary access never silently falls back to native
access, and native recovery does not probe, repair, or require canonical connectivity first. Power
convergence respects operator-stopped intent and retains the platform's active hold.

Required native operations include release attestation and provisioning, Tailscale installation,
join/rejoin/rekey, logout, diagnostic command/script execution, and file delivery/retrieval. They
must work on Proxmox without an interactive shell. Required execution and staging cannot depend on
Phase B initialization or a helper whose installation itself needs canonical connectivity.

Recovery defaults to the minimal operation environment so unrelated workspace or application secrets
cannot block it. Explicit environment and directory requests remain technically available; requested
workspace semantics require their normal validation and secret-resolution policy. The choice of
native carrier alone is not grounds for rejecting a workspace.

Native means independent of Tailscale, not independent of every guest service. Existing cloud native
channels still rely on guest SSH. This effort must describe that dependency honestly.

### R9. RunContext delivery

`RunContext` delivers the common execution target contract for admin and agent identities. Optional
features are inspected on a delivered target without narrowing to a concrete transport. A target
absent at a lifecycle stage is distinct from a present target missing an optional feature.

Context construction and accessors do not connect, start VMs, open routes, resolve secrets, switch
identities, or select a fallback. The orchestrator delivers the selected route and scoped secrets at
the established lifecycle boundary. Descriptive `OperationScope` names never become authority to
locate or manufacture an execution target.

Preflight and runup retain their read-only contracts and existing timing. Targets supplied there
must not implicitly install helpers, stage scripts, or launch jobs. Execution targets and their
operation-scoped resources cannot remain usable after the owning lifetime closes. A later job
observation receives a fresh authorized context; a saved job reference carries no credentials.

### R10. Complete adoption and evidence

All existing adapters and core consumers adopt the same target contract. The effort retires
duplicate command rendering, transport-shaped setup wrappers, and detached helpers once their
consumers move. It does not leave a permanent old/new API bridge.

Verification covers shared behavior, required workflows over a target with no optional features, and
optional feature support/refusal. Live evidence covers the supported platforms and relevant
workstation operating systems, including supported Proxmox majors and WSL2 lifetime behavior.
Unavailable live coverage is recorded for operator disposition, never counted as passing.

Permanent capability docs, CLI help/reference, provider guidance, and other affected collateral
change with the implementation that makes their claims true.

## Acceptance scenarios

| Scenario                      | Observable success                                                                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Literal arguments and scripts | The guest receives exact arguments; compound scripts observe the requested environment, directory, and identity throughout.                               |
| Sensitive script plus stdin   | The guest consumes each independently; canary values are absent from diagnostics and retained artifacts after cleanup.                                    |
| Proxmox recovery              | With Tailscale unavailable, admin and elevated commands, a staged script, files, and a detached repair work; native shell requests are refused.           |
| Provider limits               | A script and diagnostic file larger than one provider request move intact without caller-specific chunking.                                               |
| Detached reconnect            | Loss of the initiating channel leaves one job; a new context observes its result without another launch.                                                  |
| Timeout and cancellation      | A wait timeout reports uncertainty; cancellation confirms the owned workload stopped or reports why it cannot establish that.                             |
| Context lifecycle             | Missing targets remain absent, accessors perform no I/O, readiness stays read-only, and closed targets cannot execute.                                    |
| Isolation                     | An agent-bound target cannot select admin/native authority; elevated admin work uses the same intended environment and file permissions on every carrier. |
| Optional interaction          | Canonical session attachment still works; supported native shells work; unsupported requests fail before avoidable side effects.                          |

## Review questions

The draft proposes mandatory file movement and managed detached work on native targets, beyond the
current minimal native contract. Review should confirm this breadth and the environment/workspace
policy in R8. Exact Python signatures, resource limits, and job storage mechanics belong in the next
design pass; they must satisfy these outcomes without adding a general workflow engine.
