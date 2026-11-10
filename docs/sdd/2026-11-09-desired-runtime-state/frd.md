# Desired runtime state and reconciliation

Status: requirements draft under the operator's authorized single-PR SDD effort.

## Purpose

After a VM shuts down or restarts, the operator should be able to restore the intended running
environment with one command. Agentworks must remember which VMs, sessions, and named consoles
should run, distinguish that intent from current runtime observations, and reconcile the two. Batch
operations must collect secrets before workload execution so the operator can finish interaction and
leave the operation running unattended.

## Operator and stories

The operator manages a fleet containing all three runnable kinds: VMs, sessions, and named consoles.
Their definitions outlive any particular runtime. None has a terminal completion state. A VM
shutdown, session exit, or lost console runtime does not end its desired-running intent.

- After an external VM reboot, reconcile that VM and restore its desired-running sessions and named
  consoles without remembering their names or restarting healthy peers.
- Reconcile the fleet after an outage, with useful progress and results even when one VM cannot be
  recovered.
- Stop a session or console intentionally and have later reconciliation leave it stopped.
- Stop a VM intentionally while retaining which child sessions and consoles should return when the
  VM is explicitly started again.
- Preview the required changes before executing them.
- Start or restart several sessions, supply the required secrets together, and avoid returning
  repeatedly to answer prompts between individual workload launches.

## Requirements

### R1. Durable intent, independent observation

Record desired runtime state durably for VMs, sessions, and named consoles. Sessions and consoles
support running and stopped intent. VMs distinguish running, stopped, and available on demand:
available on demand permits activation when a workload needs the VM without requiring an idle VM to
run merely because fleet reconciliation was requested.

Keep each kind's current observed status meanings. Desired state is not a cached observation, health
verdict, initialization status, or harness conversation state. Inspection must expose recorded
intent alongside current status when status is requested. Plain inventory and completion remain
local reads. Inspection must not change intent or repair runtime state.

Unexpected disappearance, external shutdown, and a session exiting normally leave intent unchanged.
Only explicit management actions change intent. There is no completed-job policy, automatic
retirement, retry daemon, or background controller in this effort.

### R2. Lifecycle commands maintain intent

Creation with an initial start, explicit start, and explicit restart express running intent.
Explicit stop expresses stopped intent, including when the runtime is already absent. An explicit VM
start clears an intentional-stop prohibition. Dependency activation of an on-demand VM must not
silently convert its policy into always running.

Provide an explicit operator action to select any supported desired state without immediately
changing runtimes. This includes selecting available-on-demand intent for a new or existing VM, and
adopting existing resources before reconciliation. Architecture owns the command spelling.

Validate local arguments, resource identities, and requested configuration and obtain any required
confirmation before recording intent. That is the acceptance boundary. Persist accepted intent
before attempting runtime changes. Guest checks that require activation belong to subsequent
execution preparation, not local acceptance. A subsequent runtime, guest-readiness, or credential
failure must leave the accepted target available for a later reconciliation retry. Refused or
cancelled requests before acceptance must not alter intent. Report the recorded target and actual
outcome honestly when execution fails.

Reconciliation applies recorded intent without changing it. Attach, status, describe, reinit,
console membership edits, and external runtime changes do not independently change running intent.
Deleting a durable resource removes its intent under the existing deletion policy.

### R3. Explicit reconciliation scopes

Provide commands to reconcile a named VM with its sessions and named consoles, or all managed VMs
and their runnable children. Also support focused reconciliation of a named session or console.
Resource names and bulk selection follow existing CLI conventions. Selection must be explicit; an
omitted operand must not silently select the fleet.

Reconciliation changes runtime state only. It must not create missing durable definitions, delete
resources, reinstall software, reinitialize environments, adopt an unknown runtime identity, replace
a healthy runtime to refresh configuration, or force a fresh harness conversation.

For an already-running target, running intent is a no-op. For an absent target, running intent
requests its ordinary domain start behavior, including existing harness continuation semantics.
Continuation is best effort and retains the integration's current limitations. Stopped intent stops
an existing runtime while retaining its definition; an already-absent runtime is a no-op.

### R4. Dependency order and explicit VM shutdown

Restore required VMs before sessions, and restore desired-running sessions before named consoles on
the same VM. A console rebuild includes its stored member order, configured shell panes, and admin
shell where configured. Shared sessions referenced by multiple consoles are acted on once.

Console membership does not own session intent. Starting or reconciling a console must not start a
member session whose desired state is stopped. Stopping a console must not stop its member sessions.
Preserve existing console behavior for displaying unavailable member sessions.

An intentionally stopped VM takes precedence over desired-running children. Keep it stopped, retain
child intent, and report those children as blocked rather than clearing their intent or starting the
VM. After an explicit VM start, reconciliation can restore them. Do not power a VM on solely to stop
child runtimes known absent because the VM is off. Where shutdown actions are needed on a running
VM, stop affected dependents before powering it off.

A focused child reconciliation may activate its on-demand VM. It must respect a stopped VM and must
not expand into unrelated sibling workload changes.

Focused console reconciliation restores only that console and its required VM, not member session
runtimes. VM-scoped reconciliation supplies the broader session-before-console restoration order.

### R5. Safe observation, recovery, and repeatability

Use the existing domain-owned runtime identity and recovery boundaries. Unknown observations must
never be treated as proof of absence or permission to replace a runtime. Report them as blocked.
Residual state may use existing safe ordinary-start cleanup. Broken or ambiguous state requiring
force must remain blocked with an actionable recovery explanation; reconciliation supplies no
implicit force or weakened identity check.

Revalidate identity and intent before consequential changes so a concurrent stop, delete, or
same-name replacement cannot be overridden using an earlier plan. Repeated reconciliation after
success must leave healthy runtimes unchanged. Retrying a partial run must continue from current
observations without duplicating successful launches or resetting harness state.

### R6. Preview and results

Provide a dry-run form of reconciliation that reports selected targets, their desired state,
observations when available, proposed actions, dependencies, and blockers. It must not change
intent, activate VMs, repair runtime state, resolve workload secret values, or launch anything. Use
existing status-observation credential rules when provider observation requires credentials. When a
stopped VM prevents guest inspection, state that dependent actions are conditional rather than
presenting unobserved guest facts as certain.

Execution must report unchanged, changed, failed, and blocked resources. A failure in one VM must
not suppress independent VM results. A failed prerequisite blocks only the actions that need it.
Return a nonzero exit when any selected managed target remains failed or blocked. User cancellation
aborts further work and is never downgraded to a warning; completed actions remain honestly reported
and retryable. Do not promise transaction rollback for external runtime changes.

### R7. Prepare the entire batch before workload changes

Share preparation between reconciliation and existing batch session start/restart operations. Select
and validate the batch, determine eligible actions, and gather all required workload and
console-pane secrets before the first session or console runtime change. Deduplicate secret
requirements across targets and deliver only the values each consumer is entitled to receive. Keep
values within the current invocation; never persist them in desired state, a plan, output, logs, or
a cross-command cache.

Preserve bail-before-prompt behavior: already-satisfied targets and refused actions must not request
runtime-only secrets. Finish required eligibility checks and confirmations before collecting secrets
for accepted workload actions. A failed required secret preparation must not leave earlier sessions
restarted while later sessions are still waiting for credentials.

Failure to prepare any required workload secret aborts workload execution for the entire selected
batch, even when an independent target's secrets are available. Infrastructure already activated for
preparation may remain running. R6's independent execution applies after successful shared secret
preparation; this boundary prevents a partially restarted batch caused by missing secrets.

Infrastructure preparation can precede workload preparation because guest checks need a reachable
VM. Collect known infrastructure credentials together where possible. Existing conditional Tailscale
repair credentials may still be acquired when startup proves repair is needed. Complete that
infrastructure interaction before announcing that the operation can proceed unattended and before
beginning any session or console runtime changes. Do not request speculative repair keys on healthy
paths. Reuse already-resolved shared values within the operation.

After the workload preparation boundary, execution must not prompt for further secrets. An
unexpected new requirement or invalidated plan must stop affected work with an actionable result,
not begin another interactive resolution between launches. Preserve non-interactive failure and
secret-redaction behavior.

### R8. Existing installations and permanent documentation

Provide an explicit adoption path for existing sessions and consoles whose intended state cannot be
recovered from historical runtime records. Do not infer running intent from old PIDs or start
timestamps, or stopped intent merely from an absent runtime after an outage. Preserve existing VM
intentional-stop and on-demand behavior during migration. Inspection must make resources awaiting
adoption visible; fleet reconciliation must report that adoption is required instead of silently
changing or forgetting them. Explicit start/stop can establish intent for an existing resource.

Document adoption, the distinction between external exit and explicit stop, VM and child precedence,
console independence, preview limitations, batch prompting phases, and partial-run recovery in
permanent product documentation. Keep command reference, help, completion, and retained guide
content synchronized with the implementation. Current behavior must be understandable without
reading this SDD.

### R9. Delivery and validation

Deliver requirements, the remaining design artifacts, the entire implementation, validation, and the
final SDD lock in one PR. Use a draft PR with `review-requested` for the FRD checkpoint and again
for the complete design checkpoint. Publish the completed implementation through the normal ready
handoff. Each of those three stages has an operator-authorized allowance of up to two feedback
rounds, conditioned on convergence before the next stage. Follow standard private review,
published-feedback evaluation, monitoring, and handoff discipline. Lock only after the work and its
required validation and reviews have actually converged. No merge is authorized by this brief.

Demonstrate behavior across all three runnable kinds, intentional and external shutdown, mixed
desired states, unknown and residual observations, concurrent lifecycle changes, partial failure,
adoption, dry run, repeated reconciliation, shared secrets, cancellation, and non-interactive use.
Exercise real CLI recovery against a live VM with sessions and named consoles. Report actual
workstation and platform coverage and any unavailable lanes without implying they passed.

## Acceptance criteria

1. One VM-scoped command restores desired-running sessions and named consoles after a VM restart.
2. Running targets retain their runtime identities across repeated reconciliation.
3. Explicitly stopped sessions and consoles stay stopped; runtime exit alone does not clear intent.
4. An explicitly stopped VM stays off with child intent retained and desired-running children
   reported as blocked; explicit VM start allows later recovery.
5. An idle on-demand VM stays off unless a desired-running child requires it.
6. Console recovery restores its definition without taking ownership of member session lifecycles.
7. Unknown or unsafe runtime state causes a visible blocker without destructive recovery.
8. Dry run performs no activation, repair, workload-secret resolution, or mutation.
9. Batch workloads share secret preparation and perform no secret prompting after workload changes
   begin. A required secret failure occurs before any selected workload runtime changes.
10. Independent targets make progress after another VM fails, and incomplete convergence exits
    nonzero with per-resource results.
11. Existing ambiguous intent requires explicit adoption and remains visible until adopted.
12. The same PR contains current permanent documentation, green required gates, live evidence,
    completed plan records, and the final lock after authorized review convergence.

## Non-goals

- A background reconciliation service, scheduler, continuous restart loop, or restart backoff
  policy.
- Terminal jobs, successful-completion detection, or automatic retirement of desired-running intent.
- Full declarative provisioning or initialization convergence.
- A generic runnable lifecycle manager or common observed-status taxonomy.
- Implicit force, fresh-conversation restart, or guaranteed harness conversation recovery.
- Changing VM platform power contracts, shutdown versus deallocation policy, or transport design.
- Automatically starting stopped console members or rewriting child intent during VM shutdown.

## Operator rulings

### 2026-11-09: scope and delivery

The operator accepted desired runtime state and explicit reconciliation for VMs, sessions, and named
consoles, including shared batch secret preparation. The operator clarified that runtime exit is not
terminal and authorized one PR with FRD, design, implementation, and lock checkpoints under the
review budgets in R9. `Reconcile` is the selected operation name.
