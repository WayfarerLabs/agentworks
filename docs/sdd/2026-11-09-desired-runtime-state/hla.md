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

Give VM and console rows durable UUIDs, following the existing session UUID pattern. These identify
the selected resource across confirmation and preparation, where a name may be deleted and reused.
Graph keys and CLI operands remain kind/name. UUIDs are execution checks, not new addressing syntax
or an observed-status framework. Details are in [state and concurrency](runtime-state-lld.md).

Keep lifecycle ownership in the current VM, session, and console managers. There is no `Runnable`
base class, adapter registry, generic lifecycle manager, provider change, or background controller.
A small `runtime_state` package owns explicit reconciliation coordination, result records, and the
operation's infrastructure secret cache. Domain preparation and execution stay with their managers.

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

Each invocation constructs an in-memory plan from current rows and executes it once. It does not
save an executable plan or retry automatically.

1. Validate scope and select resource identities. Acquire VM-family guards in deterministic order,
   retaining successful families and reporting busy ones as blocked. Reload and validate selected
   identities and targets under those guards. Structural database-open or registry failures before
   selection remain command errors; ordinary per-VM preparation failures remain isolated.
2. Build the required infrastructure operations and their readiness graphs. Prepare every selected
   VM with running intent, regardless of its child count or its children's targets. Also prepare
   permitted parent VMs needed by selected running workload candidates. A stopped parent blocks
   those candidates; an idle on-demand VM with no candidates is not activated.
3. Resolve distinct required infrastructure credentials, observe power state, activate required VMs,
   complete conditional Tailscale repair, and retain their active holds. A failed VM blocks its own
   dependent operations while other families continue. VM stops are planned here but deferred.
4. Prepare eligible session and console operations using their own identities, templates, targets,
   and transports. Existing status, policy, command rendering, command-presence, and setup checks
   run before workload secret collection. Healthy running targets and refused operations add no
   workload-only secrets. A console may still build a waiting view for an absent member session.
5. Register eligible graph declarations and effective environment/pane targets on one workload
   resolver, seed already-resolved infrastructure values, resolve once, and validate every target's
   use of those values. Failure prevents every remaining runtime change, including every VM stop.
   Already-activated infrastructure may remain running and is reported.
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

Infrastructure preparation and workload preparation are distinct compositions. Infrastructure
credentials can fail independently across VMs. Resolve each distinct required infrastructure secret
once through the existing complete-resolution API, caching both success and failure for this
invocation. This small per-name preparation avoids exposing partial values from a failed complete
batch or making one site's missing credential doom another site. Known requests are front-loaded;
conditional repair names join the same cache only when a VM proves they are needed.

The workload composition uses the existing `Resolver.register_targets`, declaration registration,
`seed`, `resolve`, and `ScopedSecrets`. It resolves the complete eligible workload union together,
so a missing value is an operation-wide barrier rather than a partial restart. It does not reuse the
old batch's completed VM resolver by registering new names after its boundary.

After phase 5, domain execution can read supplied values but cannot resolve more. The infrastructure
cache is closed to new resolution before workload execution. Any newly discovered requirement is a
failure requiring another invocation. Values never enter a serialized plan, stored intent, result
record, or log. Detailed seams and failure cases are in
[batch preparation](batch-preparation-lld.md).

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
cannot introduce new secret prompts. An inconclusive observation or unmet target is reported as
such. Successful observation is not an uptime, useful-work, or continued-availability guarantee.
Guest probes retain their finite existing attempt/time limits. Provider checks retain documented
backend limits, including AWS/Azure SDK defaults that can exceed ten seconds; no uniform ten-second
provider deadline or unbounded convergence polling is added.

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
