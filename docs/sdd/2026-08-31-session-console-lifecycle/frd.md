# Functional Requirements: Session and Console Lifecycle Grammar

- Status: Draft for requirements review
- Date: 2026-08-31
- Scope: truthful, consistent runtime lifecycle commands for sessions and named consoles
- Supersedes: the current command and implementation vocabulary established by the completed
  `2026-08-04-session-resume` effort

## Summary

Agentworks currently exposes different lifecycle models for three resources that have a durable
identity and a live runtime:

- a VM has explicit `start` and `stop` commands;
- a session has `stop`, but its inverse is called `resume` even though it may replace a running
  process and cannot guarantee that its harness conversation resumes; and
- a named console has no explicit runtime lifecycle. `console attach` silently creates or rebuilds
  its tmux state and may resolve secrets before joining it.

The command grammar will distinguish durable resource creation, runtime start, runtime stop, runtime
replacement, and interactive attachment. Sessions and consoles gain the same operator vocabulary
where the underlying operation is the same: `create`, `start`, `stop`, `restart`, `attach`, and
`delete`. VMs retain their existing `create`, `start`, `stop`, `delete`, and `reinit` surface; this
effort does not invent a VM restart merely for symmetry.

For sessions, `start` and `restart` both request continuation of prior harness state when the
selected harness integration supports it, unless the operator passes `--force-new`. Continuation is
a harness decision, not the name or guaranteed outcome of the Agentworks lifecycle operation. A
harness integration supplies start behavior. Core owns exact tmux runtime teardown, liveness
verification, and fail-closed cleanup of proven-stale broken runtime state. A harness integration
exposes neither stop nor restart and never owns the old process.

The change is complete across the current system. Commands, manager operations, capability APIs,
configuration vocabulary where affected, output, errors, logs, completion, guide content, operator
and contributor documentation, tests, and live identifiers use the new terminology. Historical
release notes and locked SDD records remain historical.

## Goals

1. Make one verb mean one operator-visible lifecycle action.
2. Make `attach` join an existing runtime without creating or replacing it.
3. Give sessions and consoles explicit, predictable start, stop, and restart operations.
4. Treat harness conversation continuation as a best-effort start decision rather than a promised
   resource lifecycle result.
5. Keep process ownership and teardown policy in core while allowing the harness integration to
   supply launch behavior.
6. Preserve a bounded command compatibility path while removing obsolete internal vocabulary in one
   cutover.
7. Share a CLI grammar across VMs, sessions, and consoles without introducing a speculative common
   runnable framework.

## Terminology

- **Durable resource**: the Agentworks database-backed VM, session, or console identity and its
  declared configuration.
- **Runtime**: the live external realization of that resource: a powered VM, a session's tmux server
  and workload, or a console's tmux view.
- **Initial start**: the first runtime launch performed as part of resource creation.
- **Subsequent start**: launching an existing durable resource whose runtime is not currently
  running.
- **Restart**: core stopping or replacing a resource's current runtime and then starting it again.
- **Continuation**: a harness integration attempting to continue its own prior conversation or
  workload state while constructing a new session runtime. A continuation attempt may still launch
  fresh, defer a choice to the harness, or fail.
- **Attach**: joining a running interactive runtime without creating or replacing that runtime.

## Users and Stories

### Operator returning to a stopped session

The operator runs `agw session start NAME`. Agentworks starts the existing logical session and asks
its harness integration to continue prior harness state when supported. If no prior state is usable,
the harness starts fresh and Agentworks does not falsely claim that a conversation resumed. The
operator can pass `--force-new` to require a fresh harness start instead.

### Operator deliberately replacing a running session

The operator runs `agw session restart NAME`. Agentworks performs complete core-owned teardown, then
invokes the same harness start operation used for a stopped session. The command itself is
sufficient authorization to replace the running runtime; it is not mislabeled as a resume.

### Operator attaching to a session

The operator runs `agw session attach NAME` and either joins the existing runtime or receives an
actionable not-running or broken-state error. Attachment never starts or replaces the session.

### Operator using a named console

The operator can create, start, stop, restart, and attach to a console as distinct actions. Secrets
needed to construct console panes are resolved during create/start/restart, not as a hidden side
effect of attach. Attaching to an already-running console does not rebuild it.

### Harness integration author

The author implements one start operation. Start receives `force_new`, which defaults to false. The
integration examines its own state and decides whether and how to continue when continuation is
allowed. Core owns signaling, tmux teardown, escalation, and final state. The integration implements
neither stop nor restart.

### Existing automation owner

Existing `session resume` and `console attach --recreate` invocations remain usable in 0.19, warn
through the ordinary deprecation channel, and identify the canonical replacement. They are removed
in 0.20. New help, completion, examples, and documentation expose only the new grammar.

## Requirements

### R1: One lifecycle vocabulary

The following meanings are canonical:

| Verb      | Meaning                                                                                  |
| --------- | ---------------------------------------------------------------------------------------- |
| `create`  | Persist a new durable resource and perform its initial start where the resource runs.    |
| `start`   | Make an existing durable resource's absent runtime run.                                  |
| `stop`    | Stop the runtime while retaining the durable resource.                                   |
| `restart` | Replace or re-establish the runtime, then leave it running.                              |
| `attach`  | Join an existing running interactive runtime without realizing or replacing it.          |
| `delete`  | Remove the durable resource under its domain-specific cleanup and confirmation policy.   |
| `reinit`  | Reconcile VM or user initialization; never a synonym for process or power-state restart. |

The vocabulary is a CLI convention, not a shared implementation hierarchy. Session, console, and VM
lifecycle managers retain domain-specific state, transport, recovery, and secret behavior. This
effort MUST NOT add a generic `Runnable` type, generic lifecycle manager, or cross-domain status
model solely to make the command names look alike.

### R2: Session start

`agw session start NAME` MUST start an existing stopped session. It MUST preserve the current
session row, workspace, agent/admin identity, template selection, instance overlay, environment
construction, and per-session socket model. It MUST preserve every foreign harness integration state
namespace. The selected integration continues to own its own namespace and MAY update it as part of
choosing a continuation or fresh identity. Start MUST request harness continuation because the
logical Agentworks session already exists. On a stopped session, `session start NAME --force-new`
MUST instead require a fresh harness launch while retaining the same Agentworks session resource and
its other durable configuration.

If the session is already running, `session start` MUST report that fact and succeed without
stopping, replacing, resolving runtime-only secrets, or invoking the harness start operation.
`session start NAME --force-new` on a running session MUST instead fail without mutation and direct
the operator to `session restart NAME --force-new`; start MUST NOT acquire restart semantics from
the policy flag. A dedicated server that remains reachable after the canonical named tmux session
disappears is a residual runtime, not an indeterminate broken runtime. Start MUST remove that whole
reachable server without requiring `--force`, then launch according to the requested continuation
policy. If the session is broken because its ordinary tmux authority cannot be reached, the command
MUST refuse before destructive action unless the operator passes the command's explicit force
option. A forced start MUST use the same core-owned fingerprinted stale-state recovery as restart,
verify that the broken runtime no longer exists, clean only the exact validated managed socket, and
then start the replacement. It MUST NOT signal a numeric PID. If prior-server absence cannot be
verified, it MUST fail without cleanup, stopped-state persistence, or harness start.

`session start --all` MUST operate on every matching stopped session and leave already-running
sessions unchanged. The existing VM, workspace, agent, and admin filters MUST remain available and
retain their current composition rules. `--force-new` MUST apply the fresh policy to every selected
stopped session, while an already-running selected session MUST produce the same actionable error as
the named command rather than be replaced. That per-session error MUST NOT prevent other selected
stopped sessions from starting; the batch MUST retain aggregate partial-failure reporting. The
canonical start command MUST NOT need a redundant `--all-stopped` spelling.

### R3: Session restart

`agw session restart NAME` MUST leave the named session running after replacing or re-establishing
its runtime. For a running session, core MUST complete preflight and required secret resolution
before destructive action, authoritatively remove the existing tmux runtime, verify that the old
runtime no longer owns the session, and only then invoke the harness start operation. A broken
runtime MUST require the explicit force option before stale-state recovery.

A stopped session passed to `session restart` MUST be started without manufacturing a meaningless
stop operation. Both the running and stopped paths MUST request continuation from the harness
integration because the durable logical session already exists. `session restart NAME --force-new`
MUST perform the same core lifecycle transition but require a fresh harness launch afterward.
Restart MUST remove a reachable residual dedicated server before launch without requiring `--force`.

The restart verb itself is explicit authorization to replace a running runtime. Canonical
single-session restart MUST NOT prompt merely because the session is running, and MUST NOT carry a
`--yes` option whose only purpose is to suppress that prompt.

`session restart --all` MUST apply the same state-aware operation to every matching session. It MUST
retain the existing VM, workspace, agent, admin, force, and `--force-new` behavior across the
selected set. It MUST NOT preserve the current `resume --all` confirmation ceremony under the new
explicit verb.

### R4: Session stop and attach remain distinct

Core MUST own the full stop state machine. Every current admin or agent session has a dedicated tmux
server and socket. An operation that intentionally ends a reachable current runtime MUST invoke
tmux's server-destruction operation through that persisted, validated managed socket. It MUST NOT
inject terminal keys, enumerate pane processes, or add a grace delay before that operation. The
dedicated tmux server is the complete managed runtime boundary: teardown MUST include every session,
window, and pane in it, including tmux state the operator created after launch. Core MUST verify
that the server exited before persisting the stopped state. This includes direct stop, the teardown
half of restart, direct session deletion, and cascading workspace, agent, or VM deletion where the
runtime can still be addressed.

A reachable legacy row that may use a shared default tmux server MUST instead address its exact tmux
session as `=NAME` and invoke `kill-session`. Core MUST verify only that exact session is absent; it
MUST NOT destroy, signal, or require exit of the shared server.

Shared teardown logic used by stop, restart, direct delete, and cascading delete MUST have one core
authority with explicit policy inputs for their real differences. The implementation MUST remove
duplicated session kill loops. Restart MUST NOT grow a separate command-local kill implementation,
and the harness integration MUST NOT expose stop or restart operations.

A broken dedicated tmux runtime whose socket cannot perform exact teardown MUST refuse stale-state
recovery unless the caller supplied the operation's explicit force option. Force recovery MUST use a
stable tmux-server process fingerprint captured at creation: VM boot ID, positive server PID, and
Linux process start time. A changed boot or absent stored PID proves the prior server gone without a
start time. On the same boot with that PID still present, a valid stored start time is required and
only a mismatch proves prior-server absence. Core may then clean the exact validated managed socket
and persist stopped state. It MUST NOT signal a numeric PID or attempt to kill a still-live server
whose tmux control socket is unusable. Evidence missing from the applicable branch or any
indeterminate identity MUST fail closed with manual-recovery guidance. The same boundary applies to
legacy rows whose PID may identify a shared tmux server.

`session attach` MUST continue to join only a running session. A stopped or broken session MUST
produce an actionable error identifying the appropriate lifecycle command. Attach MUST NOT start,
restart, repair, or otherwise replace the session runtime.

A residual dedicated runtime MUST behave like a reachable runtime for stop and deletion: core
destroys the dedicated server without requiring `--force`. Attach MUST refuse because the canonical
named session no longer exists and direct the operator to start or restart it.

### R5: Console lifecycle

A named console is a durable saved definition with an independently disposable tmux runtime.

`console create` MUST persist the definition and attempt its initial detached start. The initial
start MUST use the same realization operation as `console start`; create MUST NOT preserve a second
console-building implementation. If the definition is valid and persisted but remote realization
fails, Agentworks MUST retain the definition, report that it was created but is not running, and
identify `console start NAME` as the retry. It MUST NOT report an unqualified successful create.
Realization MUST publish the canonical runtime identity only after every required construction step
succeeds. Any managed partial-build artifact MUST have an identity that cannot collide with another
console's canonical runtime and MUST be removed and verified absent before an ordinary build failure
is reported. Because a deleted predecessor may have left a same-name tmux runtime behind after
best-effort cleanup, initial create MUST never adopt an existing runtime by name. After resolving
everything needed for the replacement, it MUST prove the predecessor runtime absent or remove it
through the console's core teardown operation and verify removal. Until that boundary succeeds,
create MUST fail without leaving the new durable definition addressable. Only after predecessor
absence is verified may a later build failure retain the definition for an ordinary `console start`
retry. This is a policy input to the one realization operation, not a second builder or a new
persisted runtime-generation model.

`console start NAME` MUST build the tmux runtime from the complete current definition when it is
absent. If it is already running, start MUST succeed without rebuilding it or resolving pane
secrets. Pane environment secrets and other inputs needed to open new shells MUST resolve before the
first construction mutation. A residual managed partial-build artifact is not a running console:
start MUST remove and verify it before resolving construction-only secrets or building, while attach
MUST refuse without mutating it. That cleanup is an intentional recovery step, not construction, and
MUST NOT prompt for pane secrets that may never be used.

`console stop NAME` MUST remove only the live Agentworks console tmux runtime and any managed
partial-build artifact for that definition. It MUST retain the console definition, membership,
ordering, and shell declarations. Repeated stop after both managed runtime identities are absent
MUST be an idempotent success.

`console restart NAME` MUST rebuild the runtime from the complete current definition. It MUST
resolve every required pane input before killing a healthy existing runtime, then use core-owned
teardown and the same start realization used by create and start. If the runtime is absent, restart
MUST behave as start without manufacturing a stop failure.

`console attach NAME` MUST attach only to an already-running console. It MUST NOT create, rebuild,
or resolve console pane environment secrets. It MAY still cross the ordinary VM activation and
transport credential boundaries required to reach the machine, just as session attachment does. When
the console runtime is absent, it MUST fail with guidance to run `console start NAME`.

`console delete` MUST continue to remove the durable definition and perform best-effort cleanup of
both canonical and partial managed runtime artifacts. It MUST remain distinct from stop.

Console list, describe, names-only output, and shell completion MUST remain DB-only and free of live
runtime probes. Operators can invoke idempotent start or receive attach's actionable not-running
error without turning inspection into a remote operation.

Every tmux target used by console lifecycle MUST select the full generated session name exactly.
Prefix matching MUST NOT permit one console or staging artifact to inspect, attach to, rename, or
remove another console whose name merely shares a prefix.

### R6: Harness start boundary

The harness integration capability MUST expose one launch operation named `start`. It MUST NOT
expose `stop`, `restart`, or `resume` lifecycle operations. Core owns session status inspection,
exact tmux teardown, liveness verification, forced stale-state cleanup, socket cleanup, tmux
replacement, and the decision to invoke start. A harness integration MUST NOT kill, replace, attach
to, or persist liveness for the Agentworks session runtime.

`start` MUST receive a keyword-only `force_new` Boolean whose default is false. The CLI and service
layers MUST retain that positive spelling so `--force-new`, `force_new`, and the capability input
form one vocabulary. Core sets the operator policy. When `force_new` is false, the integration MUST
inspect and interpret its own state to determine whether continuation is supported, available, and
safe. When `force_new` is true, it MUST take its fresh path without probing whether prior state can
be resumed, except for integration-local work strictly required to retire or rotate its own binding
safely:

- session create MUST pass `force_new=True` and MUST NOT permit adoption of state belonging to a
  deleted predecessor that reused the name or workspace;
- session start MUST use the false default and pass true under `--force-new`; for a running session,
  core MUST refuse rather than invoking start or performing teardown;
- session restart MUST use the false default and pass true under `--force-new`, after core has
  completed teardown.

When `force_new` is false, an integration that supports continuation SHOULD resume when its own
compatible prior state is usable. It MUST be free to start fresh when no such state exists and to
defer a choice to its own tool when that is the safest available behavior. When `force_new` is true,
the integration MUST select a fresh start and MUST NOT adopt or resume prior harness state. An
integration that does not support continuation MUST still be a valid implementation of start.

This effort MUST NOT increment a capability contract version. Any in-repository-only capability
contract whose public shape this effort changes MUST use version 1 across its descriptor,
implementations, permanent docs, samples, upgrade guidance, and tests, without an adapter or
compatibility surface. Harness-integration changes and therefore MUST return to version 1. No other
capability contract change is currently required; unchanged contracts, including vm-platform, MUST
retain their existing version rather than be renumbered solely for consistency. A future externally
supported contract MAY introduce a new version when an actual compatibility boundary exists.

To perform a forced fresh start, the selected integration MAY rotate or remove its own persisted
conversation binding. It MUST preserve every foreign integration namespace and MUST NOT delete,
archive, or mutate the prior external harness conversation merely because Agentworks stopped using
its binding. Any fresh identity or binding the integration allocates before runtime construction
MUST be stable across an ordinary retry after a failed runtime launch while the same durable session
survives. Retry MUST NOT abandon or rotate that known pending binding merely because launch failed.
An external identity unknown until after launch, and a failed session creation whose durable row is
rolled back, are outside this retry-stability requirement.

The start operation SHOULD report the launch decision it can truthfully make alongside the command
needed to create the pane. Agentworks MUST surface a useful integration decision when available
without claiming that an external harness successfully resumed merely because the generated command
requested it. The architecture phase owns whether a small return object can replace the current
mutable launch-note side channel and whether confirmation beyond the pre-launch decision is
feasible; the FRD does not require a status taxonomy.

### R7: Complete vocabulary cutover

Current implementation and permanent collateral MUST use lifecycle terms according to what the
operation actually does. The cutover includes:

- CLI commands, command help, dynamic completion, and recovery hints;
- session and console manager entry points, private helpers, parameters, variables, operation names,
  logger labels, sections, results, and errors;
- the harness integration capability contract, implementations, configuration models, samples,
  manifests, plugin-author guidance, and capability documentation;
- guide concepts, CLI and command reference documentation, operator guides, root and package
  READMEs, upgrade guidance, and release notes; and
- tests, fixtures, fakes, and structural contract coverage.

The word `resume` MAY remain only where it truthfully describes harness-level continuation, such as
an external tool's resume flag, a continuation decision, or historical material. It MUST NOT remain
as the current name of the Agentworks session lifecycle operation, a core manager operation, a
logger operation, or a harness lifecycle method. Historical changelog entries, locked SDDs, and
third-party command names MUST NOT be rewritten to manufacture current terminology.

### R8: Compatibility and migration

In 0.19, `agw session resume` MUST remain as a hidden, warning-producing command wrapper around the
new canonical lifecycle operations:

- a named session MUST preserve the prior state-aware behavior by dispatching to restart;
- `--all-stopped` MUST dispatch to `session start --all`; and
- `--all` MUST dispatch to `session restart --all`.

Before a named or `--all` wrapper invocation replaces any running session, the CLI wrapper MUST
preserve the prior confirmation gate unless legacy `--yes` is present. That bounded interaction MUST
use read-only status facts and MUST NOT repair or mutate durable state before consent. When
interactive input is unavailable, a replacement that would require confirmation MUST refuse unless
`--yes` is present. The interaction MUST remain in the wrapper; canonical restart services MUST
remain prompt-free and MUST own the actual lifecycle transition. The wrapper MUST use the ordinary
suppressible deprecation channel, MUST identify the exact canonical mapping for that invocation, and
MUST NOT duplicate lifecycle implementation. Canonical help, completion, docs, and examples MUST
expose only start/restart. The compatibility wrapper and its legacy-only options MUST be removed in
0.20.

For 0.19, `console attach --recreate` MUST remain as a deprecated compatibility form that performs
restart followed by attach through the canonical service operations. Ordinary `console attach` MUST
adopt the new attach-only behavior immediately; preserving implicit first-attach realization would
preserve the design defect. The compatibility form MUST be removed in 0.20.

Any affected configuration spelling MUST receive an explicit migration decision during architecture.
A retained alias MUST be bounded and must normalize into the one canonical model; the implementation
MUST NOT carry parallel capability contracts or execution paths.

### R9: VM alignment without false symmetry

VMs retain the existing meanings of `vm create`, `vm start`, `vm stop`, `vm delete`, and
`vm reinit`. Session and console command help and documentation SHOULD use the same start/stop
meanings where they apply. This effort MUST NOT:

- add `vm restart` without a separately demonstrated operator need and a complete power-cycle,
  reconnect, and Tailscale contract;
- rename `vm reinit` to restart or imply that initialization reconciliation is a power operation;
- alter VM auto-activation or operator-stopped semantics merely to match sessions or consoles; or
- create a shared runnable implementation abstraction.

### R10: Safe lifecycle boundaries

Every start or restart MUST finish validation, readiness checks, and required pre-mutation secret
resolution before destroying a healthy runtime. Refused, invalid, or declined operations MUST not
prompt for secrets they will not use. A failure before teardown MUST leave the existing runtime
unchanged. A failure after a completed teardown MAY leave the durable resource stopped, but MUST
retain enough state for an ordinary start retry and MUST report that state honestly.

Attachment MUST not become a shortcut around these boundaries. Start/restart MUST not silently
attach. Runtime creation MUST not be hidden behind inspection, list, completion, or attach commands.

## Acceptance Criteria

1. Session help and completion expose canonical `start`, `stop`, `restart`, and `attach` operations;
   console help and completion expose the same runtime verbs.
2. Starting a stopped session invokes the harness start operation with continuation requested;
   ordinarily starting a running session is a no-op; starting it with `--force-new` errors; and
   restarting it completes core-owned teardown before invoking that same start operation.
3. `--force-new` makes session start/restart request a fresh harness launch when that operation
   launches a runtime; initial session creation always uses the fresh policy.
4. Harness integrations expose start and no stop/resume/restart lifecycle methods; core retains sole
   authority over teardown and stopped state.
5. Continuation-capable harness integrations preserve their current safe continuation behavior;
   integrations that cannot continue remain valid and start fresh without false success claims.
6. Console create/start/restart share one realization path; console stop removes only runtime;
   console attach never realizes or rebuilds runtime.
7. Initial console create refuses without retaining its new definition when a same-name predecessor
   runtime cannot be proven absent; attach and idempotent start cannot adopt that predecessor.
8. Console pane secrets are not requested by attach when the console runtime is already present or
   absent; the relevant create/start/restart operation owns those prompts.
9. Session batch start/restart/stop filters retain their current selection semantics without the
   redundant canonical `--all-stopped` form.
10. The bounded compatibility commands dispatch through canonical operations and are absent from
    canonical help, completion, examples, and documentation.
11. A one-time scoped current-surface review finds retired lifecycle terminology only in bounded
    compatibility code, genuine harness continuation behavior, third-party commands, or historical
    artifacts; persistent tests guard removed APIs and canonical behavior rather than words.
12. Permanent docs and guide content explain the lifecycle without depending on this SDD.
13. Focused deterministic CLI, service, orchestration, secret-boundary, capability-contract, and
    completion tests demonstrate the full state matrix; representative live console/session tests
    cover admin and agent targets plus continuation and forced-fresh behavior.
14. Direct and cascading session teardown route through one core stop authority; current dedicated
    runtimes use whole-server destruction, legacy shared-server rows use exact session destruction,
    and neither path injects pane signals or a grace delay. Forced broken-state recovery cleans
    exact managed state only after proving the prior server is already absent; it never signals a
    numeric PID.

## Non-goals

- A generic runnable framework or shared lifecycle manager.
- A new VM restart command.
- Guaranteeing that an external harness conversation resumes successfully.
- Teaching Agentworks how each harness stores or restores its conversation beyond the selected
  harness integration's contract.
- Changing console membership, ordering, shell-layout, or live best-effort synchronization semantics
  except where needed to establish the explicit lifecycle boundary.
- Changing VM provisioning, power, reinitialization, or Debian release-transition behavior.
- Changing the vm-platform capability contract.
- Introducing cgroup or process-namespace containment. Systemd-managed per-session cgroups are the
  security follow-up tracked in [issue #715](https://github.com/WayfarerLabs/agentworks/issues/715).
- Rewriting historical release notes or locked SDD artifacts.

## Requirements Rulings

- **2026-08-31 — Base branch:** Design and implementation are based on the tip of
  `feat/debian-release-transition-sdd` so the lifecycle work composes with the in-flight Debian
  release transition rather than resolving a large integration conflict afterward.
- **2026-09-01 — Main rebase and release window:** After the Debian transition merged in PR #702,
  the design branch rebased onto `main` at `8695afcd833790ee433b50bb9f5d5c696177233d`, and PR #710
  changed its base to `main`. Release 0.17.0 landed in the same range, and the fully green 0.18.0
  release PR #714 is already ready before this design and its two implementation PRs. Compatibility
  wrappers therefore ship in 0.19 and are removed in 0.20. PR #709 changes typing metadata only;
  neither merge changes the session, console, or harness implementation contract described here.
- **2026-08-31 — Continuation on both operations:** Session start and session restart both request
  harness continuation when supported. `--force-new` forces a fresh harness launch. Session create
  is always fresh. `session start --force-new` on a running session errors rather than taking
  ordinary start's idempotent no-op path or acquiring restart semantics.
- **2026-08-31 — Harness lifecycle boundary:** A harness integration offers start and an optional
  default-no-op cooperative stop hook, but no restart operation. Core orchestrates liveness,
  graceful fallback, bounded force kill, persisted state, and subsequent start for restart.
- **2026-09-01 — Stop seam removed under YAGNI (initial teardown shape, superseded below):** The
  harness contract offers only start. No current integration has a distinct graceful-stop request
  and no planned feature needs one. Core owns teardown rather than adding a speculative harness
  hook. The following ruling replaces this ruling's initial pane-signal/grace implementation shape.
  A harness-specific stop seam may be designed when the first concrete consumer demonstrates a
  requirement that core teardown cannot satisfy.
- **2026-09-01 — Dedicated tmux server is the teardown authority:** Every current Agentworks session
  has its own tmux server, but a process inside it can create sibling tmux sessions as well as panes
  or windows. `kill-server` through the persisted, validated managed socket therefore replaces
  injected `C-c`, pane-PID SIGTERM, and the grace phase for reachable current-runtime teardown. It
  truthfully destroys the complete tmux-owned runtime without pretending that an
  application-specific graceful shutdown occurred. A reachable legacy row that may share a default
  server uses exact `kill-session -t =NAME` instead. Broken dedicated-server recovery uses a
  persisted boot/PID/start-time fingerprint only to prove that the old server is already absent
  before exact admin-or-agent socket cleanup. It never signals a numeric PID and fails closed when a
  live server cannot be controlled through tmux. Strong containment and reliable whole-session
  process termination are deferred to the systemd/cgroup design in issue #715.
- **2026-08-31 — Internal capability versions:** This effort does not bump capability versions. Any
  in-repository-only capability contract it changes uses version 1 without compatibility machinery;
  unchanged capability contracts are not renumbered. All current harness integrations ship in this
  repository, so the breaking lifecycle cleanup returns harness-integration to version 1.
