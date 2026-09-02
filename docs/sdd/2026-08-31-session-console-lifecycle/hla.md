# High-Level Architecture: Session and Console Lifecycle

- Status: Draft for design review
- Date: 2026-08-31
- Requirements: [frd.md](./frd.md)
- Detailed design: [lifecycle-lld.md](./lifecycle-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)

## Architectural Result

The CLI presents one lifecycle vocabulary while session and console domains retain separate
implementations:

```text
session create/start/restart             console create/start/restart
              |                                        |
              v                                        v
session lifecycle manager                    console lifecycle manager
  |          |          |                    |          |          |
status   core teardown  launch           live probe  teardown   realization
              |          |                    |                     |
              |          v                    |                     v
              |    harness start result       |          console tmux session and panes
              |          |
              |          v
              |    persist integration state
              |          |
              |          v
              |  create session tmux runtime
              v
 kill dedicated server, verify, stale-state recovery
```

The commonality is command meaning, not a shared runnable abstraction. Sessions retain persisted
PID, socket, template, target-user, and harness state. Consoles retain a database definition and a
derived tmux runtime whose presence is checked live. VMs keep their existing platform-owned power
and initialization lifecycle.

## Components

### CLI groups

The session command group exposes canonical `start`, `stop`, `restart`, and `attach` verbs. The
console group exposes the same four runtime verbs. Command bodies only translate arguments and flags
into manager calls. Managers own status validation, typed errors, mutation ordering, and operator
output.

`--force` and `--force-new` remain independent:

- `--force` authorizes recovery from a broken runtime whose ordinary tmux identity cannot be
  trusted;
- `--force-new` selects a fresh harness conversation when a session launch occurs.

`start --force-new` does not become a restart. It launches fresh from a stopped state and refuses an
already-running session with guidance to use `restart --force-new`. This keeps the lifecycle verb
authoritative over the policy option.

### Session lifecycle manager

The session manager keeps distinct public operations and shares only domain-specific internal steps:

- `start_session` classifies current status and either no-ops, refuses, removes a reachable residual
  dedicated server, performs forced indeterminate-state recovery, or invokes the absent-runtime
  start path;
- `restart_session` performs all pre-mutation work, uses the shared teardown authority when a
  runtime exists, then invokes the same absent-runtime start path;
- `stop_session`, direct deletion, batch operations, and cascading deletion use one teardown
  authority with explicit policy inputs for their actual differences;
- `attach_session` remains a running-runtime-only terminal operation.

The absent-runtime start path resolves the current session definition and graph, constructs a fresh
operation context, asks the harness integration for a launch result, persists any integration state
change, and only then creates the tmux runtime. It is shared by initial create, subsequent start,
and restart after teardown, with the caller supplying `force_new` and creation/retry policy.

This is not a generic state-machine framework. A small number of explicit functions keeps status
handling visible and avoids encoding session-specific recovery in flags on a cross-domain engine.

### Core-owned session teardown

One session teardown authority owns the selected target and current status, exact tmux teardown,
verification, forced broken-state recovery, socket cleanup, and persisted stopped state. A current
reachable runtime is removed with `kill-server` through its persisted, validated managed socket.
Core does not inject `C-c`, enumerate pane processes, or wait through a grace phase. The dedicated
server is the ownership boundary, so the same operation destroys every tmux session, window, and
pane the operator added to that Agentworks runtime. Batch and cascading operations issue teardown to
their reachable candidates and verify each dedicated server exited.

A reachable legacy row may still share the default tmux server. It uses exact
`kill-session -t =NAME` and verifies only that named session absent. It never destroys or signals
the shared server. This asymmetry stays inside the session-domain teardown authority rather than
weakening the dedicated-server guarantee.

If the canonical named session is absent but its dedicated server still answers on the persisted
managed socket, status is `RESIDUAL`. This can happen when a process created a sibling tmux session
and the canonical session later exited. Stop and deletion destroy the server; start and restart do
the same before launch. None requires `--force`, because tmux still provides the ordinary scoped
control authority. Attach refuses because there is no canonical session to join.

The broken-state path is deliberately different because tmux no longer supplies the ordinary control
authority. Session creation persists a `TmuxServerFingerprint` alongside the existing socket and
boot identity: positive server PID and Linux process start time. With explicit `--force`, core
requires the stored boot identity and positive PID, then compares current state. A changed boot or
absent PID proves prior-server absence without a start time. A same-boot existing PID requires a
valid stored start time and mismatch. Only that branch-appropriate proof permits removal of the
exact validated managed admin or agent socket and a durable stopped-state transition. A matching
live process, evidence missing from the applicable branch, or indeterminate check fails closed with
manual-recovery guidance. Core never signals the numeric PID, and a legacy row whose PID may name a
shared server cannot widen that boundary.

Per-VM repair and status retain the VM admin transport. Their probes of agent-owned tmux servers and
`/proc` identities therefore use the existing non-interactive root boundary; owner-targeted singular
operations continue without elevation. An indeterminate fingerprint is terminal for the repair
attempt and cannot enter absence proof or become persisted stopped state.

The harness integration has no stop or restart API. If a future integration demonstrates a concrete
application-specific shutdown requirement that exact tmux teardown cannot satisfy, that requirement
gets a separately designed seam rather than speculative lifecycle machinery now.

Tmux remains the process boundary for this effort. Terminal-detached descendants are outside its
guarantee; systemd-managed cgroup containment is a separate security design tracked in
[issue #715](https://github.com/WayfarerLabs/agentworks/issues/715), not a second supervisor layered
into this lifecycle change.

### Harness integration contract version 1

The capability returns to version 1 because every implementation is in this repository and the
effort deliberately replaces the current internal contract without an adapter.

The target contract is intentionally small:

```python
@dataclass(frozen=True)
class HarnessStart:
    command: str
    note: str | None = None


class HarnessIntegration(Capability):
    def start(self, ctx: RunContext, *, force_new: bool = False) -> HarnessStart: ...
```

`HarnessStart` replaces the mutable `launch_note()` side channel. It carries only the pane command
and an optional truthful pre-launch note. It does not claim that the external tool successfully
continued after the pane starts, and it does not introduce a typed decision taxonomy.

The integration still owns only its namespaced portion of `harness_integration_state`. The session
manager persists the complete blob so a template switch never discards another integration's
namespace.

### Continuation and fresh bindings

`force_new=False` is the ordinary path. Each integration examines its own state and decides whether
continuation is supported and usable. `force_new=True` bypasses continuation discovery except for
the local cleanup or binding rotation needed to launch safely.

The built-in mappings are:

| Integration | Ordinary start                                                        | Forced fresh start                                         |
| ----------- | --------------------------------------------------------------------- | ---------------------------------------------------------- |
| shell       | use `resume_command` when configured, otherwise `command`             | use `command`                                              |
| Claude Code | probe the bound UUID and resume its transcript, otherwise launch it   | mint and bind a new UUID, then launch with that UUID       |
| Grok Build  | probe the bound UUID and resume its state, otherwise launch it        | mint and bind a new UUID, then launch with that UUID       |
| Codex       | adopt recorded state, use a bound ID, discover, pick, or launch fresh | record rejected state, clear the binding, and launch fresh |

Claude and Grok conversation identifiers are immutable external identities, but the Agentworks
binding is replaceable. On forced fresh start the integration stores a new UUID without deleting the
old external conversation. The manager persists that binding before tmux construction. If launch
fails, an ordinary retry reuses the pending UUID instead of minting another.

Codex assigns its own thread identifier after launch. Forced fresh therefore clears the known
binding, stores one flat `fresh_pending` key whose value is the valid recorder identity rejected by
the request or `null`, and issues the existing bare fresh command, whose pane setup removes the
recorder. A later ordinary operation adopts a recorder only when it differs from the rejected
identity in that key. Key absence means no forced-fresh operation is pending; an absent or matching
recorder launches fresh again instead of discovering or adopting older workspace state. This remains
safe when tmux creation fails before pane setup removes the old recorder. If the process fails
before reporting a new identifier, an unbound external thread may remain; the tool has not provided
an identity Agentworks can persist or clean up. This limitation is reported honestly and does not
permit adoption of the rejected prior conversation.

### Console lifecycle manager

The console manager owns a separate lifecycle because a console has no persisted PID or runtime
generation. Its runtime identity is the deterministic tmux session name derived from the console
name.

All realization calls share the existing console layout and pane builder. Public operations own the
state policy around that builder. Every lifecycle probe accounts for both the canonical and reserved
staging names:

- create validates a prospective definition, resolves the target and pane inputs, proves both
  managed names absent, inserts the definition, and invokes the shared builder;
- start removes and verifies any staging residue before pane-secret resolution, no-ops when the
  canonical runtime is present, and otherwise resolves inputs and invokes the builder;
- restart resolves inputs before destroying a healthy runtime, tears it down when present, and
  invokes the builder;
- stop removes both managed names and no-ops when both are absent;
- attach joins only a canonical runtime with no staging residue and never loads console build
  inputs;
- delete retains best-effort cleanup of both names followed by durable-definition deletion.

Create uses an in-memory prospective definition to calculate build inputs before the database row
exists. If stale-runtime absence cannot be established, no new row is inserted. Once absence is
verified, the definition is inserted before building. The sole builder constructs under the reserved
`aw-console-build+NAME` tmux name, whose plus sign makes it structurally disjoint from every
canonical `aw-console-NAME` generated from a valid resource name. It publishes the canonical name
only after every required step succeeds. Shared console teardown owns both names. A later build
failure removes and verifies absence of staging state, retains the durable row, and reports
`console start` as the retry. No runtime-generation column or second builder is introduced.

All tmux operations that accept a target use exact `=NAME` syntax. Prefix selection is forbidden for
canonical and staging probes, attach, teardown, build operations, and final rename, so related names
such as `foo` and `foobar` cannot cross lifecycle boundaries.

### Compatibility wrappers

Compatibility is isolated at the CLI boundary for 0.19:

- hidden `session resume NAME` delegates to canonical restart;
- hidden `session resume --all-stopped` delegates to canonical batch start;
- hidden `session resume --all` delegates to canonical batch restart;
- hidden `console attach --recreate` invokes canonical restart, then canonical attach.

Wrappers accept the bounded legacy option spellings needed by existing automation, emit the ordinary
suppressible deprecation warning, and contain no lifecycle implementation. The session wrapper alone
preserves its former pre-dispatch confirmation for running named/`--all` selections, with legacy
`--yes` as the bypass. Its selection is read-only, and a non-interactive replacement refuses without
that bypass; the canonical restart services remain prompt-free. Internal manager and capability
names switch completely to the new vocabulary. Canonical help, completion, docs, and examples
contain only the new commands. The wrappers are removed in 0.20.

The CLI output layer gains one small deprecation emitter that respects the existing global
`--no-deprecations` state. Completion introspection excludes hidden subcommands as well as hidden
parameters, so a hidden compatibility command cannot leak into generated completion.

No configuration alias is required. The shell integration's `resume_command` remains because it
truthfully names harness-level continuation and becomes the command selected by ordinary start;
`command` is selected under `force_new=True`.

## Operation Ordering

### Session start or restart

```text
load durable definition and current status
        |
        v
validate state and operator policy
        |
        +---- ordinary start already running -> success, no launch work
        |
        +---- start --force-new already running -> typed refusal, no mutation
        |
        v
build graph, readiness, target, and required secret union
        |
        v
resolve required pre-mutation secrets
        |
        v
restart/forced-broken path: shared teardown and verified absence
        |
        v
harness.start(fresh context, force_new=...)
        |
        v
persist complete integration-state blob
        |
        v
create tmux runtime and persist PID/socket
```

The status gates that can refuse before a launch occur before launch-only secret resolution when
those secrets are not needed to classify the runtime. Restart still resolves all inputs required by
the replacement before tearing down a healthy runtime.

### Console attach

```text
load console row -> obtain VM transport -> exact canonical and staging probes
        |
        +---- canonical absent -> typed error naming console start
        |
        +---- staging present -> typed error naming console start
        |
        v
exact canonical attach
```

The path does not construct a console build plan, load pane layouts for realization, or resolve pane
environment secrets. VM activation and transport credentials remain ordinary reachability
boundaries.

## Persistence and Failure Model

One additive nullable session column stores the tmux-server process start time needed to distinguish
an already-exited server from PID reuse during forced broken-state cleanup. Existing harness state
remains in the full namespaced `harness_integration_state` blob. Consoles continue to persist only
their definition.

The key commit points are:

- a session integration binding is persisted before tmux creation;
- a restart does not rotate a fresh binding until the old runtime has been removed;
- after a failed session launch, verified runtime absence leaves the durable row stopped with its
  known pending binding, or Codex's rejected-recorder marker, available to an ordinary retry;
- when failed-launch cleanup cannot prove runtime absence, the durable row retains its addressable
  socket while its PID, boot ID, and start ticks remain unknown rather than claiming stopped state;
- a console create does not expose a row while an unverified predecessor runtime may still exist;
- after predecessor absence is verified, console construction publishes the canonical tmux name only
  after a complete staging build; a failed build retains the durable definition, reporting it as
  stopped only when both managed names are proven absent and otherwise reporting runtime state as
  indeterminate;
- console stopped state means both the canonical and reserved staging tmux names are absent;
- failed pre-mutation validation or secret resolution does not destroy a healthy runtime.

## Security and Authorization Boundaries

- Start and restart resolve only secrets required for the launch they will perform.
- An already-running ordinary session start and a refused running `start --force-new` do not resolve
  launch-only secrets.
- Console attach does not resolve pane secrets. It may resolve only the VM/site inputs required to
  establish its authorized transport.
- Harness notes and errors describe pre-launch decisions, not third-party success.
- State values, generated commands containing sensitive values, and third-party process output are
  not emitted in diagnostics.

## Package and Contract Boundaries

- `agentworks.cli.commands.session` and `.console`: argument translation and compatibility wrappers.
- `agentworks.sessions.manager`: session status, launch, shared teardown, attach, and batch policy.
- `agentworks.sessions.multi_console`: console definition, realization, teardown, attach, and live
  best-effort synchronization.
- `agentworks.capabilities.harness_integration`: version-1 start-only contract, `HarnessStart`, and
  shell implementation.
- built-in plugin packages: integration-owned continuation and forced-fresh behavior.
- database layer: one atomic session-runtime-state update for socket path, PID, boot ID, and process
  start time; console storage APIs remain unchanged, while service-facing method names may change
  with the vocabulary cutover.

No package imports a console or VM implementation into the harness capability. No generic runtime
base, cross-domain status enum, or shared lifecycle dispatcher is added.

## Rejected Alternatives

### Keep `resume` as the core operation

Rejected because the command cannot promise continuation and already replaces a running runtime.
Continuation remains an integration decision under start.

### Make `start --force-new` restart a running session

Rejected because a policy option must not change the lifecycle verb. The command refuses and names
`restart --force-new` instead.

### Put restart on the harness integration

Rejected because the integration supplies workload behavior but does not own tmux, PID, liveness,
exact runtime teardown, or broken-server recovery.

### Keep attach-time console realization

Rejected because attaching should not mutate runtime state or unexpectedly resolve pane secrets.

### Add a generic runnable framework

Rejected because the domains share words but not state or failure mechanics. Explicit domain
managers are smaller and easier to audit.
