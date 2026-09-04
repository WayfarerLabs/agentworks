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

Session list additionally retains this 0.18-only compatibility parameter:

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
except the bounded compatibility adapter and its retirement test.

## Observation request representation

List aggregate records carry whether observation was requested because table shape cannot be
inferred from row values or from an empty result:

```python
@dataclass(frozen=True)
class VMListing:
    vms: tuple[VMListRow, ...]
    status_requested: bool

@dataclass(frozen=True)
class SessionListing:
    sessions: tuple[SessionListRow, ...]
    status_requested: bool

@dataclass(frozen=True)
class ConsoleListing:
    consoles: tuple[ConsoleListRow, ...]
    status_requested: bool
```

This is an internal presentation fact, not a JSON field. Machine projection derives its per-row
carrier from the joined row.

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

The pure classifier receives canonical and staging `ProbeStatus`:

```text
if either is UNKNOWN:       UNKNOWN
else if staging is PRESENT: RESIDUAL
else if canonical PRESENT:  RUNNING
else:                       STOPPED
```

Unknown dominates even when the other probe is present. A status inspection cannot safely claim a
healthy or residual complete state while one managed name is inconclusive.

`ConsoleListRow.status` is `unavailable` when not requested, otherwise the enum value.
`ConsoleDescription.status` is always one console enum value projected to text.

## Session observation

### Singular observer

```python
def observe_session_status(
    db: Database,
    config: Config,
    session: SessionRow,
) -> SessionStatus:
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED
    vm = resolve_backing_vm(db, session)
    target = bounded_observation_transport(db, config, vm)
    return check_session_status(session, target=target)
```

Expected local identity, address, and transport failures are caught by the describe/list
orchestrator and converted to unknown. `check_session_status` retains its pure classifier behavior,
including typed legacy-state errors. Lifecycle callers that need those typed errors continue to call
it inside their own boundary.

### Batch observer

`observe_session_statuses` receives the selected rows and returns every requested name:

```text
initialize result[name] = STOPPED for PID_STOPPED rows
initialize result[name] = UNKNOWN for every other row
group non-stopped rows by backing VM
for each VM in an eight-worker pool:
    validate canonical SSH identity
    create canonical transport with default_timeout=10
    batch_check_status(rows, target, timeout=10, retries=1, tty=false)
    overwrite only returned conclusive or explicit UNKNOWN results
return result
```

The remote command stays one compound fact script per VM. `batch_check_status` takes explicit probe
policy rather than relying on an unbounded transport default. It passes `tty=False` so Windows' old
interactive-shell TTY workaround does not merge parseable probe streams. Tests cover inherited-TTY
diagnostics as input to the parser because older transports and fixtures may still produce them.

The function does not enter `_best_effort_batch_vm_boundary`. That boundary remains lifecycle-only
and its docstring no longer names session list.

`running_session_names`, used by `console create --all-running`, continues to consume the low-level
batch observer. It receives the renamed `RUNNING` enum and the same timeout safety without acquiring
list presentation policy.

## Console observation

### Remote fact grammar

For each console, one remote script runs exact `tmux has-session` for the canonical and staging
names. Each fact line contains only the validated Agentworks console name, return codes, and
hex-encoded diagnostic streams:

```text
C:<console>:<canonical_rc>:<canonical_diag_hex>:<staging_rc>:<staging_diag_hex>
```

The parser requires exactly six fields, a selected name present in the request mapping, integer
return codes, valid UTF-8 hex diagnostics, and a complete canonical/staging pair. It delegates each
pair to the existing tmux presence classifier with `missing_target_is_absent=True`. Missing,
duplicate, malformed, or unclassifiable facts leave that console unknown.

Exact targets use the existing quoted `=NAME` helper. The observer never parses `tmux list-sessions`
or treats an unrelated user tmux session as Agentworks state.

### Batch observer

`observe_console_statuses` mirrors the session grouping shape:

```text
initialize every requested console to UNKNOWN
group by vm_name
for each VM in an eight-worker pool:
    validate row, SSH identity, and Tailscale address
    create canonical admin transport with default_timeout=10
    run one compound fact script with tty=false, timeout=10, retries=1
    classify each returned pair
return complete mapping
```

The observer is placed in a focused `_status.py` module rather than growing the already large
console attach module. Existing lifecycle `_console_runtime_presence` calls the same pure
canonical/staging classifier for its state decisions, but lifecycle keeps its gated transport and
typed refusal behavior.

### Singular observer

Console describe calls the batch observer with one row or a singular wrapper over the same remote
fact builder. It takes the configured description snapshot even if observation returns unknown.

## VM observation

### Batch composition

`observe_vm_statuses` receives selected `VMRow` values plus config and interaction policy:

1. Initialize every selected name to `VMStatus.UNKNOWN`.
2. Build one request registry including live database resources.
3. Build one live VM node per selected row, reusing site-node memoization.
4. Walk the union, register the declared secret union on one resolver, and run one preflight at
   system scope.
5. Resolve the union once using the ordinary interaction policy.
6. Build each node's scoped `RunContext` through the existing platform context helper.
7. Group rows by bound site. Run independent sites in a finite worker pool and call
   `node.site.platform.status(row, context)` serially within each site group, because bundled
   platform instances may lazily cache mutable clients or credentials.
8. Replace that row's unknown default only with a returned `VMStatus`.

There is no activation gate and no native or canonical guest transport. Provider status remains the
sole authority.

A failure building the shared registry, preflight, or credential union affects every selected VM
that depends on that boundary. Site- or platform-specific construction and status failures affect
only their rows where the dependency graph exposes that isolation. The implementation should reuse
existing graph facts rather than add provider-specific batching APIs.

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
    interaction: TtyInteractionPolicy,
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
    interaction: TtyInteractionPolicy | None = None,
) -> ConsoleListing:
```

As with VM list, config and interaction are required only for observation. Default list remains its
current database query.

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
and human progress is emitted before provider preflight/status work. Its optional live-resource
query remains a separate describe-only fact and does not enter VM list.

## Human rendering

Renderers branch on `listing.status_requested`, not on row contents:

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

Console describe adds required `status`. Session describe keeps required `status`. VM describe keeps
its existing fields.

Because console and VM list fields are additive and session's unavailable value already means
skipped work, schema version stays 1. Collection order and pre-existing fields do not change.

## Testing seams

Tests instrument these boundaries, not authored prose:

- list without status: provider status, resolver, activation gate, transport, tmux, and DB update
  stubs must remain untouched;
- list with status: provider/transport read stubs may run, activation/repair/lifecycle and DB update
  stubs must remain untouched;
- exact number of guest calls: one per distinct VM for each domain observer;
- timeout policy: `tty=False`, `timeout=10`, `retries=1` reaches the transport call;
- partial failure: one VM/provider unknown, unaffected rows keep their observed values;
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
