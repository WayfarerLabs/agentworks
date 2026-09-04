# Runnable Status Inspection: High-Level Architecture

- Status: Design
- Date: 2026-09-03
- Requirements: [frd.md](./frd.md)
- Code basis: `origin/main` at `3f97ea3cd357582ceb71c2c065ad23db8d08379d`

## Summary

Each runnable resource gets the same CLI question and keeps its own answer. List services build a
local inventory first, then optionally enrich that immutable result through a resource-owned status
observer. Describe services use the same observer for one selected resource. Observation is a leaf
operation: it reads provider or tmux state, returns a closed resource-specific status, and never
enters the activation or lifecycle orchestration paths.

The architecture deliberately separates two planes:

```text
local inventory plane                 explicit observation plane
---------------------                 --------------------------
database rows                         VM platform status API
local config/manifests                canonical SSH transport
stored relationships                  exact tmux observations
existing list filters                 guest time bounds and finite concurrency
          |                                      |
          +------------ result join -------------+
                              |
                    human or JSON projection
```

The join enriches selected rows only. Observation cannot add, remove, reorder, repair, or persist a
row.

## Current architecture and correction

### Session

`session_listing` currently treats status as the default and enters
`_best_effort_batch_vm_boundary`. That boundary runs VM preflight and secret resolution and opens
`activation_gate` for each reachable VM. A read-side list can therefore start a stopped VM, repair
Tailscale, and wait without a finite command timeout. The manager boolean `no_status` hides this
inversion.

`session_description` also reaches status through `_prepare_vm`, the activation-gated boundary used
by lifecycle and interactive operations. The status classifier itself is read-only, but its caller
is not.

The target design removes list and describe status from both gated boundaries. Session observation
uses the canonical SSH transport directly after local SSH-identity validation. It groups list rows
by VM, sends one non-interactive fact command per VM, and caps each call at 10 seconds with one
attempt. The existing eight-VM concurrency cap remains.

### Console

Console list and describe are currently local database projections. Lifecycle operations probe the
canonical `aw-console-NAME` and reserved `aw-console-build+NAME` tmux sessions, but only after
entering `_prepare_vm_target`, which opens the activation gate.

The target design extracts the presence-to-console-status classifier from lifecycle policy and adds
a read-only batch observer. The observer groups consoles by VM, obtains one formatted tmux session
enumeration per VM, and compares each validated canonical and staging name by exact string equality.
Unrelated user sessions are ignored. Lifecycle operations keep their gated boundary; inspection
never uses it.

### VM

VM list is currently a database projection. VM describe already queries `VMPlatform.status` without
an activation gate, but it assembles preflight and secrets for one VM at a time and also performs a
best-effort live-resource query.

The target VM list observer builds one request registry, walks the selected live VM nodes, runs one
preflight over the union, resolves the declared provider credentials once, and calls each platform's
existing read-only `status(vm, ctx)` operation. Registry, preflight, and resolution retain their
current all-or-nothing command boundary: an expected failure before provider dispatch leaves every
selected VM unknown while preserving all inventory rows. Independent site groups may run
concurrently within a finite worker pool after setup, while calls sharing one bound platform
instance remain serial because several bundled platforms cache mutable clients and credentials. A
provider-call failure is attached only to the affected row as unknown. No new vm-platform method or
contract version is needed.

VM describe retains its richer focused resource and issue reporting. Its status value continues to
come from the same platform operation as list. The shared status projection is extracted so list and
describe cannot assign different public meanings to one `VMStatus`.

## Components

### 1. Thin CLI adapters

The three list commands expose `status: bool` at the CLI boundary and pass `include_status=status`
to their services. They reject `--status --names-only` before loading config or opening the
database. VM and console list load config only when live observation needs it; session list retains
its existing local config load because the harness-integration column is derived from session
templates and desired overlays.

`session list --no-status` remains a hidden 0.18.0 compatibility option. The adapter validates it
against `--status`, emits the shared suppressible deprecation warning, and otherwise dispatches the
ordinary local list. No manager or renderer receives `no_status`.

Describe adapters keep their existing public shape. Console describe gains config and interaction
policy only because live observation needs the canonical transport. JSON mode continues to assemble
facts under presentation suppression before writing its envelope.

### 2. Resource-owned observation services

Each domain owns a pure status classifier and an I/O observer:

- VM: `VMStatus` and platform `status` remain the authority.
- Session: `SessionStatus` becomes `RUNNING`, `STOPPED`, `RESIDUAL`, `BROKEN`, or `UNKNOWN`.
- Console: a new `ConsoleStatus` is `RUNNING`, `STOPPED`, `RESIDUAL`, or `UNKNOWN`.

The observers return mappings keyed by stable resource name. An absent mapping is not overloaded as
unknown: the list service knows whether observation was requested and assigns the public
not-requested carrier itself. A requested row always receives a concrete domain status, defaulting
to `UNKNOWN` when an expected operational boundary cannot report.

There is no shared runnable observer interface. The three domains have materially different
authorities, credentials, grouping, and state machines. The repeated orchestration shape remains a
small amount of direct code rather than an abstraction with conditional behavior.

### 3. Non-activating transport boundary

Session and console need the same safety property but not a new lifecycle boundary. Their observers:

1. resolve the row's VM locally;
2. require the recorded canonical SSH identity;
3. require a recorded Tailscale host;
4. create the canonical transport with a 10-second default timeout;
5. invoke a non-interactive probe with `tty=False`, `check=False`, and one attempt; and
6. return unknown for the affected VM on an expected transport or identity failure.

They do not build live VM nodes, load provider credentials, call platform status, or enter
`activation_gate`. A stopped VM stays stopped.

The two domain observers may share a narrow internal helper for constructing this bounded canonical
transport if implementation shows exact duplication. They do not share remote fact grammars or
status classification.

### 4. List enrichment

Every list service follows one order:

```text
validate filters and select local rows
        |
        +-- no rows or include_status=false --> project inventory
        |
        +-- include_status=true
                |
                +-- emit human progress before external work
                +-- observe selected rows only
                +-- join by stable resource name
                +-- project inventory plus status
```

Filtering and ordering occur before observation. The observer receives no authority to alter them.
Names-only remains a separate local fast path in CLI and service code.

CLI adapters pass `include_status` to human renderers so they can choose a table shape; listing
records do not duplicate that presentation input. Machine projectors use the row facts alone:
session and console rows carry `unavailable` when skipped, while VM rows carry null observed status.
This preserves session JSON v1 and matches the established VM describe carrier.

### 5. Focused describe

Focused describe always asks for status, but still assembles locally available facts when the live
read fails.

- Session describe replaces `_prepare_vm` with the singular session observer, then takes its
  existing structural snapshot.
- Console describe takes its configured membership snapshot and joins the singular console observer.
  It never computes pane build targets or resolves pane secrets.
- VM describe continues its no-gate provider status flow and shares status/disposition projection
  with VM list. It initializes a requested observation to unknown, preserves unknown on an expected
  preflight, credential, or provider failure, and records only the existing safe issue facts.

Human describe emits a progress line before the external read and a warning for an expected
observation failure. JSON emits `unknown` without backend diagnostic prose. VM JSON retains its
existing closed `issues` records.

### 6. Presentation and machine contracts

The human list status column is conditional:

| Command        | Default table | With `--status`                        |
| -------------- | ------------- | -------------------------------------- |
| `vm list`      | Existing      | Existing plus `STATUS` and disposition |
| `session list` | No `STATUS`   | Existing inventory plus `STATUS`       |
| `console list` | Existing      | Existing plus `STATUS`                 |

The JSON v1 additions are:

| Record               | Not requested | Requested values                            |
| -------------------- | ------------- | ------------------------------------------- |
| session `status`     | `unavailable` | running, stopped, residual, broken, unknown |
| console `status`     | `unavailable` | running, stopped, residual, unknown         |
| VM `observed_status` | `null`        | running, stopped, deallocated, unknown      |

VM `status_disposition` is null except for supported stopped/deallocated observations. Describe
never emits a not-requested state because it always observes.

For 0.18 producers, `unknown` has one meaning across these machine records: live observation was
requested but could not reach a conclusive domain state. An emitted `unavailable` or null means the
list did not request live work. The established JSON v1 consumer meaning of session `unavailable`
remains broader, so older v1 producers may have used it for other unavailable observations.

## Failure model

### Expected operational failures

An expected dispatched boundary failure degrades only affected rows:

- missing or refused canonical SSH identity;
- missing Tailscale address;
- transport timeout or connection failure;
- malformed or mixed remote probe facts;
- row-local site/platform construction or dispatched provider status failure; and
- exact tmux presence that cannot be classified as present or absent.

VM registry construction, the one union preflight, and the one credential-resolution pass are shared
setup boundaries. An expected failure there leaves every selected VM status unknown. This is an
explicit current-mechanism tradeoff: the design does not split setup into repeated per-site prompt
sessions merely to obtain finer preparation-failure isolation.

Human list renders unknown and emits one compact summary grouped by boundary. It does not print one
warning per stopped row. JSON carries closed status facts and no third-party diagnostics.

### Structural failures

Bad CLI combinations, unknown filters, missing referenced local rows, corrupt invariants that make
the inventory itself untrustworthy, and unexpected exceptions remain typed failures. They are not
converted to unknown merely because the operation is observational.

### Timeout and cancellation

Guest probe calls use 10 seconds and one attempt. VM provider clients keep their existing
provider-specific timeout controls; the worker pool limits concurrency but does not pretend Python
can safely cancel a provider call that the provider library cannot cancel. Progress is emitted
before dispatch so the operator understands the delay. Ctrl-C propagates and cancels outstanding
future work where supported; it never persists partial observations.

## Security and side effects

| Operation               | Local config/DB | Secret resolution | Provider read | Guest read      | Activation/repair | DB write |
| ----------------------- | --------------- | ----------------- | ------------- | --------------- | ----------------- | -------- |
| list                    | yes             | no                | no            | no              | no                | no       |
| VM list `--status`      | yes             | provider only     | yes           | no              | no                | no       |
| session list `--status` | yes             | no                | no            | yes             | no                | no       |
| console list `--status` | yes             | no                | no            | yes             | no                | no       |
| describe                | yes             | domain-specific   | VM only       | session/console | no                | no       |

"DB write" excludes the CLI's existing schema-open policy before the service runs. The list and
describe services themselves open read snapshots and never repair or persist observed state.

Resolved provider credentials remain scoped through `RunContext` and never enter facts, logs, human
output, or JSON. Guest probes use the configured SSH identity path but do not resolve secret-source
values. Remote diagnostic text is classified at the boundary and not copied into machine output.

## Capability contracts

No capability API changes.

- vm-platform version 1 already requires `status(vm, ctx) -> VMStatus` as a read-only operation.
- harness-integration version 1 is not involved in liveness observation. Session status describes
  the Agentworks-owned tmux runtime, not whether a harness has useful work in progress.
- Console status is internal tmux state and has no capability surface.

All bundled implementations remain at version 1. An internal method extraction or enum-member rename
does not create an external plugin transition while capabilities are repository-internal.

## Compatibility and release sequencing

The design and implementation ship together in one PR for 0.18.0. The new default is intentionally
observable: plain session list becomes local and status-free, while `session list --status` retains
the old live question with corrected non-activating behavior.

The only compatibility shim is CLI-local:

- 0.18.0 accepts hidden `session list --no-status`, warns through the shared deprecation channel,
  and runs plain session list.
- 0.19.0 removes the option and its tests. Issue tracking is created during closeout if the removal
  is not already represented by the release process.

No old manager boolean or dual observation path remains. Completion, canonical help, permanent docs,
and examples teach only `--status`.

## Rejected alternatives

### Keep session status on by default

Rejected because it makes the most common inventory command remote, slow, and capable of activation.
It also leaves VM and console grammar inconsistent.

### Add standalone status commands

Rejected for now. `list --status` handles fleet inspection and describe handles one resource. Three
more verbs would duplicate selection, rendering, and machine contracts without a demonstrated
question they answer better.

### Introduce a generic Runnable abstraction

Rejected because VM provider power, session dedicated-server state, and console canonical/staging
tmux state do not share an implementation contract. A common enum would discard useful states or
accumulate exceptions.

### Reuse activation gates for reliable transport

Rejected because reliable observation cannot change the observed state. Starting a stopped VM to ask
whether its session is running makes the result self-fulfilling and violates operator intent.

### Persist the latest observation

Rejected because the requested product question is current status. A cache introduces age,
invalidation, ownership, and migration semantics without removing the need for a live read.
