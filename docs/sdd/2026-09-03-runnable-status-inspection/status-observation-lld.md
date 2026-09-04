# Status Observation: Low-Level Design

- Status: Design
- Date: 2026-09-03
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Design invariants

1. Inventory selection completes before any live observation.
2. `include_status=False` cannot reach a provider, transport, resolver, activation gate, or database
   write seam.
3. A requested row always receives a domain status. Failed expected observation becomes `UNKNOWN`,
   never an absent mapping and never `STOPPED`.
4. List observation cannot affect filtering or row order.
5. Session and console guest probes use exact tmux names, a non-interactive transport, a 10-second
   timeout, and one attempt.
6. Describe uses the same classifier and observer as list for its resource.
7. Machine not-requested state and requested-unknown state are distinct.
8. Status names remain resource-owned. No cross-domain enum or protocol is introduced.

## Public CLI signatures

Conceptual Typer parameters:

```python
status: Annotated[
    bool,
    typer.Option("--status", help="Include live runtime status"),
] = False
```

This parameter is added to `vm list`, `session list`, and `console list`.

Session list additionally implements R23 with this CLI-only compatibility parameter:

```python
no_status: Annotated[bool, typer.Option("--no-status", hidden=True)] = False
```

Validation order for all list commands:

1. reject `names_only and output_format is json`;
2. reject `status and names_only`;
3. for session only, reject `status and no_status`;
4. emit the deprecation warning when session `no_status` is true;
5. parse and validate remaining CLI-local option relationships; and
6. load only the local services needed by the selected path.

The service parameter is always positive:

```python
include_status: bool = False
```

No manager, row builder, renderer, test helper, or permanent documentation retains `no_status`
except the R23 adapter and its retirement test.

## Observation request representation

The existing listing rows carry projected status values. They do not also carry a presentation-only
request boolean. CLI adapters pass `include_status` directly to human renderers so table shape is
explicit even for an empty result. Machine projection derives its per-row carrier from the joined
row.

## Status types and projections

### VM

Existing `VMStatus` remains:

```python
class VMStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    DEALLOCATED = "deallocated"
    UNKNOWN = "unknown"
```

`VMListRow` adds:

```python
observed_status: str | None
status_disposition: str | None
```

The shared projector accepts a domain `VMStatus` and `operator_stopped`:

```text
RUNNING                 -> ("running", null)
STOPPED, operator=true  -> ("stopped", "manual")
STOPPED, operator=false -> ("stopped", "idle")
DEALLOCATED, true       -> ("deallocated", "manual")
DEALLOCATED, false      -> ("deallocated", "idle")
UNKNOWN                 -> ("unknown", null)
not requested           -> (null, null)
```

VM describe consumes the same projector after its platform call.

### Session

Rename the domain enum member and value:

```python
class SessionStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    RESIDUAL = "residual"
    BROKEN = "broken"
    UNKNOWN = "unknown"
```

`SessionStatus.OK` is an internal runtime value, not persisted state and not a capability contract.
Every reference changes atomically. The JSON projection already owns the public `running` spelling,
so its vocabulary remains unchanged.

`SessionListRow.status` remains a closed string. It is `unavailable` only when
`include_status=False`; a requested row maps one `SessionStatus` directly.

### Console

Add a domain-local enum in the console status module:

```python
class ConsoleStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    RESIDUAL = "residual"
    UNKNOWN = "unknown"
```

The pure classifier receives the conclusive membership facts from an authoritative enumeration:

```python
def classify_console_status(
    *,
    canonical_present: bool,
    staging_present: bool,
) -> ConsoleStatus:
    if staging_present:
        return ConsoleStatus.RESIDUAL
    if canonical_present:
        return ConsoleStatus.RUNNING
    return ConsoleStatus.STOPPED
```

Enumeration failure becomes `UNKNOWN` at the observer and never calls this classifier. A lifecycle
operation likewise examines its raw `ProbeStatus` pair first and raises its existing typed refusal
when either fact is `UNKNOWN`; it converts both conclusive facts to booleans before classification.

`ConsoleListRow.status` is `unavailable` when not requested, otherwise the enum value.
`ConsoleDescription.status` is always one console enum value projected to text.

## Session observation

### Singular observer

Session describe resolves the backing VM, creates the bounded canonical transport, and calls
`check_session_status` directly. Expected identity, address, and transport failures become unknown.
The singular classifier retains typed structural errors for apparently live legacy rows and
malformed stored runtime evidence; those errors are not routed through the forgiving batch parser.
Its `PID_STOPPED` early return is removed: a row with a dedicated socket is probed regardless of
stored PID, while a stopped legacy row without a canonical runtime locator becomes unknown.
Lifecycle callers continue to use the same classifier inside their own boundary.

Both singular and batch classifiers use the same evidence order:

```text
exact session present                              -> RUNNING
exact session absent, dedicated server present    -> RESIDUAL
session and server absent, pid is PID_STOPPED      -> STOPPED
session and server absent, live same-boot process  -> BROKEN
session and server absent, dead or stale process   -> STOPPED
any inconclusive required fact                     -> UNKNOWN
```

The sentinel therefore supports stopped only after live tmux absence is authoritative. It cannot
hide a manually resurrected exact session or residual dedicated server.

### Batch observer

`observe_session_statuses` receives the selected rows and returns every requested name:

```text
initialize result[name] = UNKNOWN for every row
group every selected row by backing VM
for each VM in an eight-worker pool:
    validate canonical SSH identity
    create canonical transport with default_timeout=10
    batch_check_status(rows, target)
    overwrite only returned conclusive or explicit UNKNOWN results
return result
```

`batch_check_status` no longer excludes `PID_STOPPED` rows that have a dedicated socket, and the
list join no longer substitutes stopped before consulting the observer. A requested row without a
canonical socket remains unknown. The batch parser applies the same post-absence `PID_STOPPED`
branch as the singular classifier.

The remote command stays one compound fact script per VM. A compact fixed shell loop consumes
base64-encoded row data, avoiding repeated per-row shell source and keeping a maximum-name fleet
below Windows' 32,767-character process command-line limit. The parser accepts results only when the
call succeeds without stderr and every requested row has exactly one valid, known frame; unframed,
duplicate, unknown, missing, or malformed frames leave the entire VM unknown.

`batch_check_status` bakes in the sole probe policy at its transport call: `tty=False`,
`timeout=10`, and `retries=1`. The last value is the transport's spelling for one total attempt and
zero retries. There are no public policy knobs for callers to vary.

The function does not enter `_best_effort_batch_vm_boundary`. That boundary remains lifecycle-only
and its docstring no longer names session list.

`running_session_names`, used by `console create --all-running`, continues to consume the low-level
batch observer. It receives the renamed `RUNNING` enum and the same timeout safety without acquiring
list presentation policy. The caller preserves its existing `pid != PID_STOPPED` eligibility filter
before observation and refuses an unknown result only within that eligible set. This keeps the
status-inspection change from altering console creation semantics; list and describe still observe
persisted-stopped rows when status is requested.

## Console observation

### Remote session enumeration

One remote command per VM runs `tmux list-sessions` with a format that emits only each session name,
one per line. A successful response becomes a set of complete names. The existing tmux diagnostic
classifier recognizes authoritative no-server absence; any other nonzero result, malformed stream,
or unclassifiable diagnostic leaves every requested console on that VM unknown.

For an authoritative enumeration, the observer derives each validated console's canonical and
staging names locally and compares them to the returned set with exact string equality. Unrelated
user-created tmux sessions and prefix matches have no effect. This produces one coherent server
snapshot and one tmux subprocess per VM rather than two subprocesses per console.

### Batch observer

`observe_console_statuses` mirrors the session grouping shape:

```text
initialize every requested console to UNKNOWN
group by vm_name
for each VM in an eight-worker pool:
    validate row, SSH identity, and Tailscale address
    create canonical admin transport with default_timeout=10
    run one formatted session enumeration with tty=false, timeout=10, retries=1
    classify each canonical/staging pair by exact membership
return complete mapping
```

The observer is placed in a focused `_status.py` module rather than growing the already large
console attach module. Existing lifecycle `_console_runtime_presence` keeps its gated transport,
checks raw unknown presence before classification, and then calls the same pure canonical/staging
classifier for conclusive state decisions.

### Focused describe

Console describe selects its one row directly from the batch observer's complete mapping. It takes
the configured description snapshot even if observation returns unknown; no one-call wrapper adds a
second policy surface.

## VM observation

### Batch composition

`observe_vm_statuses` receives selected `VMRow` values plus config and interaction policy:

1. Initialize every selected name to `VMStatus.UNKNOWN`.
2. Build one request registry including live database resources.
3. Build one live VM node per selected row, reusing site-node memoization. An expected row-local
   site or platform construction failure leaves that row unknown and excludes it from later setup.
4. Walk the remaining union, register the declared secret union on one resolver, and run one
   preflight at system scope.
5. Resolve the union once using the ordinary interaction policy.
6. Build each node's scoped `RunContext` through the existing platform context helper on the owning
   thread, while database access is still safe. Only immutable node rows and contexts cross the
   executor boundary.
7. Group rows by bound site. Run independent sites in a finite worker pool and call
   `node.site.platform.status(row, context)` serially within each site group, because bundled
   platform instances may lazily cache mutable clients or credentials.
8. Replace that row's unknown default only with a returned `VMStatus`.

There is no activation gate and no native or canonical guest transport. Provider status remains the
sole authority.

An expected failure building the shared registry, running the all-or-nothing preflight, or resolving
the all-or-nothing credential union leaves every selected VM at its unknown default. This matches
the existing one-prompt orchestration contract. After setup succeeds and provider dispatch begins,
site or platform status failures affect only their rows. The implementation does not add repeated
per-site resolution passes or provider-specific batching APIs.

### Provider timeout reality

The core worker pool is finite, but it cannot impose a reliable Python timeout on a blocking SDK
call without leaking a worker. Existing provider clients and CLIs retain their own timeout policy.
The implementation inventories those policies and adds a provider-local bound only where the
existing status call lacks one and the provider API supports it. This is not a new vm-platform
contract.

## List services

### VM

```python
def vm_listing(
    db: Database,
    config: Config | None = None,
    *,
    include_status: bool = False,
    interaction: TtyInteractionPolicy | None = None,
) -> VMListing:
```

The service enforces that config and interaction are present when `include_status` is true. It
selects DB rows once, announces human progress, observes those rows, and constructs row facts.
Without status, it never dereferences config.

### Session

```python
def session_listing(
    db: Database,
    config: Config,
    *,
    ...,
    include_status: bool = False,
) -> SessionListing:
```

The local config remains required because `HARNESS INT.` is an effective declaration fact, not a
persisted session column. `_display_registry` continues to use `include_live_resources=False` and
degrade display-only config errors. The default path still performs no external status work.

### Console

```python
def console_listing(
    db: Database,
    config: Config | None = None,
    *,
    ...,
    include_status: bool = False,
) -> ConsoleListing:
```

Config is required only for observation. Session and console inspection use fixed non-interactive
probe policy rather than accepting an interaction parameter. Default list remains its current
database query.

## Describe services

### Session describe ordering

```text
require session and backing local rows
emit human progress
attempt singular non-activating observation
    expected operational failure -> UNKNOWN plus human warning fact
take authoritative structural snapshot
join status and render
```

The structural snapshot is not held open across SSH. Status is momentary and may change immediately;
holding a DB transaction during transport would not make it atomic.

### Console describe ordering

```text
take configured definition and membership snapshot
emit human progress
observe canonical and staging names through bounded transport
join status, preserving configured snapshot on UNKNOWN
render
```

No call reaches console build planning, pane secret resolution, or `_prepare_vm_target`.

### VM describe ordering

VM describe retains its current rich assembly. The status/disposition mapper is shared with list,
and human progress is emitted before provider preflight/status work. It initializes requested status
to `UNKNOWN`; an expected preflight, credential, or provider status failure preserves that value and
adds only the existing closed safe issue projection. Its optional live-resource query remains a
separate describe-only fact and does not enter VM list.

## Human rendering

Renderers branch on the explicit `include_status` argument from the CLI adapter, not on row
contents:

- false: preserve existing inventory columns, except session drops its old placeholder status
  column;
- true: append `STATUS`; VM may render `stopped (manual)` or `stopped (idle)` in one cell; and
- empty selection: preserve the existing friendly empty message with no external work.

Post-table summaries group unknown resources by their observation boundary where that information is
available. Broken and residual session/console statuses remain visible state, not observation
failure. Warnings explain actionable meaning without asserting that a resource is stopped.

Authored wording is review-owned and is not pinned by unit tests.

## JSON projection

The JSON list projectors emit stable fields regardless of the flag:

```text
session: existing status = unavailable | domain status
console: new status = unavailable | domain status
VM:      new observed_status = null | domain status
         new status_disposition = null | manual | idle
```

The 0.18 console describe producer always emits `status`, and session describe keeps its established
required `status`. VM describe keeps its existing fields. Because an additive JSON v1 field remains
optional to consumers and may be absent from older v1 producers, the permanent schema documents that
producer-versus-consumer distinction for new VM and console fields.

Because console and VM list fields are additive and session's field type and value vocabulary do not
change, schema version stays 1. The existing v1 consumer meaning of session `unavailable` remains
"status unavailable for this record." The 0.18 producer uses it only when live work was not
requested and uses `unknown` for requested inconclusive work. Collection order and pre-existing
fields do not change.

## Testing seams

Tests instrument these boundaries, not authored prose:

- list without status: provider status, resolver, activation gate, transport, tmux, and DB update
  stubs must remain untouched;
- list with status: provider/transport read stubs may run, activation/repair/lifecycle and DB update
  stubs must remain untouched;
- exact number of guest calls: one per distinct VM for each domain observer;
- timeout policy: `tty=False`, `timeout=10`, and the transport's one-total-attempt spelling
  `retries=1` reach the transport call;
- cancellation policy: exceptional exit cancels queued work and does not wait for the whole fleet;
- process-launch `OSError` values become typed transport/provider failures and degrade to unknown at
  the observation boundary;
- missing, drifted, or unavailable SSH identity degrades through its narrow policy errors, while
  malformed or unsupported persisted applied-state remains a typed structural failure;
- partial failure after dispatch: one VM/provider unknown, unaffected rows keep their observed
  values; shared VM setup failure leaves all selected VM observations unknown;
- state matrices: every session and console branch, including malformed and mixed diagnostics;
- default and enriched human table structures without pinning warning sentences;
- JSON field types, enum values, null/unavailable distinction, and order;
- describe degradation while configured facts remain intact;
- Windows forced-TTY source transport with explicit non-interactive probe override;
- `--names-only --status` and `--status --no-status` fail before service work; and
- completion candidates use local names-only commands and never pass status.

## File ownership map

| Concern                  | Primary files                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------- |
| CLI grammar              | `cli/agentworks/cli/commands/{vm,session,console}.py`                                 |
| Session status           | `cli/agentworks/sessions/manager/_status.py`, `_queries.py`, `_scope.py`              |
| Console status           | `cli/agentworks/sessions/multi_console/_status.py`, `attach.py`, `__init__.py`        |
| VM status                | `cli/agentworks/vms/manager/inspect.py`, focused helper module if size requires       |
| Domain enums/projections | `cli/agentworks/db/models.py`, `db/projections.py`                                    |
| Completion               | `cli/agentworks/completions/spec.py` and completion tests                             |
| Machine contracts/docs   | `cli/command-reference.md`, operational JSON tests                                    |
| Operator guidance        | `cli/README.md`, `docs/guides/session-status.md`, lifecycle guide, 0.18 upgrade guide |

The implementation may split files further to stay under project size guidance, but it does not move
ownership across domains or introduce a runnable package.
