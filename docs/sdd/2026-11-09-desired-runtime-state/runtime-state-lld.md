# Runtime intent, migration, and concurrency

This implements FRD R1, R2, R5, and R8 within the [architecture](hla.md).

## Data model

Define `DesiredRuntimeState(StrEnum)` beside the existing database row enums. Its values are
`running`, `stopped`, and `on-demand`. Add these fields:

| Row     | Field                   | Stored contract                            |
| ------- | ----------------------- | ------------------------------------------ |
| VM      | `desired_runtime_state` | Non-null; any of the three values          |
| Session | `desired_runtime_state` | Running, stopped, or null pending adoption |
| Console | `desired_runtime_state` | Running, stopped, or null pending adoption |
| VM      | `vm_uuid`               | Canonical UUID, unique and non-null        |
| Console | `console_uuid`          | Canonical UUID, unique and non-null        |

Keep `SessionRow.session_uuid` and `run_id` unchanged. A resource UUID survives start, stop,
restart, target changes, and reinitialization. Deleting and recreating the same display name gives
it a new UUID. Runtime fingerprints and harness conversation identities are different values.

Use the existing row converters as the persisted-data boundary. Reject unknown intent values,
invalid kind/value pairs, missing VM intent, and malformed UUIDs with typed state errors that do not
echo raw persisted values. Do not convert unsupported intent into stopped, running, or adoption.
Null is allowed only for legacy session and console intent. Enum values serialize as ordinary
strings in existing row-backed backup exports.

Fresh insert methods record running unless their caller explicitly supplies an allowed target.
First-party create services always supply running for their initial runtime. Row construction and
database insertion generate UUIDs once; reloading a row never invents a new one. Tests representing
one resource across multiple reads must retain its identity rather than construct a replacement.

## Migration 39

Add a callable migration after version 38. Leave historical migrations unchanged.

1. Open `BEGIN IMMEDIATE` inside the callable so validation, data conversion, table reconstruction,
   foreign-key verification, and the runner's version checkpoint share one transaction.
2. Validate the legacy VM flag values before changing tables. Refuse malformed values rather than
   silently guessing an intent.
3. Rebuild the VM table with its current complete shape, replacing `operator_stopped` with
   `desired_runtime_state` and adding `vm_uuid`. Copy all existing metadata, initialization facts,
   release evidence, timestamps, site references, and platform metadata without reinterpretation.
   Map true to stopped and false to on-demand. Mint one UUID per existing VM.
4. Add nullable session intent with a check restricting non-null values to running/stopped. Preserve
   session UUIDs, run IDs, socket/agent constraints, fingerprints, and opaque harness state.
5. Rebuild the console table with its current complete shape, adding nullable intent and a durable
   console UUID. Preserve member relationships, shell entries, ordering, flags, and timestamps.
6. Let the existing runner perform `foreign_key_check`, write version 39, and commit. Failure must
   roll back the whole version and leave it retryable. Do not commit inside the callable.

SQLite table rebuilds use the existing create/copy/drop/rename pattern with foreign keys disabled by
the migration runner, not an old-table rename that redirects dependent references. Preserve all
first-party indexes and constraints. Update schema-sentinel additions, removed columns, and any
affected foreign-key sentinels in the same change. Restore must recognize the exact version-39
shape; merely increasing `LATEST_VERSION` is insufficient.

The existing CLI safe-open backup and migration locking remain in charge. No ad hoc backup format,
config migration, automatic legacy adoption, or migration-time provider access is introduced.

## Intent services

Expose resource-owned manager functions behind `set-desired-state`. They parse public input,
validate the kind's allowed values and local relationships, select the existing UUID, acquire the
VM-family guard, reload that exact resource, and write the target through a named database method.
No registry or secret resolver is needed for a pure target change.

Add `set_vm_desired_runtime_state`, `set_session_desired_runtime_state`, and
`set_console_desired_runtime_state` to `Database`. Require the expected UUID on updates. Use a
conditional write against name and UUID and raise a typed state error on replacement. These methods
participate in an enclosing database transaction and otherwise commit normally. They do not acquire
native locks themselves; the public manager boundary owns native-operation coordination.

An unchanged target is a no-op, preserving timestamps. A session/console target change updates its
existing modification timestamp without changing runtime evidence or start timestamps. Target
changes do not publish an initialization checkpoint or alter instance overlays.

Public lifecycle commands use the same target-write path while sharing their already-held guard.
Keep acceptance separate from execution:

| Request                      | Before acceptance                                 | Accepted target                                            | Execution failure           |
| ---------------------------- | ------------------------------------------------- | ---------------------------------------------------------- | --------------------------- |
| Explicit VM start            | Local identity/config checks                      | Running                                                    | Retain running              |
| Explicit VM stop             | Local identity/config checks                      | Stopped                                                    | Retain stopped              |
| Session/console start        | Local validation; known read-only policy refusals | Running, even for a healthy no-op                          | Retain running              |
| Session/console restart      | Existing confirmation bound to selected identity  | Running                                                    | Retain running              |
| Session/console stop         | Local validation and applicable confirmation      | Stopped, even if already absent                            | Retain stopped              |
| Ordinary session batch start | Validate every match and classify known refusals  | Running for every accepted match, including running no-ops | Retain each accepted target |
| Pure target setter           | Local identity/value validation                   | Requested allowed value                                    | No runtime execution        |
| Reconciliation               | No target acceptance or rewrite                   | Existing target                                            | Preserve it                 |

A read-only policy conflict established before acceptance leaves that target unchanged. A guest or
policy failure discovered only after acceptance retains the accepted target and reports failure. Do
not erase a target because an authentication prompt is later cancelled. Existing creation cleanup
still owns whether a failed new definition survives.

An accepted child start under a stopped VM records running, performs no activation, and exits
nonzero. A public stop below an already-off VM records stopped without starting the VM. Reconcile
similarly treats an off VM as sufficient to avoid pointless child-stop work, while reporting that
the guest was not inspected.

## Runtime-only helpers

Split acceptance from existing domain operations rather than passing a hidden flag to a public
start/stop service. Add or extract internal VM runtime start/stop helpers, prepared session launch
and stop helpers, and prepared console realization/stop helpers. They consume a retained row and
prepared context; they never record desired state.

In particular, `_mark_stopped`, boot/PID repair, failed-launch bookkeeping, restart teardown,
console staging cleanup, provider shutdown, and harness-state persistence remain evidence-only
operations. They must not call a target setter. VM dependency activation preserves on-demand.

The ordinary VM start runtime path retains conditional Tailscale reconnection/rejoin behavior even
when the provider already reports running. Reconciliation must not assume that a powered VM is
reachable merely because power-on is unnecessary. Do not route runtime recovery through reinit.

## VM-family guards and identity checks

Reuse `native_mutation_guard` from `harness_setup.locking`. Its non-blocking per-database/per-VM
token already supports explicit nested reuse and Windows/POSIX locking. Add no second lock family,
global database write transaction spanning network calls, or background worker.

All first-party intent writers, VM start/stop, session lifecycle mutation, console lifecycle and
membership mutation, and reconciliation share the family guard. Reconciliation acquires guards in
sorted VM-name order and holds each successful family through preparation, execution, and final
observation. A busy family is blocked; it does not prevent other families from preparing.

Read-only confirmation can happen before the guard, but the selected UUID is retained and checked
after acquisition. Reload the resource and verify its parent family and current target before every
consequential runtime operation. A missing/replaced row refuses; never fall back to a previously
captured VM row after its deletion. Prepared operations cannot substitute a new same-name owner.

Native guards coordinate Agentworks mutations. External provider events can still occur; observe
before acting and verify afterward. Arbitrary direct SQLite or provider writes do not become
transactional merely because an Agentworks guard is held.

### Activation's existing callers

Close the existing read-intent-to-provider-start race in `LiveVMNode.auto_start` using the same
family guard and a fresh identity/intent read inside it. A node/factory may receive an explicitly
held `NativeMutationGuard`; gated VM boundary helpers forward that optional token. Recheck provider
state under the guard before starting, so another completed activation is not duplicated.

Current guarded callers must pass their token, not reacquire the same lock through a separate
handle. The exact affected compositions at the design baseline are:

- Agent delete's standalone gated boundary and agent reinit's activation gate.
- Workspace delete's standalone boundary and workspace rehome's nested VM helper.
- Batch session restart's shared VM gates and nested singular launch gates.

Unchanged read-only access commands may briefly acquire a guard for actual auto-start, then release
it before the interactive body. Do not hold a new mutation lock for the lifetime of `vm shell`,
agent shell, port forwarding, or attach. Mutating callers that already guard their whole operation
continue to reuse the same token. A stale or wrong-family token remains an error.

Stopped intent prohibits automatic power-on. It does not make diagnostic access to an already-live
guest a new authorization system. Managed child start/reconciliation enforces stopped-parent
precedence explicitly; ordinary access and cleanup retain existing domain behavior on a guest that
is already running.

## Inspection and compatibility

Add target fields to the existing VM/session/console safe fact records and JSON projections. Human
plain inventory shows targets without live work; a null workload target displays adoption required.
Describe and `list --status` show desired and observed values separately. Names-only output stays
one name per line and does not inspect or repair anything.

Preserve JSON v1's existing VM `operator_stopped` field solely as
`desired_runtime_state == stopped`. Retain legacy status-disposition projections from the new
target, without keeping a parallel boolean field, setter, model property, or decision path. Publish
the additive `desired_runtime_state` field and document compatibility with older v1 producers that
omit it. UUIDs need not become new operator addressing or JSON fields for this feature.

Full database backup/restore must preserve all three kinds' intent and UUIDs. Existing VM backup row
exports must serialize their new fields correctly. Do not add a new restore mechanism or silently
claim that a pre-existing export covers kinds it did not previously include.

## Verification boundaries

- Migrate populated version-38 databases, older valid histories, and empty installs; verify mapping,
  preserved relationships/metadata/UUIDs, schema constraints, restore sentinels, rollback, and
  retry.
- Reject wrong-kind intent and invalid persisted enum/UUID values without provider or secret work.
- Exercise UUID replacement between selection and guard, concurrent intent changes, same-family
  contention, nested reuse, and crash release. Mark host-sensitive tests for Windows CI.
- Show accepted intent surviving provider/secret failure and cancellation, while local refusals and
  declined restart confirmation leave it unchanged.
- Prove evidence-only teardown/recovery never changes target, and parent stop preserves all child
  targets. Repeated reconciliation preserves healthy runtime identities and start observations.
- Drive list, describe, names-only, additive JSON, completion, and backup boundaries without prose
  assertions or snapshots of authored messages.
