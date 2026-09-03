# Upgrading to 0.19

Agentworks 0.19 makes session and console lifecycle explicit. Most operators need only replace old
commands in scripts; resource declarations and stored session/console definitions do not change.

Existing running sessions acquire the new process-start fingerprint lazily when a lifecycle command
needs that identity for safe teardown or replacement. Inspection commands such as
`agw session list`, `describe`, `logs`, and `attach` do not backfill or mutate session rows.

## Replace session resume with start or restart

Choose the operation that matches the intended runtime change:

- `agw session start NAME` starts a stopped session and leaves a running session alone.
- `agw session restart NAME` replaces the runtime whether it is stopped or running.
- Both operations continue the harness conversation when possible. Add `--force-new` to require a
  new conversation when a launch occurs.

For batch automation, replace `session resume --all-stopped` with `session start --all`, and replace
`session resume --all` with `session restart --all`. Existing VM, workspace, agent, admin, and force
filters remain available.

The old `session resume` forms remain accepted, hidden, and warning-producing in 0.19. A named form
maps to restart, `--all-stopped` maps to batch start, and `--all` maps to batch restart. Named and
`--all` compatibility forms still confirm before replacing running sessions unless legacy `--yes` is
present; pass `--yes` for those forms when interactive input is unavailable. They are removed in
0.20.

## Start consoles before attaching

`agw console attach NAME` now only attaches. If the console is stopped, run:

```console
agw console start NAME
agw console attach NAME
```

Use `agw console restart NAME` to rebuild a running or stopped console from its stored definition.
Replace `console attach NAME --recreate` with `console restart NAME`, followed by
`console attach NAME` when an interactive attachment is wanted. The hidden `--recreate`
compatibility form remains accepted with a warning in 0.19 and is removed in 0.20.

## Update harness integrations

Harness integrations now expose one core-facing lifecycle method:

```python
def start(self, ctx: RunContext, *, force_new: bool = False) -> HarnessStart:
    ...
```

Return the pane command and optional operator note together as `HarnessStart`. Remove the old
`resume()` method and mutable `launch_note()` side channel. An ordinary start should continue the
harness conversation when possible; `force_new=True` must request a fresh conversation without
deleting external history.

All in-repository implementations move together to contract version 1. Agentworks matches contract
versions exactly, so an external version-2 implementation is refused rather than adapted. The
version number identifies the complete current shape; it does not restore compatibility with the
different version-1 contract that existed before 0.17.

Core now owns runtime replacement and teardown. Integrations produce launch behavior; they do not
implement session restart, stop, or process signaling.
