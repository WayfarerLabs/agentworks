# Desired runtime state: architecture

The [requirements](frd.md) govern this design. The FRD checkpoint converged at `c6337609d` after two
authorized feedback rounds. The [plan](plan.md) records delivery and validation. This is one feature
in PR #885, not a sequence of independently merged partial implementations.

## Stored intent and resource ownership

Add a `desired_runtime_state` column to each runnable's existing table and a matching row field. Use
one string-compatible `DesiredRuntimeState` enum in `db.models`: `running`, `stopped`, and
`on-demand`. Database constraints and service validation restrict sessions and consoles to the first
two values. A null session or console target means the operator has not adopted that legacy
resource; it is not another runtime state.

VM intent replaces `operator_stopped` completely. Migration 39 maps the old true value to stopped
and false to on-demand, then removes the old column. New resources created with their initial
runtime record running intent. No target belongs in a template, instance overlay, applied-state
slice, or secret store. Initialization evidence and observed runtime fingerprints remain separate.

Give VM rows durable UUIDs, following the existing session UUID pattern. These bind nodes captured
before guard acquisition to their selected VM. Console selection instead occurs under the family
guard, including delete confirmation; it needs no new UUID or table rebuild. Graph keys and CLI
operands remain kind/name. Details are in [state and concurrency](runtime-state-lld.md).

Keep lifecycle ownership in the current VM, session, and console managers. There is no `Runnable`
base class, adapter registry, generic lifecycle manager, provider API change, or background
controller. A small `runtime_state` package owns explicit reconciliation coordination and result
records. Domain preparation and execution stay with their managers. The existing Resolver owns
invocation secrets.

## CLI contract

| Command                                    | Scope and behavior                                           |
| ------------------------------------------ | ------------------------------------------------------------ |
| `agw vm set-desired-state NAME STATE`      | Record running, stopped, or on-demand without runtime work   |
| `agw session set-desired-state NAME STATE` | Record running or stopped without runtime work               |
| `agw console set-desired-state NAME STATE` | Record running or stopped without runtime work               |
| `agw vm reconcile NAME [--dry-run]`        | Apply the VM's target and its stored session/console targets |
| `agw vm reconcile --all [--dry-run]`       | Apply all managed VM scopes                                  |
| `agw session reconcile NAME [--dry-run]`   | Apply one session's target and only necessary VM activation  |
| `agw console reconcile NAME [--dry-run]`   | Apply one console's target and only necessary VM activation  |

The explicit setter name distinguishes recording intent from changing a runtime. It is the adoption
path and the way back to on-demand VM policy. Do not add a duplicate create option, global default,
capture-current-state operation, or a second adoption model. Setters do not load secret sources,
observe guests, initialize resources, or change runtimes.

Managers validate operand versus `--all` selection before work; neither form silently defaults to
the fleet. CLI bodies only forward arguments and the existing interaction policy. New verbs use
existing completion mechanisms and static state choices. Reconciliation produces human results and
typed service records; this effort does not introduce a new operational JSON command family.
Existing list/describe JSON receives additive intent fields.

## Accepting intent and applying it

Public lifecycle requests first validate local input and known operation-policy conflicts and obtain
existing confirmations. They then record accepted targets under the VM-family guard before runtime
execution. Ordinary batch start records running for accepted running no-ops too. A child start under
a stopped parent records running, reports blocked, and exits nonzero.

Creation stores intent with its new row and retains current domain cleanup on failure. Runtime
evidence helpers such as `_mark_stopped` must not write intent: restart and recovery call them too.
Public explicit stop writes stopped; internal teardown, provider shutdown, harness exit, and PID
repair do not. Deletion removes the row under existing cleanup policy.

Extract domain preparation and execution helpers from the existing lifecycle bodies. Public commands
combine acceptance with those helpers. Reconciliation uses the helpers without calling public
start/stop services that would rewrite intent. Dependency activation preserves on-demand.

## Reconciliation phases

Execution constructs an in-memory plan from current rows and applies it once. It does not save an
executable plan or retry automatically. Dry run follows the separate observation path below and does
not acquire or create mutation guards.

1. Validate scope and retain VM/session identities. A preliminary focused-console lookup locates its
   parent guard only. Acquire VM-family guards in deterministic order, retaining successful families
   and reporting busy ones as blocked. Reload and validate VM/session identities; select consoles
   and their definitions under the guards, refusing a changed parent on focused lookup. Structural
   database-open or registry failures remain command errors; per-VM preparation failures are
   isolated.
2. Build observation/provider contexts for every affected VM, including parents needed only to
   inspect or stop children. Activation is a separate decision: activate every selected VM with
   running intent, regardless of child count or child targets, plus permitted parents needed by
   selected running workload candidates. A stopped parent blocks those running candidates; an
   on-demand parent used only for child stop does not receive an activation request.
3. Resolve distinct required infrastructure credentials, observe power state, activate required VMs,
   complete conditional Tailscale repair, and retain their active holds. A failed VM blocks its own
   dependent operations while other families continue. VM stops are planned here but deferred.
4. Prepare eligible session and console operations using their own identities, templates, targets,
   and transports. Existing status, policy, command rendering, command-presence, and setup checks
   run before workload secret collection. Healthy running targets and refused operations add no
   workload-only secrets. A console may still build a waiting view for an absent member session.
5. Register eligible graph declarations and effective environment/pane targets on the same Resolver
   used for infrastructure gates. Its boundary checks required names against memoized gate failures
   before any source call. Otherwise it reuses seeded successes, resolves the remaining union once,
   and validates every target's use of those values. Failure prevents every remaining runtime
   change, including every VM stop. Already-activated infrastructure may remain running and is
   reported.
6. Execute concrete domain operations with supplied values: session actions before console builds
   within each VM, then deferred provider stops. Do not individually tear down children merely to
   stop their VM. A failed action does not prevent independent actions. Abort stops further work.
7. Perform bounded final observations through the existing status authorities, report all selected
   targets and completed prerequisite changes, and return nonzero for failed or blocked targets.
   Clear invocation-owned values and release holds and guards on every exit.

The uniform rule in phase 2 settles the final FRD review's optional wording point. A running-intent
VM with all children stopped is activated in the same infrastructure phase as a childless one. A
failure preparing an unrelated workload secret does not undo either successful activation.

Console membership supplies ordering and view content, not a hard dependency on a running member. A
VM failure blocks its console; a session launch failure need not block a valid console's waiting
view. Missing workspace or agent definitions remain the console builder's structural errors.

## Readiness and secret composition

The current `RunContext` has one admin target and one agent target. Session readiness checks require
that session's `SESSION` scope; a `SYSTEM` context silently skips command checks. Retain a prepared
graph and correct context per session operation, run its readiness checks, then union its secret
requirements. Do not combine multiple sessions' graphs and walk them with the wrong guest context.
Shared VM/site construction may be memoized; no capability API or target-routing registry is needed.

Infrastructure preparation and workload preparation are distinct compositions over one existing
Resolver. Extend `resolve_gate`, which already resolves and seeds successful singleton requests,
with value-free failure memoization. Register a new gate name only after success. This isolates a
failed site's credential without exposing partial values or retrying a shared failure. Known
requests are front-loaded; conditional repair joins only when needed. Cancellation is never cached.

Register eligible workload declarations and `SecretTarget`s afterward. Inside `resolve()`, reject
required names with memoized gate failures before any source call; ignore failures outside that
union. Existing seeded successes are reused and remaining workload names resolve in one complete
batch. No separate cache, name-query API, seed transfer, or partial-resolution API is needed. Do not
pre-register every infrastructure graph's names, which would poison unrelated workloads.

Boundary failure terminates the invocation and disposes its Resolver; no catch-and-continue path can
reopen gate resolution. Successful resolution closes gates through the existing boundary rule.
Execution and final observation receive supplied values without a resolution callback. Newly
discovered requirements fail and require another invocation. Values never enter a serialized plan,
stored intent, result record, or log. Detailed seams and failure cases are in
[batch preparation](batch-preparation-lld.md).

Existing batch session start, restart, and stop use the same domain preparation. Stop observes
parents without activation and contributes no workload env targets. Retire the old batch VM boundary
and its late-repair path once these callers move. Preparation failures remain local to their VM
families; the complete workload-secret failure remains an operation-wide barrier.

## Observation and dry run

Keep each domain's status authority. Unknown observations block action; they do not authorize
replacement. Residual session/server and console staging cleanup use existing safe recovery during
execution, after the shared secret barrier. Broken session recovery requiring force stays explicit.

Dry run selects and validates local rows, observes available runtime state, and renders proposed or
conditional actions. It never opens activation gates, repairs PID evidence, removes staging state,
loads workload secret values, or invokes source preview for workload secrets. Provider status may
resolve its existing observation credentials. A powered-off VM has no inspectable guest; report the
conditional checks needed after activation instead of treating an unattempted probe as an unknown
runtime that can be replaced. No saved dry-run output is accepted as execution authority.

Final execution observations use prepared provider contexts and direct guest observers, so they
cannot introduce new secret prompts. An inconclusive observation blocks convergence; a target absent
again after its start is failed. Successful observation is not an uptime, useful-work, or
continued-availability guarantee. Guest probes retain their finite existing attempt/time limits.
Provider checks retain documented backend limits, including AWS/Azure SDK defaults that can exceed
ten seconds; no uniform ten-second provider deadline or unbounded convergence polling is added.

## Inspection and compatibility

Human list and describe views show the stored target, including pending adoption, independently of
requested live status. Names-only completion remains unchanged. Operational JSON v1 gains
`desired_runtime_state` on applicable records, with null for pending session/console adoption.
Retain `vm.operator_stopped` only as a derived compatibility projection of stopped intent, never as
storage or a decision input. Current status enums and existing field meanings remain intact.

Permanent documentation lives in `docs/guides/desired-runtime-state.md`, the command reference,
runnable-status guidance, and the orchestration README. No permanent artifact links to this SDD. The
[migration strategy](migration-strategy.md) defines the one-cutover schema and caller migration. The
[research](prior-art-research.md) explains terminology and mechanisms deliberately not adopted.

## Current-main parallel stop integration

The supplemental integration retains the prepared controller as lifecycle composition and main's
session teardown module as native teardown and conditional persistence authority. An internal
`stop-all` operation distinguishes public bulk stop from named stop even when a filter selects one
row. It selects stopped intent; no public command or desired-state value is added.

Only public bulk stop uses the existing bounded concurrent dedicated teardown engine. It preserves
main's admin route, strict actionable-unknown refusal, collision/submission gates, caller-thread
persistence/output and serial compatibility work afterward. Named stop, start/restart and
reconciliation retain synchronous ordering; reconciliation retains independent-family failure
handling. Stop-only work neither activates an off parent nor opens a hold that could boot it.

Explicit named stop also retains raw selected rows through shared secret success. Safe caller-thread
admin PID repair precedes any status dispatch, followed by the named stop's owning-user preparation
and synchronous teardown. Repair conflicts and database writer failures escape before status or
destructive dispatch. Off-parent stops skip repair, guest status and transport construction.
Reconciliation keeps its read-only preparation before workload resolution.

An off parent establishes current inactivity, not native process death: suspension may preserve
guest memory. Guest-free stop revalidates the original session snapshot and retains its last-known
runtime fingerprint. It performs no runtime-state write and reports runtime work unchanged; accepted
intent can independently count as changed. Verified native absence or teardown remains the authority
for stopped-state CAS. This uses existing states and fields, preserving safe ownership on a later
resume without changing VM power policy.

Destructive work follows complete workload preparation. Guards and the writable database remain
owned through interruption draining and reconciliation. One narrow caller-thread outcome observer
records success after conditional stopped persistence and records established failures before their
future leaves pending. Receipts survive interrupted notification, so neither successful work nor a
known failure is replaced by cancellation. Notification retries repeat neither native work nor a
completed persistence attempt; interrupted persistence still uses the existing safe retry.
Cancellation, including `UserAbort`, remains control flow and must not become a persistence warning
or permission to run subsequent serial work.

If the failure observer itself cannot deliver an established failure, the shared reporting boundary
is fatal: retain the first primary error, cancel queued work, drain active work and propagate it.
Ordinary native failures with a functioning observer remain per-session outcomes.

### Observation provenance while a parent is inactive

Child results keep one invocation-local `observation_from_parent` fact alongside the observation. It
identifies a status inferred from provider inactivity, rather than guest inspection for that
observation. The renderer consumes it separately from the proposed/completed action and failure
reason. This preserves conditional actions and authoritative errors instead of overloading either
field with reporting provenance. Native VM power observations themselves need no qualification.

Pair provenance with its observation: metadata-only updates preserve both, a fresh guest or unknown
observation clears earlier parent-derived provenance, and an OFF-derived projection explicitly sets
it. Preparation, completion, final observations, previews and the guest-free error fallback follow
the same rule for sessions and named consoles. Public named Console stop reports the equivalent fact
at its existing lifecycle boundary, based on the final prepared path after the power recheck.

The disclosure qualifies this observation, not all work in the invocation. Earlier guest work or
verified teardown may have occurred before a final OFF projection. Inactivity alone does not clear
recorded native evidence, but the result must not promise that an original complete fingerprint
survived earlier legitimate teardown. Existing statuses, convergence decisions, target acceptance,
power behavior, database schema and native teardown remain unchanged.
