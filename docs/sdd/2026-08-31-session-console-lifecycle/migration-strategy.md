# Migration Strategy: Session and Console Lifecycle

- Status: Draft for design review
- Date: 2026-08-31
- Baseline: `8695afcd833790ee433b50bb9f5d5c696177233d`
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [lifecycle-lld.md](./lifecycle-lld.md)

## Current State

At the baseline:

- sessions expose `create`, `resume`, `stop`, `attach`, and `delete`;
- named `session resume` may stop and replace a healthy running runtime after confirmation;
- `resume --all-stopped` and `resume --all` combine selection and replacement policy;
- the service layer and logger call the replacement operation `resume`;
- harness-integration contract version 2 requires separate `start` and `resume` methods and exposes
  a mutable `launch_note()` side channel;
- shell configuration selects `command` for create and `resume_command` for resume;
- Claude and Grok use the same continuation decision in both capability methods;
- Codex uses `start` as its fresh path and `resume` as its state-aware path;
- session stop and deletion contain several related teardown paths;
- console create writes only the database definition;
- ordinary console attach builds the runtime when absent, and `attach --recreate` kills and rebuilds
  it;
- console attach resolves pane secrets whenever it decides a build is needed;
- console list and describe remain database-only;
- session rows already store namespaced harness integration state, PID, boot ID, and socket path,
  but not the process start time needed to distinguish an exited server from PID reuse;
- console rows already store every durable definition field required for later realization.

One additive database migration adds nullable `tmux_server_start_ticks`. The broader migration
affects CLI grammar, internal vocabulary, the in-repository capability API, orchestration ownership,
and operator expectations.

## Domain-Complete Implementation Stack

The target system lands as a short two-PR stack after this design checkpoint:

1. session and harness lifecycle, including canonical session verbs, the hidden session wrapper, the
   complete manager/internal vocabulary cutover, harness-integration version 1, every built-in, and
   that domain's tests and collateral;
2. console lifecycle, including canonical console verbs, the hidden recreate wrapper, realization
   ownership, and that domain's tests and collateral.

The second PR may be stacked on the first and is rebased after the first merges. Neither PR splits a
domain's command surface from its semantics. The SDD remains open until both increments land and the
combined system passes its final gates. No capability adapter or parallel internal execution path is
introduced.

## CLI Compatibility

### Session resume

For 0.19, the old command remains accepted but hidden:

```text
agw session resume NAME              -> agw session restart NAME
agw session resume --all-stopped     -> agw session start --all
agw session resume --all             -> agw session restart --all
```

Existing VM, workspace, agent, admin, and force filters normalize into the corresponding canonical
manager parameters. For named and `--all` forms, the hidden wrapper retains the old confirmation
before replacing running sessions; legacy `--yes` still bypasses it and is required when interactive
input is unavailable. The read-only gate stays in the CLI compatibility shim, while canonical
restart remains intentionally prompt-free. The wrapper emits the ordinary suppressible deprecation
warning and identifies the exact mapping.

The wrapper is excluded from help, completion, examples, and permanent teaching. It is deleted in
0.20.

### Console recreate

For 0.19, `agw console attach NAME --recreate` remains accepted as a hidden deprecated form. It
normalizes to:

```text
agw console restart NAME
agw console attach NAME
```

Ordinary attach changes immediately to attach-only behavior. Preserving first-attach creation as a
compatibility default would preserve the defect this effort fixes. The hidden option is removed in
0.20.

### New force-new policy

`--force-new` is additive and has no old spelling. It maps directly to `force_new=True` from CLI
through service and capability layers. It is independent from `--force`:

- `--force-new` chooses a fresh harness conversation when launch occurs;
- `--force` authorizes broken-runtime recovery.

On a running session, `start --force-new` refuses and directs the operator to `restart --force-new`.

A dedicated server that remains reachable after the canonical session disappears reports the new
`residual` status. It does not require `--force`: stop/delete remove it, start/restart remove it
before launch, and attach refuses because no canonical session exists.

## Configuration Compatibility

No configuration field changes.

The shell integration retains `resume_command`. It names harness-level continuation rather than an
Agentworks lifecycle operation. Under ordinary start it remains preferred when nonempty; under
`force_new=True`, `command` is used.

Session templates keep the same harness configuration and persisted integration-state blobs. Console
definitions keep the same schema. Sample configuration changes only if nearby comments or examples
currently teach the retired CLI vocabulary.

## Capability Contract Cutover

Harness-integration contract version 2 is replaced directly by version 1:

```text
v2: start(ctx) -> str
    resume(ctx) -> str
    launch_note() -> str | None

v1: start(ctx, *, force_new=False) -> HarnessStart
```

All implementations are internal to this repository. Every built-in, manifest descriptor, fake,
test, capability README, root capability inventory, plugin guidance, and guide topic changes in the
same PR. No registry adapter accepts version 2.

No other capability contract changes. In particular, vm-platform retains its existing API and
version.

## Persisted State

### Session bindings

Existing namespaced blobs remain readable without migration. Forced fresh behavior updates only the
selected integration's namespace:

- Claude and Grok replace their UUID binding and preserve old external state;
- Codex removes its known binding, uses one flat `fresh_pending: str | None` key to record the exact
  recorder identity rejected by the request, and removes the managed recorder inside the fresh pane
  command;
- shell stores no integration state.

A normal start or restart does not rotate a usable binding merely because the method name changed.
Rows with legacy default-tmux-server state continue their existing on-use migration into the
per-session socket model through canonical start or restart.

### Tmux server fingerprints

The database migration leaves `tmux_server_start_ticks` null for existing rows. A new runtime stores
it atomically with PID, boot ID, and socket path. A reachable existing dedicated server may backfill
it only after tmux reports the stored PID and the current boot matches the stored boot ID. A broken
same-boot row whose stored PID remains live but has no provable start time cannot prove prior-server
absence; it fails closed and asks for manual recovery rather than risking cleanup around a live or
unrelated process. A changed boot or absent PID already proves the old process gone.

Stopping a retained session clears the start time with the PID sentinel. An older binary may leave a
stale or null value after rollback; a later new binary never trusts that value unless it matches the
current socket-reported server identity.

### Console rows

Every existing console row becomes a stopped durable definition if its derived tmux runtime is
absent, or a running definition if that runtime exists. No backfill records runtime status. The
first lifecycle operation probes it live. A residual reserved staging runtime is incomplete managed
state, not a running console; start/stop/restart/delete clean it through the shared teardown
authority and attach refuses until it is gone.

Existing console runtimes remain usable by ordinary attach. They are rebuilt only under explicit
restart or definition-changing behavior that already requires live synchronization.

## Upgrade Experience

The 0.19 upgrade guide and release notes explain:

1. use `session start` for a stopped session and `session restart` to replace one;
2. both operations attempt harness continuation by default;
3. use `--force-new` to require a new harness conversation;
4. use `console start` before attach when no console runtime exists;
5. replace `console attach --recreate` with `console restart`, followed by attach when desired;
6. hidden compatibility forms last only through 0.19 and are removed in 0.20;
7. harness integration implementations move directly to the version-1 start-only API; core uses
   whole-server destruction for reachable dedicated runtimes, exact session destruction for legacy
   shared-server rows, and proven-absence-only stale cleanup for broken dedicated runtimes.

The guide teaches only canonical commands. Command reference material may include one bounded
compatibility note in the 0.19 upgrade guide, not in every command description.

## Rollback

The additive nullable column remains backward-readable. Rollback to the previous binary restores the
old CLI grammar and capability code, but operators must understand four asymmetries:

- a new forced-fresh operation may have changed a selected integration's binding, so the old binary
  resumes or launches from that new binding rather than recovering the abandoned one;
- while a Codex pending-fresh marker exists without a newly reported ID, the old binary does not
  understand the marker and may read or rediscover the recorder identity the new binary rejected;
- a console created under the new binary may already have a runtime, while the old binary simply
  attaches to it;
- the old binary does not maintain `tmux_server_start_ticks`; after returning to the new binary, a
  reachable server can be safely backfilled, while a broken server without a matching fingerprint
  refuses forced PID recovery.

None of these cases corrupts durable state. Old external conversations remain available through
their own tools even when Agentworks no longer binds to them.

## Removal in 0.20

The release following 0.19 deletes:

- the hidden `session resume` command and its legacy-only option parser;
- the hidden `console attach --recreate` option;
- their deprecation warnings and compatibility tests.

The 0.20 work is recorded as a bounded release task, not a dormant compatibility framework. No old
internal manager or capability method survives until then.

## Migration Verification

- Existing session rows start and restart without blob conversion.
- Existing Claude, Grok, and Codex bindings continue under default start/restart.
- Forced fresh changes only the selected namespace and does not delete external history.
- Existing console rows attach when runtime is present and require explicit start when absent.
- Hidden 0.19 forms reach canonical manager operations and emit deprecation through the shared
  channel.
- Canonical help and generated completions expose no legacy forms.
- Installed-wheel tests cover both old accepted invocations and new canonical invocations.
- A current-surface scan finds retired terminology only in the bounded wrapper, truthful harness
  continuation, external tool arguments, release migration text, or immutable history.
