# Transport Improvements: Functional Requirements

- Status: Design baseline for publication; implementation acceptance remains unproven.
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

Subsequent operator direction establishes intentional shell selection, requirements shaped by future
core and plugin workflows, and building a new execution stack alongside the old before cutting over.
This includes a new SSH implementation, not a wrapper around the legacy SSH runner. Working code may
be copied and adapted, but the new stack must not call or depend on the legacy execution stack. The
coordination with the SSH effort is described in the HLA; this document does not edit that effort's
owned artifacts. Following the SSH developer's feedback, the operator requests a small contract
proof before broad parallel implementation, one owner for carrier input, and reusable SSH-backed
platform access with Remote Lima as its first consumer. The operator specifies an OpenSSH minimum of
8.5; the SSH design must make the applicable executable locations explicit before the proof gate
closes.

The operator intends future third-party plugin permissions to determine what `RunContext` exposes.
This design supports separately granted operations and identities now, without implementing the
future permission-policy system or claiming to sandbox in-process plugins.

Operator direction on 2026-09-15 adds robust file-only provisioning and a core-owned filesystem
mutation allowlist. Registration-time resource grant requests and user approval/denial are future
work; they may narrow, but never widen, that core ceiling.

On 2026-09-16 the operator confirms this effort owns building the independent stack with the SSH
effort, migrating every consumer and deleting the legacy stack. The 0.19.0 file helpers are
migration inputs, not a second permanent API. This checkpoint covers up to two authorized artifact
feedback rounds after rebasing onto that release. The operator also removes FIFO creation from the
initial contract: current session objects are tmux sockets, whose directories and controlled cleanup
remain in scope.

The operator now directs publication of the reviewed design baseline before the proof, with the
transport lead owning the carrier contract and its acceptance criteria. The current deliverable is
that design baseline, not completion of the effort's build/migration mandate. It changes no runtime
behavior and leaves proof, detailed design and implementation acceptance open.
[Issue #788](https://github.com/WayfarerLabs/agentworks/issues/788) and the now-closed
[PR #789](https://github.com/WayfarerLabs/agentworks/pull/789) remain historical input.

### Operator ruling, 2026-09-17

On deferring guest cancellation from the buffered PoC, while keeping observation-only deadlines:

> Yeah, I'm confortable deferring cancellation for now.

<!-- cspell:ignore confortable -->

### Operator rulings, 2026-09-18

On lifecycle ownership and the earlier session-cgroup proposal:

> Yeah, to be clear, 770 is going to change radically or even disappear entirely. I'm open to
> either.

On keeping the implementation together:

> Tbh, I'm not sure it really makes sense to try to split the cgroup work. I'm inclined to have you
> do it all here. But I'm open to other ideas as long as you can define the coordination solution.

On shell selection:

> So totally agreed on the shell constants.

On independent execution dimensions and additive protection profiles:

> Yeah, that totally makes sense. And your dimensions are better I think. And yes, the execution
> boundary should just be an escalating set of profiles. You're basically just layering in
> additional protections.

The response to these rulings is in the [lifecycle design](execution-lifecycle-lld.md) and revised
[execution contract](execution-contract.md). They propose unified execution access with separately
granted actions, rather than separate command/job interface objects. File-only access remains
separate. Guest workload protection is distinct from the in-process plugin isolation excluded below.
The #770 requirement mapping and compatibility proofs remain explicit delivery gates.

### Operator rulings, 2026-09-19

On staged delivery and removal:

> In my (possibly naive) way of thinking, this implementation work ends with expanding RunContext to
> have this new surface in parallel with the old. This shouldn't break anything.
>
> Then there's another PR (or set of them) to migrate everything from old to new within RunContext
> (as well as direct calls if they exist). Finally there's a PR to rip out the old.

On permission timing and deliberate consumer choices:

> Yeah, we're merely laying the groundwork for permissions. They shouldn't be enforced or otherwise
> relied upon until the old is removed. The only thing is that the consumers should make concious
> choices about what they should use for each operation.

<!-- cspell:ignore concious -->

These rulings supersede the earlier single-increment cutover and permission-enforcement timing in
R7/R9/R10 and their acceptance scenarios, not the final-state requirements. The response and staged
acceptance gates are in the [migration strategy](migration-strategy.md#sequence-and-cutover-gates).

On transport ownership of the shared cgroup/supervisor implementation and session adoption:

> Yes, transport owns that implementation

On investigation of an early Python prerequisite:

> Investigate an early Python prerequisite (recommended)

On the earlier session-cgroups PR:

> And 770 has been closed with a note

#### File safety and guest runtime rulings

On adding Python to the guest provisioning package list:

> So we're already requiring some apt packages as part of provisioning, right? We probably need to
> put more structure around that, but if you're simply asking to put python3 in that list, all good.
> Just please ensure you don't use anything that wouldn't be supported by Bookworm's python3.

On general authorization:

> Isn't #1 simply: (when security lands), things can't do what they're not authorized to do? Please
> don't tell me you have special requirements/rules/logic around just this one narrow case.

On keeping file safety small and preserving access semantics:

> Ok. I'm glad #1 is general.
>
> And for #2, I agree but keep it smart/small/elegant. And for the atomic writes, please consider it
> a requirement that the file end up as if it were written directly, including impact of
> ACLs/perms/etc. I'm not an expert here but I feel like writing the temp file to the target
> directory (with a conflict-free name) is the right move.
>
> And finally, what situation would an operation have more OS priv than the caller? That sounds like
> a bad idea. Can we just do everything as the target user?

On conservative support and refusal:

> Yeah, honestly, do we really want atomic writes? And we should err on the side of caution across
> the board. Refusing to write strange files (sym or hard links, etc.) is perfectly reasonable,
> especially at first. And I'd prefer that to a bunch of complexity that we'll never use.

On accepting conservative atomic whole-file replacement with defined metadata semantics, and
excluding malicious target-user process containment:

> Perfect. Agreed.
>
> And then my general assumption is that a malicious process running as a given user will be able to
> pwn any other process owned by that user as well as the files that user has access to. Maybe
> cgroups give us something here (can we block process inspection outside the group?) but more is
> going to require proper jails, which we're not doing.
>
> So, no, we shouldn't be protecting against a malicious target user process. That's already game
> over for that user.

<!-- cspell:ignore pwn -->

### Implementation scope

In scope for the eventual implementation:

- The shared execution contract, all existing VM transport adapters, and native transport delivery
  by VM platforms.
- Commands, scripts, finite stdin, output, environment, working directory, identity, privilege,
  files, detached work, and optional terminal or streaming features.
- Delivery of execution targets through `RunContext`, including context construction and consumers.
- Existing helper and caller migration, including setup runners, initialization, backup, recovery,
  VM/agent execution, and session/console attachment.
- A reusable SSH-backed VM platform access pattern, first exercised by Remote Lima placement-host
  provisioning and interrupt cleanup, including work before the VM exists. Other platform adapters
  must be able to reuse this access without depending on Lima. New platform implementations and a
  managed-host product surface are not implied.

Outside this effort:

- Replacing Agentworks orchestration or provisioning with a configuration-management framework.
- New cloud execution carriers, such as replacing public-IP SSH with a provider command service.
- A general scheduler, durable workflow engine, or replacement for tmux-backed sessions.
- A new plugin sandbox or general requester-permission policy system. Permission-scoped interface
  composition and the core file allowlist are in scope; registration-time grant requests, user
  consent, plugin grant configuration and a general policy evaluator are future work.
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
This is an implementation requirement, not a grant of every operation to every context recipient.
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

SSH access to a VM platform is distinct from SSH access to a guest. Platform operations bind the
host connection, execution identity and lifetime; platform-specific tooling and any subsequent guest
hop remain that platform's responsibility. Host work cannot require a VM identity before creation or
borrow authority from a guest target. Remote Lima is the first consumer, not a concept built into
the SSH carrier or a restriction on who can reuse the pattern.

### R2. Commands and scripts

Callers can execute a program with literal arguments or explicitly submit a script. Argument
boundaries, empty arguments, quotes, whitespace, and shell special characters are preserved. Literal
arguments are never implicitly interpreted as a script.

Script execution deliberately selects a fixed interpreter, including `sh` or `bash`, or the
execution user's configured default shell. Selection is explicit on the call or inherited from a
default deliberately bound by the owning operation. An unspecified script interpreter is an error,
not permission for the carrier to choose one. Source is interpreted in the selected shell's
language; the execution API does not translate shell languages.

Interpreter selection, login startup, and interactive shell behavior are separate choices. Fixed and
user-shell requests default to non-login, non-interactive startup unless explicitly requested
otherwise. A terminal allocation alone does not select an interpreter or change startup policy.
User-shell selection refers to the actual execution identity on the destination, including root
after elevation, never the workstation shell or an inherited `$SHELL`. Missing or incompatible
interpreters fail explicitly without substitution.

Literal program execution does not apply an application shell or shell startup implicitly. A caller
needing profile-initialized behavior requests a shell invocation intentionally. Any shell used
internally by the carrier must preserve argument boundaries and cannot redefine this contract.

Scripts support compound commands, pipelines, redirects, and multiple lines according to their
selected language. Script delivery owns temporary artifacts and cleanup. A script and its stdin
payload remain separate inputs; reading stdin inside a script must not consume its source. The same
shell policy applies to foreground and detached execution. Provisioning and plugin scripts must work
without provider-specific staging at their call sites.

### R3. Identity, privilege, environment, and directory

Ordinary execution uses the target's bound guest identity. Explicit elevation applies to the whole
operation, including scripts, redirects, file placement, and detached children. An agent target
cannot gain administrative authority by selecting native access or another identity through the
execution API. Elevation unavailable to that identity is a permission failure.

Environment and working directory have identical meaning across carriers and apply inside the
selected execution identity. Environment covers the entire script, not only its first command. The
execution layer preserves the environment policy composed by the owning operation, including
protected Agentworks identity values. It does not independently resolve config or secrets.

Shell selection follows R2, independently of environment composition and carrier choice. Default
authority must not depend on whether a provider happens to execute its guest agent as root.

### R4. Input, output, and results

Finite ordinary and sensitive stdin are supported on every target. Absent input means immediate EOF.
Bytes survive delivery without workstation newline conversion; text has a documented encoding.
Buffered execution returns distinct stdout and stderr plus the remote completion status. Direct
streaming and terminal operations retain their explicitly documented stream semantics.

Sensitive input, environment, or script content must not appear in command arguments, logs,
exceptions, or persisted job references. Output that could reflect sensitive values is suppressed by
default in results, logs, and guest artifacts. Required diagnostics use non-sensitive calls.

The owning operation may explicitly select live output for an authorized interactive or streaming
flow, preserving ordinary exec and session attachment even when their environment contains secrets.
That raw presentation can reflect secrets printed by the workload; suppression is not promised for
the selected stream. It does not enable transport logging or retained output artifacts. Merely
binding a secret-bearing environment must not silently blank an existing interactive operation.

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
directory movement required by current workflows. File-only resources can also read/stat files,
create directories, inspect bounded directory inventories, merge JSON configuration, manage
permitted ownership/modes and conditionally replace/remove permitted objects without command or job
access. These are required file semantics on supported VM targets, not SSH-only conveniences.
Regular-file operations reject special objects rather than block opening a pipe. Session owners can
remove an exact stale socket only after establishing runtime absence; the file API does not perform
that liveness check, create sockets or provide FIFO creation.

Callers select ordinary or elevated placement without hand-writing copy, chmod, or sudo wrappers.
Elevation covers staging, publication and metadata under the bound file grant, not general admin
execution authority. Transfer may be slower on a native API channel, but native recovery must be
able to deliver scripts and retrieve diagnostic files.

Atomic file replacement means staging and replacement on the destination filesystem, with the
requested access mode applied before publication. It does not promise an atomic directory-tree
replacement. Partial-transfer cleanup, ownership, symlink handling, and overwrite behavior must be
specified before implementation. Cleanup is restricted to artifacts the operation owns.

Structured updates accept data, not caller-supplied remote scripts or callbacks. They preserve
unrelated JSON values and define nested objects, arrays, deletion, missing files and invalid input
explicitly. Preserve shipped harness merge policies and literal JSON null values; null must not
silently become deletion. Merge operations reject malformed existing configuration; explicit replace
and skip-existing retain their distinct semantics. Formatting preservation is not implied. Read
snapshots and conditional writes/removal let domain code preserve generated-section surroundings
without remote callbacks. Update results distinguish changed, unchanged, conflict, failure and
uncertain publication without returning existing contents to a caller lacking read access. Internal
read/modify/write is permitted by the merge grant; it does not confer public download authority.

Read/modify/write operations must not silently lose updates from cooperating writers. Define the
serialization and conflict protocol, and its limits with external non-cooperating writers, before
implementation. A content check followed by rename is not atomic compare-and-swap. Metadata
preservation/change rules include existing ownership/mode and relevant ACLs or security attributes;
unsupported preservation must be explicit, not silent loss. Secret-bearing content follows R4 and
must not leak through diffs, errors or mutation results. Multi-file operations do not imply a
transaction; report partial progress and uncertain outcomes without blind replay.

#### Core-owned mutation allowlist

Every public file mutation is limited by a core-maintained allowlist and the recipient's bound
grant. Entries identify exact files or whole subtrees, allowed operations and ownership/mode limits,
with explicit target identity/scope. Unlisted destinations are denied by default. Resource/plugin
registration, call arguments, configuration and elevation cannot add entries or widen them. A new
location or operation outside that ceiling requires a reviewed core change, not a plugin override.
Core composition may resolve approved per-agent/session roots from trusted identity data; a caller
cannot supply an arbitrary root under the guise of an approved template.

For example, a subtree entry for `/etc/claude-code` can allow configuration updates beneath it
without granting writes to siblings, mutation of `/etc` itself, or removal/replacement of the
approved root. Creating that root is a separate explicit permission under its trusted parent.
Ownership/mode changes and recursive deletion are not implied by content-write authority. Read
access remains independently granted; a mutation allowlist is not a read grant.

Enforcement covers all mutation paths, including both ends of a move, extraction, directory
replacement, metadata changes and cleanup. Validate known grant/path denials before preparation;
enforce filesystem-dependent confinement at the point of use, including elevated helpers. Reject
traversal, symlink/hard-link aliasing and race-based escapes rather than relying on string prefixes
or a preflight path check. The implementation must define its trusted-parent and mount assumptions
and fail closed when it cannot establish the boundary. Guest root remains outside this containment
claim. Internal staging/locks use narrowly core-authorized locations and owned names, never a
caller-selectable bypass or an implicit write grant on the destination's parent.

Review each allowed location for how its files are consumed: an approved configuration may contain
command hooks, and an executable or service definition conveys execution-related authority. The
allowlist does not certify arbitrary content as non-executable or confine separately granted exec.
Use a narrower structured operation or withhold a grant where arbitrary bytes are inappropriate;
this effort does not add a generic application-schema policy engine. Core provisioning and recovery
must have their required file destinations inventoried and approved, not bypass denial by routing a
file-only request through public commands.

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

`RunContext` delivers permission-scoped views of the common execution target contract for admin and
agent identities. A recipient receives only the operation interfaces and authority provided by the
owning composition root. Command execution, file access, and job operations are separable;
possession of one does not automatically expose the others. Finer grants must be possible within a
family, including upload versus download and job observation versus cancellation. Target-user
execution, VM-admin-user execution, and elevation to root are distinct grants.

Optional channel features describe implementation support, not permission. An unavailable lifecycle
target, a withheld interface/action, a missing optional channel feature, and a guest permission
failure are distinct conditions. Authorization denial must not be reported as an unsupported
transport operation. Authorized core provisioning and recovery still receive everything they need;
restricting plugin views must not weaken required native functionality.

File views bind permitted paths, actions and metadata/elevation limits within R7's core ceiling. An
admin identity alone does not confer unrestricted file mutation. Future registration-time requests
and user approval may select narrower grants without replacing the enforcement boundary; this effort
implements explicit core composition, not that future consent workflow.

Fine-grained recipient grants are an intentional contract investment ahead of that workflow, not a
claim that 0.19.0 already has a general plugin authorization system. Keep the implementation to
small bound values and checks. Core composition can give a settings publisher only its reviewed
locations/actions while retaining broader recovery authority; it needs no registration service or
generic policy language to express those different views.

Views are bound before delivery and cannot widen their authority through `sudo=True`, another
identity, an environment-derived view, a saved job reference, or public access to an unrestricted
target/carrier. Denials knowable from bound grants happen before staging, dispatch or other effects;
filesystem-dependent confinement is enforced at use time before the protected mutation. Accessors
are passive; they expose the bound decision rather than evaluating plugin policy or discovering
authority.

Context construction and accessors do not connect, start VMs, open routes, resolve secrets, switch
identities, or select a fallback. The orchestrator delivers the selected route and scoped secrets at
the established lifecycle boundary. Descriptive `OperationScope` names never become authority to
locate or manufacture an execution target.

Preflight and runup retain their read-only contracts and existing timing. Targets supplied there
must not implicitly install helpers, stage scripts, or launch jobs. Their controlled preparation
must not request shell startup files or depend on startup side effects. Carrier/account hooks that
run before the payload are documented connection prerequisites, not initialization supplied by this
API or a guarantee that arbitrary account hooks are read-only. Execution targets and their
operation-scoped resources cannot remain usable after the owning lifetime closes. A later job
observation receives a fresh authorized context; a saved job reference carries no credentials.

These are API authority boundaries, not claims of hostile-code isolation. Arbitrary execution as a
user conveys that account's available guest authority, including filesystem access and configured
sudo privileges. Withholding upload does not confine an allowed shell; withholding API elevation
does not prevent an allowed command, script or job from invoking sudo itself. Elevation grants
control API-performed elevation, not removal of guest privileges. Actual root denial requires a
restricted guest identity or another enforcement boundary, not command-text filtering here.
In-process Python plugins can access process resources outside this API. Strong isolation requires a
separate enforcement boundary and is not provided by this transport effort.

### R10. Complete adoption and evidence

Build the new execution stack alongside the operational old stack, validate it independently, then
cut production callers over to the settled contract. Temporary coexistence is an implementation
strategy, not two supported public execution APIs or an operator-selectable transport version. The
new path must not execute a mutation through both stacks for comparison.

Before broad parallel implementation, demonstrate the small shared contract end to end, including
shell bootstrap, input/source separation, guest output separation and no-staging readiness. The
[plan](plan.md) defines the proof gate and the following design reconciliation, independent build,
workflow validation and complete cutover. Unresolved seam behavior must not be independently
invented by the two efforts. The proof is not full-platform acceptance or authority to begin it in
this documentation-only revision.

The new stack includes SSH connection and delivery machinery. It is independently usable without
legacy execution packages installed: no imports, inheritance, forwarding calls, or indirect runtime
dependencies on the implementations being retired. Copying working code is permitted when it is
adapted to the new contract and tested independently. This is not a ban on using ordinary shared
project utilities that remain supported and do not depend on the retired stack.

Preserve operator-owned SSH configuration and trust evidence through an explicit state transition;
replacing implementation code must not reset trust, silently widen authentication, or require old
execution code to interpret retained state.

At cutover all existing adapters and core consumers adopt the same target contract. The effort
retires duplicate command rendering, transport-shaped setup wrappers, and detached helpers. It does
not leave a permanent old/new API bridge. Migration still audits callers' intended shell, identity,
environment, and lifetime; current implementation quirks do not define the new contract.

Verification covers shared behavior, required workflows over a target with no optional features, and
optional feature support/refusal. Live evidence covers the supported platforms and relevant
workstation operating systems, including supported Proxmox majors and WSL2 lifetime behavior.
Unavailable live coverage is recorded for operator disposition, never counted as passing.

### R11. Core and plugin developer workflows

Requirements must serve credible future core and plugin workflows as well as existing callers.
Current usage informs migration and demonstrates problems; absence of a current caller does not by
itself justify removing an operation developers need. Each facility needs a concrete developer
scenario and observable guarantees, without requiring a production caller before it can be designed.

Examples include a plugin running a Bash installer with separate sensitive stdin, a developer
choosing a user's login shell for configured tools, binary file movement across a bounded native
channel, and a long-running operation whose output and result are observed from a later authorized
invocation. The common target must make these workflows straightforward. Extensible command ASTs,
generic scheduling, or a second orchestration system are not implied by supporting them.

## Acceptance scenarios

Shell acceptance includes fixed `sh` and `bash`, user-default selection, login versus non-login
startup, and elevated user-shell resolution. Changing carriers must not change those choices;
literal arguments and script stdin remain intact. Readiness probes must not request user startup
files or depend on their side effects to prepare an invocation. Acceptance distinguishes this
controlled preparation from carrier/account bootstrap behavior described in R9.

Cutover acceptance exercises complete workflows through the new stack before switching production
entry points, then proves those entry points and plugin contexts use it exclusively. Future-facing
scenarios in R11 are acceptance cases even where the current implementation has no caller.

Context acceptance includes command-only, file-only, upload-without-download, job observation
without cancellation, and admin views without API elevation. Missing grants fail before effects;
derived views and saved job references do not restore withheld authority. The same carrier supports
a restricted plugin view and a fully authorized recovery view without changing its feature
description.

File-only acceptance provisions whole files and merges harness JSON under an approved `/etc` subtree
and installs content under `/opt/agentworks/artifacts` with commands/jobs withheld. A session slice
manages the approved tmux socket directories and removes a confirmed stale socket without granting
socket creation or treating it as a regular file. Prove ordinary and elevated ownership/modes,
unrelated-key preservation, malformed-input refusal, cooperating-writer conflict handling and
secret-safe results over SSH and native delivery. Prove refusals leave protected objects unchanged
for parent/sibling/prefix collisions, traversal, links, concurrent path substitution, unauthorized
metadata/deletion and archive escapes. Exercise root creation separately from root replacement,
cleanup after partial transfer and uncertain publication. Do not turn confinement gaps into an
unsupported native feature or silently fall back to caller-visible exec.

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
