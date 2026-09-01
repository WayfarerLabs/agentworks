# Low-Level Design: Session, Harness, and Console Lifecycle

- Status: Draft for design review
- Date: 2026-08-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Design Rules

1. Lifecycle verbs define runtime transitions; flags refine policy without changing the verb.
2. Session and console implementations remain separate and share no speculative runnable layer.
3. Core owns runtime status, teardown, persistence, and retry boundaries.
4. Harness integrations own only launch choice and their namespaced state.
5. Attach never realizes runtime state.
6. Required inputs are resolved before destructive mutation.
7. Compatibility terminates at hidden CLI wrappers.

## Harness Integration Contract

### Types

Names are normative unless ordinary implementation constraints require a local private helper name.

```python
@dataclass(frozen=True)
class HarnessStart:
    command: str
    note: str | None = None
```

`command` is the raw pane command before core template substitution and tmux wrapping. An empty
string retains the current meaning of a login shell. `note` is an optional pre-launch observation
for operator output. Neither field is persisted as runtime status.

The abstract surface is:

```python
class HarnessIntegration(Capability):
    contract_version = 1

    def start(
        self,
        ctx: RunContext,
        *,
        force_new: bool = False,
    ) -> HarnessStart: ...
```

The existing `resume()` abstract method and mutable `launch_note()` method are deleted. No adapter,
v2 compatibility class, or dual-dispatch branch remains. Every built-in implementation and fake
migrates in the same change.

### Context and secret delivery

Each start receives a fresh session-scoped `RunContext` with exactly the launch target for its mode
and the integration's scoped secret view. Start runs only after readiness and required secret
resolution have succeeded.

### State ownership

The capability receives its own mutable namespace within the session row's complete
`harness_integration_state` blob. Start may update that namespace. Core persists the complete blob
after a successful start decision and before tmux construction.

Foreign namespaces remain byte-for-byte represented by the same parsed objects unless their owning
integration is selected and changes them.

### Launch-result notes

The base contract does not define a launch-decision enum. Built-ins may provide concise notes such
as continued, launched fresh, or deferred to the tool, but tests validate result structure and
branch behavior rather than authored wording. A note describes only what the integration decided
before process launch.

## Built-in Integration Behavior

### Shell

The configuration remains:

```text
command
resume_command
required_commands
```

`resume_command` is retained because it truthfully describes a harness-level continuation command.

| `force_new` | Selected command                               |
| ----------- | ---------------------------------------------- |
| false       | `resume_command` when nonempty, else `command` |
| true        | `command`                                      |

No remote state probe is added.

### Claude Code

The integration continues to own a canonical UUID under `state["session_id"]`.

Ordinary start:

1. validate or mint the bound UUID;
2. probe for the matching transcript;
3. use `claude --resume UUID` when present;
4. otherwise use `claude --session-id UUID`.

Forced fresh start:

1. mint a new UUID unconditionally;
2. replace only the Claude namespace's `session_id`;
3. do not probe the prior transcript;
4. use `claude --session-id NEW_UUID`;
5. leave the prior transcript untouched.

Core persists the new UUID before tmux creation. A later ordinary retry sees that UUID; if no
transcript exists yet, it launches the same UUID fresh. A second explicit `--force-new` is a new
operator request and may rotate again.

### Grok Build

Grok follows the same binding algorithm as Claude using its own namespace and its own state probe.
Canonical UUID validation remains. Forced fresh rotates the UUID, skips the prior-state probe, and
uses the fresh `--session-id` form without deleting prior state.

### Codex

Ordinary start retains the current ordered sources:

1. a valid target recorder value;
2. a valid persisted binding;
3. bounded discovery or picker behavior;
4. a fresh launch when nothing usable exists.

Forced fresh:

1. removes only Codex's persisted `session_id` binding;
2. records one flat `fresh_pending` key containing the valid target-recorder ID rejected by this
   request, or `null` when no valid recorder exists;
3. retires any valid legacy discovery marker through the existing safe cleanup;
4. builds the existing fresh command, which removes the managed target recorder;
5. performs no discovery or resume probe.

Key absence means no forced-fresh operation is pending. Key presence with a canonical Codex recorder
UUID or `null` is the entire schema; no nested object or generation API is introduced. Codex reports
the new thread ID after process launch, so it cannot be persisted before tmux creation. On a later
ordinary operation, a valid recorder is adopted only when it differs from the canonical UUID stored
in `fresh_pending`, or when the stored value is `null`; adoption removes the key. An absent recorder
or the exact rejected value keeps the key, skips persisted-ID discovery and the picker, and launches
fresh again. The comparison occurs before command-side recorder cleanup, so a tmux failure before
pane execution cannot make retry adopt the rejected identity.

A present marker with any other JSON type or a non-canonical UUID string is malformed persisted
state. The integration emits a fixed safe warning, clears the persisted binding, replaces the
malformed value with the currently visible valid recorder UUID or `null`, and takes the fresh path.
It never degrades malformed pending state to key absence. A second explicit forced-fresh request
likewise replaces the key with the recorder identity visible for that new request.

## Session Operations

### Shared preparation

Preparation resolves the durable row, workspace, VM, effective template and overlay, enabled
resources, live graph, readiness, launch target, environment plan, and required secret union. The
session status and policy gates that can refuse without launch inputs occur before resolving
launch-only secrets. Restart prepares everything required for the replacement before stopping a
healthy runtime.

The implementation should reshape the current `resume_session` orchestration into reusable
session-domain helpers rather than copying it into start and restart. Public manager functions stay
thin enough that their distinct status policy remains obvious.

### Status matrix

| Operation             | Stopped                          | Running                                         | Residual                         | Broken                                      |
| --------------------- | -------------------------------- | ----------------------------------------------- | -------------------------------- | ------------------------------------------- |
| `start`               | launch with continuation allowed | success, no launch work                         | teardown, then launch            | refuse unless `--force`; then recover/start |
| `start --force-new`   | launch fresh                     | refuse and name `restart --force-new`           | teardown, then launch fresh      | refuse unless `--force`; then recover/start |
| `restart`             | launch with continuation allowed | teardown, then launch with continuation allowed | teardown, then launch            | refuse unless `--force`                     |
| `restart --force-new` | launch fresh                     | teardown, then launch fresh                     | teardown, then launch fresh      | refuse unless `--force`; then launch fresh  |
| `stop`                | success                          | dedicated-server teardown                       | dedicated-server teardown        | refuse unless `--force`                     |
| `attach`              | refuse                           | attach                                          | refuse and name start or restart | refuse                                      |

Legacy default-tmux-server rows remain a migration case inside status preparation. Start and restart
migrate them into the existing per-session socket model through the same verified teardown and
runtime creation path.

`SessionStatus.RESIDUAL` means the canonical `=NAME` session is absent while the persisted managed
socket still reaches the fingerprinted dedicated server. It is not a force-gated broken state:
ordinary scoped tmux authority is still available and owns every sibling session on that server.
Status classification MUST test for this state rather than treating every failed `has-session` as
broken.

### Tmux server fingerprint

The session table adds nullable `tmux_server_start_ticks`, the decimal Linux clock-tick value from
field 22 of `/proc/PID/stat`. Together with the existing positive `pid` and `boot_id`, it identifies
one process incarnation across PID reuse.

Tmux creation obtains the server PID through the new socket, reads the VM boot ID and process start
time, and persists all three with the socket path as one runtime-state update. Capture reads the
socket-reported PID, boot ID, and `/proc/PID/stat`, then repeats the socket PID and stat reads
before persistence; both PID and start-time observations must match. Parsing locates the final `)`
ending the process command and selects the twentieth whitespace-delimited field after it, which is
Linux stat field 22; it does not split the command's `(tmux: server)` value on whitespace. A stopped
sentinel clears the start time. Existing positive-PID rows migrate with a null start time. When
their tmux socket is reachable, lifecycle preparation may backfill through the same repeated capture
only when the socket-reported server PID matches the stored PID and the stored boot ID matches the
VM. A same-boot broken row whose stored PID still exists but has no provable start time cannot
distinguish that process from the old server and fails closed with manual-recovery guidance.

### Absent-runtime launch

The private absent-runtime launch operation receives:

- the prepared session graph and targets;
- resolved session environment values;
- `force_new`;
- creation versus existing-row persistence policy.

It performs:

1. construct one fresh harness start context;
2. call `integration.start(ctx, force_new=force_new)`;
3. apply core template substitution to `HarnessStart.command`;
4. surface `HarnessStart.note` when present;
5. persist the complete namespaced state blob;
6. create the tmux session;
7. persist socket path and the complete tmux server fingerprint;
8. complete existing restricted-config and creation bookkeeping.

For create, step 5 is part of inserting the new row. If a later creation step fails, existing
partial-state teardown removes the row, which is outside pending-binding retry stability. For an
existing row, step 5 updates it before tmux mutation so ordinary retry reuses a known binding.

### Start

The public operation first resolves and classifies status:

- stopped enters the absent-runtime launch path;
- running with `force_new=False` reports the existing runtime and returns before launch-only secret
  resolution;
- running with `force_new=True` raises a typed state error before mutation and identifies
  `restart --force-new`;
- residual enters shared dedicated-server teardown without `force`, then the absent-runtime launch
  path;
- broken requires `force`; after fingerprinted proof that the prior server is already absent and
  exact stale-socket cleanup, it enters the absent-runtime path with the caller's continuation
  policy.

`start --all` applies the same matrix per selected session. With `--force-new`, stopped selections
launch fresh while running selections remain untouched and report the same actionable error as the
named operation. Those errors contribute to aggregate failure but do not prevent other selected
stopped sessions from starting. Remote execution retains current batch partial-success reporting
rather than pretending remote operations are atomic.

### Restart

Restart validates and resolves the replacement before destructive action. It then:

- skips teardown for stopped sessions;
- uses shared teardown for healthy running sessions;
- uses shared dedicated-server teardown for residual sessions without requiring `force`;
- requires `--force` and proves the prior server absent before cleaning broken session state;
- invokes absent-runtime launch with the requested `force_new` value.

Restart itself is authorization to replace a healthy running runtime and does not prompt solely for
that fact. Batch restart applies the same per-session matrix without the retired resume confirmation
ceremony.

### Shared teardown

A private session-domain teardown operation consumes prepared candidates and explicit policies for
direct stop, restart, delete, or cascading cleanup. It does not accept open-ended callbacks or
generic resource types.

For reachable healthy sessions it:

1. validates the persisted socket as a canonical absolute path directly inside the expected managed
   admin or agent owner directory, preserving the stored artifact identity rather than re-deriving
   it from the current naming convention;
2. requires that socket's reported server PID and current boot/start-time identity match the stored
   fingerprint, then invokes `tmux -S SOCKET kill-server` through the already authorized target;
3. verifies the fingerprinted server process exited, treating a concurrent natural exit as success;
4. removes the exact persisted socket if tmux left it behind and verifies that path is absent;
5. only then persists the stopped PID sentinel and null start time when the caller retains the
   session row.

There is no pane enumeration, injected terminal key, direct pane signal, or grace sleep. Tmux owns
the dedicated server's pseudo-terminals and destroys every session, window, and pane on it,
including a sibling tmux session the operator created after launch. A legacy reachable
shared-default-server row instead invokes `tmux kill-session -t =NAME`, uses exact `=NAME` targets
rather than prefix matching, verifies only that session absent, and does not require or attempt
shared-server exit.

If ordinary teardown fails, the operation rechecks exact session and server state. A naturally
exited server is success. An indeterminate result is a typed failure. With explicit `force`, a
broken dedicated runtime may enter stale-state recovery when a canonical stored boot identity and a
positive PID are present. Recovery applies one conditional absence proof:

1. a changed current VM boot ID proves the old process gone without a start time;
2. on the same boot, an absent stored PID proves the old process gone without a start time;
3. on the same boot with that PID present, a valid stored start time is required and the same robust
   `/proc/PID/stat` parser must return a different start time to prove the old process gone;
4. apply the same canonical managed-owner/root validation to the persisted socket path, preserving
   historical DB-source-of-truth path shapes under both admin and agent roots;
5. only after proven absence, remove that exact stale socket, verify the path absent, and permit
   stopped-state persistence.

A matching live process, a same-boot live PID with missing start time, malformed process data,
transport failure, or any other indeterminate result refuses recovery with manual-recovery guidance.
Core never invokes `kill` for the numeric PID. Legacy rows get the same absence-only treatment and
never signal or destroy a possibly shared server. Parent deletion paths preserve their current
best-effort or fail-closed policy when a target cannot be reached.

This contract deliberately stops at tmux-owned terminal state. A process that deliberately detaches
from its terminal may outlive tmux; systemd/cgroup containment of all descendants is the separate
security design tracked in [issue #715](https://github.com/WayfarerLabs/agentworks/issues/715).

## Console Operations

### Prospective definition

Create validates an in-memory `ConsoleDefinition` carrying name, VM, ordered members, shell
declarations, and admin-shell setting. Existing validation and expansion of `--all` or
`--all-running` occurs before persistence. Build-secret target derivation is adjusted to accept this
definition rather than requiring an inserted row.

No public schema or new database table is introduced. The prospective value is an internal service
record used to keep stale-runtime verification ahead of insertion.

### Prepared realization

The shared preparation step obtains:

- the durable or prospective definition;
- VM and target under the existing activation gate;
- current console layout;
- the complete pane plan;
- resolved pane environment secrets when a build will occur.

The existing `_build_console_tmux` remains the sole builder but becomes a staging builder. The
canonical name is `aw-console-NAME`. The exact staging form is `aw-console-build+NAME`; `+` is valid
in a tmux session name but invalid in an Agentworks resource name, and the distinct prefix makes the
staging form structurally impossible for any canonical console. The implementation proves this
across the accepted console-name grammar and an actual tmux name probe.

The builder first removes and verifies absence of that console's staging name, creates every
required window and pane under it, and treats any failed required tmux result as build failure. It
publishes the canonical console name only by renaming the complete staging session after confirming
the canonical name is absent. A failed step tears down the staging session and verifies both staging
and canonical names absent before returning failure. This makes canonical-name presence an objective
complete-runtime signal, so idempotent start needs no new persisted status.

Every console tmux command that accepts a target uses tmux's exact `=NAME` target form. This
includes canonical and staging probes, attach, kill, window/pane setup, selection, and the source of
the final rename. Creation supplies the complete staging name directly, and rename supplies the
complete canonical destination. No console helper may rely on tmux prefix matching. Behavioral
coverage uses related pairs such as `foo` and `foobar` for both canonical and staging names.

### State matrix

| Operation | Both names absent          | Canonical only                                       | Staging present                                           |
| --------- | -------------------------- | ---------------------------------------------------- | --------------------------------------------------------- |
| create    | insert definition, build   | remove stale predecessor before insert, then build   | remove all predecessor artifacts before insert/build      |
| start     | resolve pane inputs, build | success without pane-secret resolution               | remove staging; keep canonical or build when it is absent |
| restart   | resolve inputs, build      | resolve inputs, teardown, verify absence, then build | resolve inputs, teardown both, verify, then build         |
| stop      | success                    | teardown and verify both names absent                | teardown both and verify both names absent                |
| attach    | typed refusal naming start | interactive attach only                              | typed refusal naming start                                |
| delete    | delete definition          | best-effort teardown, then delete definition         | best-effort teardown of both, then delete definition      |

### Create commit boundary

Create performs:

1. validate the prospective definition and absence of a database row;
2. prepare target, layout, pane plan, and required values;
3. probe the canonical and staging tmux names for this console;
4. if either is present, remove both through console teardown and verify absence;
5. insert the definition and membership in one transaction;
6. invoke the shared builder;
7. report complete success only after the builder succeeds.

Failure through step 4 leaves no new definition. Failure after step 5 retains the valid definition,
but the builder must remove and verify absence of its staging runtime before reporting that runtime
start failed and naming `console start NAME` as the retry. Failure to verify cleanup is reported as
an explicit cleanup failure rather than a stopped or usable console. A console is called stopped
only after both managed names are verified absent.

### Start, restart, stop, and attach

Start probes both managed names before build planning and pane-secret resolution. Canonical-only
presence is an idempotent success. A staging residual is removed and verified first without pane
secrets; an existing canonical runtime may then remain an idempotent success, while canonical
absence resolves construction inputs and enters prepared realization. Residual cleanup is an
intentional recovery step before the construction-input ordering boundary.

Restart prepares and resolves the full replacement before removing a healthy runtime. Absence enters
realization directly. Presence uses console teardown, verifies absence, and then realizes.

Stop uses the same teardown helper for canonical and staging names, no-ops only when both are
absent, and never removes database state. Delete invokes that helper best-effort for both names
before row deletion.

Attach performs the nesting check, loads the row, opens the ordinary VM transport boundary, and
probes both managed names. It attaches only when the canonical name exists and the staging name does
not. Otherwise it refuses without mutation. It does not load the console layout or resolve pane
environment values.

Existing live best-effort membership synchronization remains. Recovery hints and `restore-session`
use `console restart` when a full rebuild is required.

## Compatibility Surface

### Session resume wrapper

The 0.19 hidden command accepts the legacy argument shapes and normalizes immediately:

| Legacy invocation                  | Canonical call              |
| ---------------------------------- | --------------------------- |
| `session resume NAME`              | `restart_session(NAME)`     |
| `session resume --all-stopped ...` | `start_all_sessions(...)`   |
| `session resume --all ...`         | `restart_all_sessions(...)` |

Legacy `--yes` remains parser-compatible but does not recreate the old running-session prompt. Every
invocation emits one suppressible deprecation warning. The wrapper is absent from help and
completion and is deleted in 0.20.

The output package exposes one `deprecation(message)` helper. It emits through the ordinary warning
presentation only when `deprecations_suppressed()` is false. Command wrappers call this helper once;
service operations do not know they were reached through compatibility syntax.

Completion spec construction skips hidden subcommands at the same introspection boundary where it
already skips hidden parameters. Generated Bash, Zsh, and PowerShell scripts therefore cannot offer
the wrapper even though Click can still dispatch it.

### Console recreate wrapper

For 0.19 only, hidden `console attach NAME --recreate` emits one deprecation warning, calls
`restart_console`, and on success calls ordinary `attach_console`. It does not call the builder
directly. The option is removed in 0.20.

## Error and Output Contract

- Managers raise existing typed `AgentworksError` subclasses; CLI bodies do not implement business
  validation.
- Start/restart output names whether Agentworks started, stopped, or replaced the runtime.
- Harness notes are additional indented detail, not the lifecycle result.
- A requested continuation is never reported as a confirmed external resume.
- Console create distinguishes persisted-definition success from runtime-start failure.
- Batch operations report per-resource failures and return a nonzero aggregate result while
  retaining successful transitions.
- Tests assert error kinds, state, mutation order, calls, and exit behavior, not authored prose.

## Structural Deletions

The implementation removes or renames all current operational surfaces that encode the old model:

- `HarnessIntegration.resume`, `launch_note`, and each built-in override;
- `resume_session`, `resume_all_sessions`, `_resume_sessions`, logger operation labels, local
  variables, comments, and current-facing tests that mean runtime restart;
- attach-owned console first-build and rebuild branches;
- duplicate session kill loops superseded by the shared teardown authority;
- canonical completion/help entries for resume and `--recreate`.

Historical release notes, locked SDDs, external CLI flags, and truthful `resume_command` references
remain.

## Verification Matrix

Tests remain structural and behavioral:

- session status matrix for create/start/restart/stop/attach, including `force` and `force_new`;
- pre-mutation readiness and secret failures leave healthy runtimes untouched;
- running `start --force-new` refuses before launch-secret resolution and mutation;
- Claude/Grok UUID rotation, no prior-state probe, state persistence before tmux, and ordinary retry
  reuse;
- Codex forced-fresh binding/recorder cleanup, rejected-recorder comparison on pre-exec failure and
  retry, malformed type/non-canonical UUID convergence, and no prior-state adoption;
- shell selection of `command` versus `resume_command`;
- dedicated `kill-server` teardown of every managed tmux session/pane/window, including a sibling
  tmux session, plus exact legacy `kill-session`, with no key injection or grace delay;
- stable server-fingerprint persistence and backfill, proven-absence-only stale cleanup, both
  managed socket roots, no numeric-PID signaling, and legacy shared-server protection;
- direct and cascading teardown use the shared authority, including residual dedicated runtimes;
- console attach performs no builder or pane-secret work;
- console stale-predecessor create boundary, staging publication/cleanup, required-step failure, and
  post-insert definition retention;
- exact tmux targeting isolates related canonical and staging names such as `foo` and `foobar`;
- console state matrix, single builder, and DB-only inspection/completion;
- hidden compatibility routing, suppressible deprecation emission, and canonical help/completion
  absence;
- capability version 1 and removal of old internal operation names;
- installed-wheel CLI smoke and representative live admin/agent, continuation/fresh, and
  session/console transitions.
