# Transport Improvements: High-Level Architecture

- Status: Revised draft for operator review
- Requirements: [FRD](frd.md)
- Supporting work: [Prior art](prior-art-research.md), [migration](migration-strategy.md),
  [plan](plan.md)

## Architectural decision

Expose one bound guest execution target to callers and `RunContext`. Put common command, script,
file, and job semantics above delivery adapters. Describe optional interaction as a feature of the
selected channel. Required operations are implemented by every target, even when a carrier needs
shared staging or polling to provide them.

This replaces the current execution-only/full inheritance split as the caller-facing contract. It
does not erase physical differences between SSH, local VM tools, and a guest-agent API. There is no
backend selection inside a command, and no generic capability registry to negotiate a route.

Build this as a new stack alongside the current one, with separate internal entry points for
development and validation. Settle and prove the contract before cutting over existing callers.
Current callers inform the migration; the intended core/plugin workflows in FRD R11 determine what
the new interface must express. New job observation or script facilities need not wait for an old
caller to demonstrate a use that the old interface could not support.

## Components and ownership

| Component                  | Owns                                                                                                                    | Does not own                                                           |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Operation composition root | VM selection, canonical/native route policy, guest identity, environment policy, secrets, activation and route lifetime | Shell quoting or provider payload encoding                             |
| `RunContext`               | Delivery of already-bound targets, descriptive scope, scoped secret reader                                              | Connections, route selection, target construction, or resource cleanup |
| Execution target           | Consistent command/script, file, and job operations; optional-feature description; bound identity                       | VM lookup, secret resolution, or implicit elevation/fallback           |
| Shared execution helpers   | Shell preparation, input delivery, staging, safe diagnostics, file publication, job records and observation             | Infrastructure orchestration or application-specific retries           |
| Delivery adapter           | Dispatch and observation over SSH, Lima, remote Lima, WSL2, or Proxmox QGA; provider limits and optional I/O            | Workspace policy or business operations                                |
| Owning resource operation  | Retention and lifecycle of a job beyond the initiating call                                                             | Reusing a transport after its route or VM hold closes                  |

These are responsibilities, not a requirement for six class hierarchies. The target composes a
carrier and small shared helpers. Concrete adapters stay with their current package owners;
Proxmox-specific API handling stays in the Proxmox plugin.

The same execution and job mechanics serve the existing remote Lima placement-host target. Its
identity is the SSH host and bound host user, not a VM that has yet to be created. The Lima platform
owns this target inside provisioning and rollback; it is not delivered through guest admin/agent
accessors in `RunContext`. This preserves an existing use of SSH without adding arbitrary host
discovery or a second detached implementation. Its shared job/file helpers must preserve supported
host userspace, including macOS, without assuming Debian paths, GNU-only options, or guest identity
files. The LLD names portable helper prerequisites and explicit host shell/PATH preparation for
`limactl`.

## Public target contract

The proposed vocabulary is `run`, `script`, `start`, file operations, and `interactive`. Names are
provisional; the behavioral division is the design:

- `run` takes a program and literal argument sequence and waits for completion.
- `script` takes explicit script content and waits for completion. It shares execution options with
  `run`, including an independent stdin payload.
- `start` accepts either command form and returns a managed job reference after launch
  acknowledgement. Waiting is a subsequent operation.
- File operations upload, download, write content, and compose directory movement.
- `interactive` attaches a terminal only when that optional feature is present.

Foreground execution captures output by default. An explicit direct-streaming mode is available on
supporting channels; ordinary CLI execution preserves its current streaming experience there. On
buffered-only native channels the CLI identifies buffered behavior. It cannot promise an interactive
stdin stream or real-time output over QGA. Required progress can use job output polling.

Execution options have one meaning across commands and scripts: elevation, environment, working
directory, finite input, output handling, check behavior, and local deadline. A small request value
may carry these through adapters; an extensible command AST or middleware pipeline is unnecessary.
Ordinary literal commands must remain concise to call without constructing a graph of objects.

### Shell policy

Keep invocation form separate from interpreter policy. `run` executes literal argv; `script` names
source and a shell policy; `start` accepts either form without changing its semantics. Shell policy
selects a fixed interpreter (`sh`, `bash`, or a supported explicit executable) or the destination
execution user's configured default shell, with separate login and interactive startup choices.
Names and exact Python signatures belong in the execution LLD.

Every script call supplies that policy or uses an explicit default bound by its owning operation.
There is no transport-selected interpreter. A shell choice without startup modifiers means
non-login, non-interactive startup. Shared preparation isolates unsolicited startup hooks where the
supported interpreter permits it; the LLD must enumerate supported interpreters and their startup
behavior rather than promise identical flags for every shell. A missing interpreter or unsupported
startup combination fails before payload execution; it does not fall back to another shell.

Resolve a user-shell request from the destination account after choosing execution identity. For
elevated execution it selects root's configured shell; requesting Bash explicitly still selects Bash
under root. Resolve once per invocation and retain that selection for detached work. Changing the
account's shell later cannot change the interpreter of an already-submitted job. Source language
remains the caller's responsibility when requesting the user's shell.

Literal execution uses the target's explicit environment and working directory without profile
initialization. A profile-dependent operation deliberately invokes a shell with the required startup
policy. Carrier bootstrap may itself involve an account shell, notably OpenSSH's remote command
handling, but that shell is delivery machinery: adapters must preserve literal argv and the chosen
payload interpreter. Isolation does not claim the carrier never starts a shell. If a supported
carrier cannot honor the payload contract through its bootstrap shell, that is an implementation gap
to resolve, not a different interpretation of the command.

Application shell policy belongs above the adapters. Provider wrappers and shared transfer/job
helpers choose their own explicit internal interpreter and must not inherit the application's
user-shell choice. Readiness uses bounded probes with fixed preparation and no profile evaluation;
context accessors never resolve user shells or run initialization files.

### Optional channel features

Use one immutable description for the selected channel, with a closed set of optional features:
interactive terminal and direct live stdio streaming. The adapter/factory owns this description;
platform preflight and the opened target reference the same definition. They do not maintain two
declarations plus a checker to keep them synchronized. A platform may provide canonical SSH with
interaction and native QGA without it, so the description is channel-specific, not a platform-wide
boolean. Reading it does not open the route.

Conformance verifies that advertised features work and required operations remain present. An absent
optional operation raises a typed refusal, allowing early refusal when the selected channel is
already known. Required methods have no unsupported default. The description is not a health probe
or permission grant. Avoid calling these flags "capabilities" where that would confuse them with
Agentworks' resource capability model.

| Operation                                          | Canonical VM target                    | Native VM target                |
| -------------------------------------------------- | -------------------------------------- | ------------------------------- |
| Buffered commands/scripts, finite stdin, env, cwd  | Required                               | Required                        |
| Bound identity and explicit permitted elevation    | Required                               | Required                        |
| File and directory movement                        | Required                               | Required                        |
| Managed detached start and observation             | Required                               | Required                        |
| Managed cancellation request with truthful outcome | Required                               | Required                        |
| Interactive terminal                               | Required for current sessions/consoles | Optional; absent on Proxmox QGA |
| Direct live stdio streaming                        | Retained by current SSH channel        | Optional                        |

Adapters may optimize file movement and job observation. An optimization cannot change the target
contract or make a required operation depend on an optional method. Provider size limits are
delivery facts, not public feature switches.

## Identity and preparation

A target binds one guest identity and one already-selected route for its lifetime. Admin targets may
request non-interactive elevation; agent targets remain constrained by their actual guest
permissions. No public per-call username or native-route selector is added to a delivered target.
Provider-root execution, as with QGA, is demoted to the bound admin identity for ordinary calls.

The shared preparation path applies privilege, environment, directory, and command grouping once.
Values must reach the final execution identity, including the whole body of an elevated script.
Adapters handle only the unavoidable carrier-specific identity switch and payload delivery.

Environment composition stays with resource operations. A bound target can carry prepared defaults
and protected keys, replacing `SetupRunner`'s copied transport interface and custom sudo wrapping.
Per-call ordinary overrides obey that policy; callers cannot rewrite protected Agentworks identity.
Derived environment views retain the same identity, route, optional features, and lifetime.

FRD R4 owns sensitive-output and live-presentation policy. Architecturally, preparation carries an
explicit input/output policy to each carrier and logger, keeps script source separate from program
stdin, and uses private staging when required. Cleanup debt after a disconnect stays with the owning
operation. Protection from incidental disclosure is not a sandbox against guest root or malicious
in-process plugins.

## Delivery and bootstrap

The internal carrier interface describes dispatch of a prepared invocation with finite input and
observation of completion. A synchronous CLI carrier and QGA's dispatch/poll carrier satisfy the
same observable contract. Their internal handles need not share a wire protocol.

Shared fallback transfer uses bounded, encoded chunks and guest-side staging when direct transfer is
unavailable. Reads bound each response below the provider output limit. Writes use private
operation-owned staging and verify complete delivery before execution or publication. Chunk retries
must address a known offset or other idempotent write, never blindly append after ambiguity. Large
captured output can be spooled and retrieved through the same bounded file mechanism.

The common guest substrate is the project's supported Debian userspace, not an installed Agentworks
daemon. Bootstrap-critical operations must work from the base image and native access already
available at provisioning. The LLD must enumerate the minimal shell/tools used by transfer and job
helpers and demonstrate that delivering those helpers does not depend on themselves. No Phase B
package install or Tailscale access can be a hidden prerequisite.

Read-only readiness probes use a direct invocation or bounded reads. They cannot trigger the
staging/spooling/job fallback that writes guest files. Readiness call sites choose bounded probes;
the LLD must provide a way to prevent implicit preparation writes in a readiness target. This is an
execution-helper constraint, not a claim that arbitrary shell code can be proven read-only.

File writes stage beside the destination and publish by rename after setting permissions and
ownership. File downloads publish locally only after complete receipt. Directory helpers retain
explicit merge/replace semantics with confined extraction and cleanup; they do not inherit an
unqualified recursive-delete default. Atomicity and crash durability are distinct promises.

## Detached jobs and lifetimes

Managed jobs are a shared execution facility, replacing `remote_exec.py` and hand-built `nohup` call
sites. The initial implementation direction is a small staged wrapper on the execution host using
existing userspace detachment and process-group mechanisms, with identity-private records for
launch, stdout/stderr, and completion. It is not a service scheduler or replacement for sessions.

Each launch has a fresh identifier. An explicit existing reference means observe that launch;
identical command text or a reused pathname does not. The wrapper publishes launch/completion facts
atomically. A reference binds the actual execution host, execution identity, boot incarnation, and
job, without secrets or an embedded connection. Managed guest targets additionally bind VM instance
identity; placement-host jobs do not invent a VM identity. PID alone is insufficient proof of
ownership. The owning resource operation stores references needed across invocations; the exact
storage belongs in its LLD, not a new general job registry.

Starting, waiting, observing output, requesting cancellation, and disposing artifacts are separate
operations. Waiting never deletes the evidence needed for another authorized observer. Cancellation
checks job ownership and targets the job's process group with appropriate privilege, then observes
termination. A missing response remains uncertainty. The LLD must handle a wrapper exiting early,
ordinary descendants surviving it, stale records, PID reuse, and concurrent observers.

The composition root holds platform activation and transient routes around each active operation. A
returned job reference may outlive that span; its live target cannot. Subsequent observation binds
the reference to a fresh target through a new operation context. Native route rules can close after
launch because detachment severs the job's stdio dependency on that connection.

Platform power lifetime is separate. In particular, WSL2's existing hold depends on a workstation
process. A job reference cannot promise to keep a distro alive after the owning CLI exits. The
initial contract guarantees disconnect survival while the VM remains running; operations requiring
completion keep the platform hold while waiting. Native background launch on WSL2 and the effect of
idle shutdown need a live feasibility check before implementation design is accepted. Explicit
operator stop always wins; no new power-management daemon is implied.

Retention belongs to the owning operation: initialization can resume an identified job, backup can
collect and dispose its archive job, and logout can release client observation without pretending to
have confirmed remote completion. The job design must include cleanup for abandoned internal
fire-and-forget work so a new public handle does not turn into indefinite guest debris.

## RunContext integration

Keep the existing accessor distinction between descriptive context and execution-bearing targets.
`admin_target()` and `agent_target()` return the common bound execution target, or no target when
that identity does not exist or was not supplied at that lifecycle stage. Their names describe
identity, not a transport class. The selected route is inspectable metadata on the target.

The owning operation can bind explicit shell defaults alongside its prepared environment. Context
consumers can inspect those defaults and override shell policy deliberately per invocation. The
target does not derive application policy from its route; accessing the target performs no account
lookup. Readiness targets retain their no-startup/no-staging preparation constraint even when a
later operation context will use an explicitly selected login shell.

The orchestrator decides whether an admin target uses canonical or native access before delivering
the context. Do not add an accessor that looks up arbitrary VMs, accepts a route override, or builds
a missing target. Native administrative recovery does not create an agent target or lend admin
authority to an agent consumer. When an operation switches from native bootstrap to canonical
initialization it constructs a new stage context with the newly established target.

| Context slice                  | Targets                                                                                        | Secrets and effects                                                                   |
| ------------------------------ | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Preflight                      | Only already-existing, already-available targets appropriate to command start                  | No resolved operation secrets; read-only probes, no staging or target realization     |
| Platform provisioning          | No guest target before creation; native delivery returned by provisioning feeds the next slice | Scoped provider secrets; platform owns provisioning through its established operation |
| Native bootstrap/recovery      | Native admin target under its active route and VM hold                                         | Required provider/recovery secrets only by default; no canonical repair prerequisite  |
| Canonical runup and operations | Established admin and/or agent targets supplied for that consumer                              | Scoped resolved secrets; runup remains read-only, operations perform requested work   |
| Later job observation          | Fresh target matching the job's VM instance and guest identity                                 | Newly authorized route/secret lifetime; no authority restored from the job reference  |

`RunContext` remains immutable and passive. Its accessors do no I/O and it owns no `ExitStack`. The
composition root owns resource cleanup and supplies targets tied to that lifetime. Use after closure
fails before dispatch. Target absence is reported according to the existing lifecycle's defer/error
rules, not reclassified as an optional transport feature.

Preserve `OperationScope` as descriptive data and `ScopedSecrets` as delivery of declared resolved
names. Passing a target remains an explicit act by the owning operation; this design does not claim
to add requester permission checks to today's context. It also does not authorize accessing
undeclared secrets through a transport factory hidden inside a capability.

Migrate context constructors and consumers together, including VM boundaries, agent realization,
session readiness/roll-forward, git-credential operations, and harness setup. Setup invocation types
that carry a runner receive the same execution target; their domain data need not all be folded into
`RunContext`. They must not retain a second execution API.

## CLI and recovery composition

`vm exec --platform` selects native access and minimal recovery policy at the operation boundary,
then invokes the common execution service. Normal `vm exec` selects canonical access and its usual
environment policy. Both share validation, command interpretation, results, and error rendering.

Minimal recovery avoids application environment resolution. Explicit workspace selection applies the
existing workspace validation, directory, and environment policy and therefore may legitimately fail
for an unavailable requested secret. The carrier itself supports env/cwd; this is no longer a
blanket `--platform`/`--workspace` incompatibility. CLI grammar and script/streaming options need an
LLD that preserves existing command quoting and stdin behavior intentionally.

Native shell requests check optional interaction and preserve provider-specific console guidance
when unavailable. Failed optional interaction does not imply native recovery is unavailable.

Retain PR #789's separation of power activation from canonical repair. Reconcile its execution entry
point with this shared service rather than carrying parallel implementations indefinitely. The
current draft takes no action on that PR.

## Results, errors, and observability

Use transport-neutral result, logger, and error vocabulary. Distinguish checked guest failure,
delivery failure, uncertain dispatch, observation timeout, incomplete output, optional-feature
refusal, and insufficient permission. These are typed facts, not necessarily one class per phrase.

Preserve separate stream bytes and guest status where observable; provide explicit text decoding for
ordinary callers. Combined terminal output is not advertised as separate stdout/stderr. A carrier
that cannot distinguish a remote signal from connection loss reports the ambiguity rather than
fabricating a signal. Command labels and job references remain safe to log.

One deadline covers an operation's preparation, dispatch, and observation budget. A wait deadline
does not cancel a process. A separate cancellation request has its own bounded observation. Retry
policy remains at the layer that can prove whether repeated dispatch is safe.

## Coordination with SSH isolation and consolidation

This is the proposed boundary for review with the SSH developer, based on the reduced FRD/HLA in
[PR #757](https://github.com/WayfarerLabs/agentworks/pull/757) at `2694d31a`. It records this
effort's integration plan; it does not amend the SSH effort's owned artifacts or claim its
agreement.

| Responsibility                                                                                         | Owner                                                        |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| Explicit SSH endpoint, user, identity, agent selection, trust and trust migration                      | SSH effort (#757)                                            |
| OpenSSH config isolation, common SSH/scp options, forwarding, subprocess I/O and connection keepalives | SSH effort (#757)                                            |
| Consolidating `ssh.py` and `transports/ssh.py` buffered internals with current interfaces/behavior     | SSH effort (#757)                                            |
| Remote Lima placement-host connection settings and provider-inner SSH isolation                        | SSH effort (#757)                                            |
| Application shell policy, commands/scripts, environment, cwd, elevation, sensitive-data policy         | `transport-improv`                                           |
| Common targets, optional features, files/jobs, results/errors and safe command retry policy            | `transport-improv`                                           |
| `RunContext` production cutover and core/plugin consumer migration                                     | `transport-improv`                                           |
| Final new-stack SSH adapter integration after the consolidated runner is available                     | `transport-improv`, using the SSH-owned connection machinery |

During consolidation the SSH effort owns edits to buffered SSH internals. This effort develops its
common layer and other carriers in separate modules. After the shared runner is available, the
transport effort owns the bounded integration change that connects the new target API, removes
superseded execution entry points, and moves common execution policy above the carrier. Connection
and trust policy remain SSH-owned. Remote Lima's host connection changes precede integration of its
new execution/job adapter; they need not delay that adapter's design or independent tests.

Before implementation, agree on the carrier seam: explicit connection plus prepared remote
invocation, finite input, I/O mode, and deadline in; observed process status, output, and available
completion evidence out. It must expose a single-attempt path. It does not compose workspace env,
select application privilege or shell, resolve `RunContext`, or decide to repeat a mutation. SSH's
legacy entry points may preserve their existing retry behavior during #757's consolidation; the new
stack cannot inherit hidden replay underneath its own no-ambiguous-retry guarantee.

#757 preserves the existing result interfaces and corrects misleading status-255 diagnostics. This
effort owns the common outcome model and maps only evidence the carrier actually provides. OpenSSH
status 255 alone cannot distinguish a guest exit from connection failure; nested Lima status does
not identify a failed inner hop. Preserve that uncertainty without adding a completion protocol,
reconnect mechanism, new SSH library, or second trust implementation.

Prepared environment, sensitive I/O, and shell wrappers currently reside partly in SSH. The
integration inventory must assign each to either common preparation or carrier delivery and remove
duplicate application of that policy. Raw SSH I/O and explicit environment delivery primitives can
remain below the seam; deciding their meaning for an Agentworks operation stays above it.

## Parallel build and cutover

Use the destination package structure for the new stack, with development/test composition roots
that exercise its contracts while production factories and `RunContext` retain the old stack. Use an
internal prototype context in those tests, not two public target types in the production context.
Never dispatch a mutating workflow through both stacks to compare results.

Prove the new layer against SSH, QGA, and placement-host differences early, then cover the remaining
adapters and complete workflows. Integrate #757's connection machinery once its agreed seam is
available. Only after the new stack's acceptance gates pass does a coherent cutover change
factories, context producers/consumers, plugin delivery, and direct service entry points. Remove the
old stack and temporary bridges in that cutover increment. The detailed gates and treatment of
existing jobs are in the [migration strategy](migration-strategy.md).

This permits development coexistence without a released old/new selector or two plugin execution
APIs. The current PR remains a design checkpoint; implementation landing units are decided in the
plan after the dependency and complete-cutover scope are known.

## Alternatives and remaining decisions

PyInfra informs centralized command preparation but is not a proposed dependency. Its host/state
integration and deployment model do not replace our provider carriers or `RunContext` ownership.
Evidence and its limits are recorded in [prior art](prior-art-research.md).

A universal full transport would misrepresent QGA interaction. Retaining the current minimal native
contract would push required file/script/job mechanics back into callers. The proposed target keeps
those operations common while making the small set of real optional I/O features explicit.

Before implementation, resolve the request/result signatures, readiness no-staging enforcement, job
storage/ownership/retention and process-group protocol, shell startup/lookup behavior, transfer
bounds and path policy, and WSL2 lifetime evidence. Review the proposed integration seam with the
SSH developer and re-inventory then-current callers before implementation. The new API remains
driven by the execution contract rather than by preserving the old runner's structure.
