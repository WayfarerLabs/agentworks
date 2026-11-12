# Desired runtime state and recovery

Agentworks remembers whether each VM, session, and named console should run. This target is
independent of observed power or tmux state. A host restart, external shutdown, or normal session
exit leaves the target unchanged. Reconciliation applies that recorded intent when explicitly
requested; there is no background controller or completed-job state.

## Choose a target

| Resource           | Target      | Meaning during reconciliation                                                               |
| ------------------ | ----------- | ------------------------------------------------------------------------------------------- |
| VM                 | `running`   | Restore the VM even when it has no running workloads                                        |
| VM                 | `on-demand` | Permit activation when a selected running workload needs it; otherwise leave an idle VM off |
| VM                 | `stopped`   | Keep the VM off and block running children                                                  |
| Session or console | `running`   | Restore its ordinary runtime when absent                                                    |
| Session or console | `stopped`   | Stop its runtime while keeping its definition                                               |

Legacy sessions and consoles may have no target yet. They require explicit adoption; neither their
old process records nor their current absence determines what the operator intended.

Targets belong to individual resources in the state database. They are not template, instance-spec,
or configuration settings. To record intent without starting, stopping, observing, or resolving
secrets, use the owning resource's setter:

```console
agw vm set-desired-state workstation on-demand
agw session set-desired-state development running
agw session set-desired-state retired-work stopped
agw console set-desired-state daily running
```

Plain `vm list`, `session list`, and `console list` show stored targets using local inventory.
Describe and `list --status` show live observations separately. `--names-only` remains a local
one-name-per-line stream. See [Runnable status inspection](runnable-status.md).

## Adopt, preview, then recover

After an upgrade or outage, inspect the local inventory and select targets from your known intent.
Do not adopt everything as running merely because it once ran. The setters above are useful even
when the resources are currently absent. Explicit start or stop also adopts the accepted target.

Preview one VM and its stored sessions and consoles, then apply the reviewed recovery:

```console
agw vm reconcile workstation --dry-run
agw vm reconcile workstation
```

A running-intent VM is activated even when every child is stopped or awaiting adoption. An on-demand
VM is activated only when selected running workloads require it. A stopped-intent VM blocks running
children, retains their targets, and is never activated for them. To bring that VM back and then
restore its children, use two explicit operations:

```console
agw vm start workstation
agw vm reconcile workstation
```

VM start establishes running intent and prepares the VM's connection. It does not restore session or
console runtimes automatically.

Fleet recovery requires explicit selection. Use it only when recovery of the entire managed fleet is
intended:

```console
agw vm reconcile --all --dry-run
agw vm reconcile --all
```

An omitted VM name does not select the fleet. A VM name and `--all` are alternatives. There are no
saved executable preview plans; execution selects and validates current resources again.

For a focused retry, apply just the named workload's existing target:

```console
agw session reconcile development --dry-run
agw session reconcile development
agw console reconcile daily
```

A focused operation may activate its permitted parent VM, but does not change unrelated workloads.
Console reconciliation restores only that console view, including its stored member order, shell
panes, and optional admin shell. It does not start member sessions. Membership does not own session
targets: stopping a console leaves sessions alone, and a console may show a waiting view for an
absent member. VM-scoped recovery processes sessions before console builds.

## Lifecycle acceptance and safe recovery

Creation with an initial runtime, start, and restart record running intent. Explicit stop records
stopped intent, including when the runtime is already absent. Ordinary batch session start adopts
running intent for accepted already-running matches too; batch stop adopts accepted absent matches.
Dependency activation preserves an on-demand VM's target.

Local validation, known read-only policy refusals, and required confirmation precede acceptance. A
refused fresh-conversation start of a running session, for example, leaves its target unchanged.
After acceptance, credential failure, provider or guest failure, or cancellation retains the target
for a later retry. A valid child start under a stopped parent accepts running intent but reports
blocked with a nonzero exit and performs no activation. Failed creation keeps the domain's ordinary
cleanup policy: a definition removed by cleanup does not survive solely to retain its target.

Reconciliation never rewrites targets, creates missing definitions, reinitializes environments, or
replaces a healthy runtime to refresh its configuration. It preserves ordinary harness continuation
behavior without forcing a fresh conversation. Unknown runtime state blocks recovery; safe residual
cleanup follows existing domain rules, while broken state requiring explicit force remains blocked.
Stopping a VM uses its platform's ordinary stop policy and retains child targets. An already-off VM
needs no activation solely to stop its children; the result identifies that the guest was not
inspected.

While a parent is off, session stop and stopped-target reconciliation retain the last known runtime
fingerprint. Platforms can preserve guest memory during suspension, so the stored PID, boot and
process start time remain useful ownership evidence after resume. The current result reports the
session inactive without clearing that evidence. Once the VM is reachable, reconcile again to stop a
surviving runtime whose desired state is stopped. A real shutdown followed by a new boot uses the
existing absence and recovery checks.

Stop and reconciliation results qualify observations inferred from parent inactivity: the guest was
not inspected for that observation, and inactivity alone does not clear recorded runtime evidence.
This qualification also applies to previewed stopped targets. A fresh guest observation replaces
that qualification; earlier native teardown in the same operation can still have cleared evidence.

Named session stop stays synchronous. Bulk session stop runs complete-fingerprint dedicated runtimes
concurrently and handles incomplete or shared-server compatibility work serially afterward. An
actionable unknown runtime refuses destructive bulk teardown before any session is stopped; accepted
stopped intent remains available for retry. Reconciliation keeps independent-target recovery rather
than applying that public bulk-stop refusal to unrelated families.

## Secrets, failure, and cancellation

Reconciliation and batch session start/restart finish eligible workload checks and collect required
session and console-pane secrets together before changing any workload runtime. Healthy no-ops and
refused actions contribute no unused workload secrets. One required workload secret failure prevents
all deferred changes in that batch, including restarts, console staging cleanup, and VM stops.
Correct the missing input and retry the intended scope.

Infrastructure preparation happens first. Required VM activation and conditional Tailscale repair
may already have completed when workload preparation fails; those effects are not rolled back.
Repair credentials are requested only when needed, including when the provider reports a VM running
but its connection needs repair. An infrastructure credential failure blocks only the families that
need it, so independent families may continue. If an eligible workload also needs that failed
credential, the complete workload-secret barrier still fails. Execution and final observation do not
open another secret prompt between workload changes.

Cancellation stops further work and retains accepted targets and completed effects. It does not
promise rollback of provider or guest changes. Results distinguish unchanged, changed, failed, and
blocked resources; any selected failed or blocked target produces a nonzero exit. Inspect the result
before retrying. Bounded final observations describe this invocation, without guaranteeing continued
uptime, useful harness progress, or restored conversation state.

## What preview does

`--dry-run` does not write targets, activate or repair VMs, repair process evidence, create or
remove tmux staging, preview or resolve workload secrets, or create mutation-guard files. Provider
status may still need its ordinary observation credentials and their configured authentication
behavior. When the VM is off, the preview identifies guest checks as conditional on later
activation; it does not claim to have inspected an unreachable workload. Unknown attempted
observations remain blockers.

## Busy families and WSL2 access

Mutating operations serialize within a VM family. Reconciliation may keep a family busy while
collecting secrets; console deletion may keep it busy during confirmation. Wait for that operation
to finish and retry the intended command. Admission to a power hold is also guarded, so even
ordinary diagnostic access to an already-live VM can report busy. A newly acquired admission guard
is released before the interactive shell, attach, or forwarding body runs.

On WSL2, a live VM with stopped intent receives no idle-shutdown protection during access **or
management commands**. A long shell, workspace creation, or agent reinitialization may lose the
guest mid-command. If normal holds and future automatic activation are intended, explicitly select
on-demand first:

```console
agw vm set-desired-state workstation on-demand
```

Alternatively, select `agw vm set-desired-state workstation running` when recovery should also
restore an idle VM. Both policies permit activation as well as normal holds; selecting one is not
merely a keepalive toggle. Otherwise leave stopped intent in place and accept the lack of idle
protection for live diagnostics. Commands do not alter the target just to obtain a hold. WSL hold
entry waits for acknowledgement from its own guest keeper before allowing command work; that
acknowledgement does not prove Tailscale readiness or cancellation of every guest effect if entry
fails.

## Upgrade and rollback

Schema migration 39 replaces the old VM `operator_stopped` storage: true becomes `stopped`, and
false becomes `on-demand`. It does not infer always-running intent from historical power state.
Existing sessions and consoles receive null targets pending adoption, preserving their definitions,
membership, runtime evidence, and harness state. Newly created resources record running intent.

**Breaking lifecycle changes:**

- Explicit VM start now records running intent even when the VM is already running. Select
  `agw vm set-desired-state NAME on-demand` to return to permission-only activation.
- Batch session start/restart no longer resolves and launches one session at a time. A missing
  required workload secret prevents the complete deferred batch instead of leaving earlier sessions
  restarted. An infrastructure credential failure remains limited to dependent families.

The normal database-open path offers a pre-migration snapshot. An older binary refuses the newer
schema; rollback requires a full pre-migration database backup and a release that understands it,
not an in-place downgrade or reconstruction of the retired flag. A database restore does not undo
provider or guest changes made since the backup. Full database backups preserve console targets and
memberships; the VM metadata/file export has its existing narrower scope. See
[Database commands](../../cli/command-reference.md#database) for backup and restore details.

Operational JSON remains v1 and adds `desired_runtime_state` to runnable list and describe records.
VM targets are strings; legacy session and console targets may be null. Older v1 producers may omit
this additive field. The VM `operator_stopped` output remains a derived compatibility boolean, true
exactly when the target is stopped; it is not a separate policy. A stopped/deallocated VM has
`manual` disposition for stopped intent, `idle` for on-demand, and null for running intent. Running
and unknown observations also have null disposition.
