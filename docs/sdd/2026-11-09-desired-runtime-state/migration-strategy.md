# Migration strategy

## Starting snapshot

Design baseline: main `438c4546c`, inspected on 2026-11-10. The converged FRD is at `c6337609d`. The
latest database migration is 38. There are three runnable tables, one stored VM intent flag, and no
stored session or console intent. Sessions already have a durable UUID; VMs and consoles do not.
Runtime observations and start timestamps exist for all three kinds.

The old `operator_stopped` flag has two meanings: true forbids automatic VM power-on; false permits
on-demand activation. It does not mean that the operator requested an idle VM to stay running.
Session `PID_STOPPED`, boot IDs, start times, and console presence cannot reconstruct desired state.

## One schema cutover

Ship callable migration 39, enum/row/converter updates, all intent readers/writers, and lifecycle
changes together. There is no period with dual stored VM intent, a compatibility setter, or a new
controller reading an old boolean. Historical migration definitions retain their old vocabulary.

| Existing record    | Migrated intent         | Other changes                                                       |
| ------------------ | ----------------------- | ------------------------------------------------------------------- |
| VM, old flag true  | Stopped                 | Assign durable VM UUID; preserve all other facts                    |
| VM, old flag false | On-demand               | Assign durable VM UUID; do not infer running                        |
| Session            | Null, adoption required | Preserve existing session UUID, runtime evidence, and harness state |
| Console            | Null, adoption required | Assign durable console UUID; preserve definition and members        |

The [state LLD](runtime-state-lld.md) defines transaction, constraint, sentinel, and identity
requirements. Migration performs no guest or provider operation and resolves no secrets. Existing
safe-open backup behavior protects the old database before mutation. A failed version rolls back and
does not advance its checkpoint.

An older binary cannot safely interpret the migrated schema. Retain the existing forward-version
refusal; do not synthesize the old flag or downgrade in place. Rollback uses a full pre-migration
database backup with a compatible binary, rather than trying to reverse external runtime changes.

## Operator adoption

After upgrade, inventory shows session and console targets awaiting adoption. Reconciliation reports
those rows as blocked instead of guessing from current liveness. Operators record their known intent
without runtime work, inspect the preview, then execute one prepared recovery:

```console
agw session set-desired-state development running
agw session set-desired-state retired-work stopped
agw console set-desired-state daily running
agw vm reconcile workstation --dry-run
agw vm reconcile workstation
```

An explicit start/stop also adopts its accepted target, including an ordinary already-running
batch-start match. Pure setters are useful when workloads are currently absent or when the operator
wants to review the complete recovery before execution. Do not add an automatic capture command that
turns whichever processes survived an outage into desired state.

Migrated VMs retain their prior on-demand/manual-stop policy. An explicit VM start now records
running intent; use `vm set-desired-state NAME on-demand` to return to the legacy permission-only
policy. A stopped VM continues to preserve child targets while blocking their activation.

## Worked recovery

Before upgrade, VM `workstation` permits on-demand activation, two sessions exist, and console
`daily` references them. A host restart has removed all three guest runtimes. The old session PIDs
and timestamps survive, but do not prove which workloads the operator intended to run.

Migration assigns the VM on-demand and leaves the two session targets and console target absent. The
first preview identifies adoption as required and performs no recovery. The operator records running
for one session and the console, stopped for the other session, and optionally running for the VM
when direct SSH/IDE access should be restored independently of managed workloads.

Execution activates the VM if required, completes connection repair, checks the selected workload,
and resolves its and the console panes' secrets together. It starts the desired-running session,
leaves the stopped session absent, then builds the console with its normal waiting view for that
member. A repeat run preserves healthy runtime identities. A later explicit VM stop changes only the
VM target and power state; both child targets remain available for future recovery.

## Existing command changes

- Explicit VM create/start establishes running intent instead of merely allowing future activation.
- Explicit session/console lifecycle maintains targets. Runtime cleanup and exit do not clear them.
- Ordinary batch session start adopts accepted running matches, even when no runtime launch is
  necessary. Refused policy conflicts remain unaccepted; accepted execution failures retain intent.
- Batch session start/restart resolves every eligible workload secret before changing any selected
  runtime. A missing required value now prevents the whole deferred workload batch instead of
  leaving earlier successful restarts and continuing one session at a time.
- All deferred actions, including VM stops during reconciliation, wait for this shared boundary.
  Required VM activation/repair may already have completed and is not rolled back on failure.
- Stopping an already-off child no longer needs to activate its VM solely for a stop operation.

Document these changes in the permanent guide, command reference, CLI help, and retained management
teaching. The implementing commit carries a conventional breaking-change footer for the VM-start and
batch-secret failure changes. Do not rely on editing authored changelog prose as the release
mechanism.

## Compatibility and collateral

Operational JSON v1 gains `desired_runtime_state`; old producers may omit it. Preserve the existing
VM `operator_stopped` output as a derived compatibility value. Remove its old storage, row field,
writer, and decision paths. Human manual/idle wording must remain truthful alongside the target; it
must not imply that every non-stopped VM has on-demand intent.

No manifest resource schema or sample setting is added: targets are per-instance lifecycle intent,
not reusable template settings. Completion learns new verbs and their resource-name/state operands
through its existing command-tree and dynamic-name tables. Names-only completion remains local.

Update full database restore sentinels and row-backed backup serialization for the new fields.
Validate all historical completed schemas as well as the new one. A full database backup preserves
consoles and their target; do not broaden claims about any existing VM export that omits them.

## Risks and safeguards

| Risk                                                     | Safeguard and evidence                                                                            |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Guessing old intent after an outage                      | Null workload targets; explicit adoption tests and guide                                          |
| Losing VM metadata or console members during rebuild     | Populated migration fixtures, exact constraints/FK checks, rollback/retry and restore tests       |
| Reusing a deleted name during confirmation               | Durable UUIDs plus guarded reload and conditional target writes                                   |
| New guard acquisition rejects existing nested calls      | Explicit held-token threading through every existing guarded activation path                      |
| One missing credential suppresses unrelated VMs          | Independent per-name infrastructure preparation with cached outcomes                              |
| Prompts appear after runtime changes begin               | One eligible workload union; execution and final observation receive cached values only           |
| Secret preparation failure unexpectedly stops another VM | Every VM stop is deferred until the shared boundary succeeds                                      |
| Upgrade claims live recovery without exercising it       | Real CLI reboot/exit recovery evidence required before lock; report host/platform gaps explicitly |

The only delivery vehicle is PR #885. Mutable implementation commits may be partial privately, but
the feature is published ready only when the schema, all callers, commands, collateral, and gates
form one complete working change. Final lock follows validation and authorized review convergence.
