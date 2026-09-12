# Transport Improvements: High-Level Architecture

- Status: First draft for operator review
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

Programmatic scripts use the supported guest's Bash in non-login mode with documented startup
isolation. Literal execution preserves arguments even where delivery requires shell quoting.
Interactive login shells keep login behavior. Existing callers relying on profile initialization
must request that behavior explicitly or supply the executable/environment they actually need.

### Optional channel features

Use one immutable description with a closed set of optional features: interactive terminal and
direct live stdio streaming. A feature declaration is backed by an implementation hook; registration
and conformance checks reject contradictory declarations. A typed unsupported-operation error is the
default for an absent optional hook. Required methods have no unsupported default.

Platform metadata can declare native interaction absent before constructing a target, allowing an
early refusal. The resulting target is the authority for the opened channel, and conformance checks
ensure the platform declaration agrees. A capability description is not a health probe or permission
grant. Avoid calling these flags "capabilities" in APIs where that would confuse them with
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

Sensitive values travel through protected finite input or restricted staging, never interpolated
into argv. Script source and program stdin have separate storage/delivery even when both need
staging. Sensitive execution defaults to discarded streams, including remote job output. Any
temporary sensitive material is private to the executing identity and removed under an owned
lifecycle; disconnect-related cleanup debt remains visible for the next authorized operation. This
is protection against incidental disclosure, not a sandbox against guest root or malicious
in-process plugins.

## Delivery and bootstrap

The internal carrier interface describes dispatch of a prepared guest invocation with finite input
and observation of completion. A synchronous CLI carrier and QGA's dispatch/poll carrier satisfy the
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
sites. The initial implementation direction is a small staged guest wrapper using existing userspace
detachment and process-group mechanisms, with identity-private records for launch, stdout/stderr,
and completion. It is not a service scheduler or replacement for sessions.

Each launch has a fresh identifier. An explicit existing reference means observe that launch;
identical command text or a reused pathname does not. The guest publishes launch/completion facts
atomically. A reference identifies the VM instance, guest identity, boot, and job, without secrets
or an embedded connection. PID alone is insufficient proof of ownership.

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

## Alternatives and remaining decisions

PyInfra informs centralized command preparation but is not a proposed dependency. Its host/state
integration and deployment model do not replace our provider carriers or `RunContext` ownership.
Evidence and its limits are recorded in [prior art](prior-art-research.md).

A universal full transport would misrepresent QGA interaction. Retaining the current minimal native
contract would push required file/script/job mechanics back into callers. The proposed target keeps
those operations common while making the small set of real optional I/O features explicit.

Before implementation, resolve the request/result signatures, readiness no-staging enforcement, job
storage/ownership/retention and process-group protocol, transfer bounds and path policy, and WSL2
lifetime evidence. Reconcile the accepted API against then-current SSH work afterward; this draft
does not adopt an in-flight SSH design as a constraint.
