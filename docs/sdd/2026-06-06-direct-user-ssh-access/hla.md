# Direct target-user SSH access: HLA

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

Three architectural changes, each independently small but composing into a cleaner access surface:

1. **Access modes codified.** A small helper builds an SSH target keyed on the Linux user the shell
   should run as (admin or an agent). All shell-opening sites use this; the `sudo --login` detour
   disappears.
2. **Agent authorized_keys integrated into the existing reconciliation path.** The current
   `_reconcile_authorized_keys` helper is generic over `home`; agent create / reinit call it with
   the agent's home dir.
3. **VM hardening at init.** `hidepid=1` on `/proc` via `/etc/fstab`; an opinionated sysctl set
   written to `/etc/sysctl.d/99-agentworks.conf`. Applied at `vm create`, reapplied at `vm reinit`.

Existing tmux socket / `server-access` infrastructure for cross-user attach is unchanged.

## Access mode plumbing

### Target-user `ExecTarget` builders

A small extension to the existing SSH target machinery in `agentworks.ssh`. The existing
`admin_exec_target(vm, config)` stays; a parallel `agent_exec_target(vm, config, agent)` is added:

```python
def admin_exec_target(vm: VMRow, config: Config) -> ExecTarget:
    """ExecTarget that connects to the VM as the admin user (existing)."""

def agent_exec_target(vm: VMRow, config: Config, agent: AgentRow) -> ExecTarget:
    """ExecTarget that connects to the VM as the agent's Linux user (new)."""
```

Both return `ExecTarget`, so downstream code is identical regardless of which builder produced it.
Internally they share a private builder that takes a Linux username plus the interactive /
non-interactive choice and threads it through the existing identity / proxy-jump / Tailscale-host
derivation; the exact shape of that helper is an LLD concern.

Call sites pick the builder that matches the operation's subject:

- Admin operations and all admin paths into agent state (per FRD R1's carve-out):
  `admin_exec_target`.
- New agent-mode session create / restart and `agent shell`: `agent_exec_target`.

### Mode 1: provisioning (admin only)

Unchanged from today: `admin_exec_target(vm, config)`, `target.run(...)` for scripts. This SDD does
not change Mode 1's transport.

### Mode 2: interactive shells

Surfaces: `vm shell`, `workspace shell`, `agent shell`, `workspace console`.

```python
# vm shell: admin
target = admin_exec_target(vm, config)
interactive(target)

# agent shell: agent
target = agent_exec_target(vm, config, agent)
interactive(target)  # ssh -t <agent-user>@vm with no command, login shell
```

`agent shell`'s current two-branch structure (with-workspace via `sudo su --login agent -c '...'`,
without-workspace via `sudo su --login agent`) collapses to one path: SSH as the agent directly,
with an optional `cd <workspace>` if a workspace was named.

### Mode 3: tmux session creation

Surfaces: `sessions/tmux.create_session` (both admin and agent branches), `sessions/console.*`,
`sessions/multi_console.*`.

```python
# admin-mode session: admin user
target = admin_exec_target(vm, config)
target.run(f"tmux new-session -d -s {q_session} ...")

# agent-mode session: agent user (no more sudo --login)
target = agent_exec_target(vm, config, agent)
target.run(f"tmux -S {q_sock} new-session -d -s {q_session} ...")
```

The `sudo --login -u <agent> tmux ...` form at `sessions/tmux.py:336-340` is replaced. The tmux
server, the panes inside it, and any commands they spawn all run as the agent uid: same end state as
today, reached via one less indirection.

The socket-permission tweak (`chmod g+rwx <sock>`) that previously ran via admin's sudo path now
runs as the agent (the socket's owner). Same effect, no sudo needed.

## Authorized keys for agents

### What changes

`vms/initializer._reconcile_authorized_keys` writes admin's `~/.ssh/authorized_keys` today via
`target.write_file(..., mode="600")`, which goes through `scp` as the connected SSH user. That works
fine for admin (admin writes to their own home), but it does not work for agents: admin cannot `scp`
into `/home/<agent>/.ssh/authorized_keys` because `scp` runs as the SSH user (admin) and the agent
home is mode 0700 owned by the agent. The existing helper needs a small extension to support writing
to a non-self path.

#### Mechanism: stage and install

For agent users, the write is a two-step staging path that runs over admin's existing `ExecTarget`:

1. `scp` the new `authorized_keys` content to a private staging path on the VM, e.g.
   `/tmp/agw-ak.XXXXXX`, owned by admin and mode 0600.
2. `sudo install -o <agent> -g <agent> -m 0600 <staging-path> /home/<agent>/.ssh/authorized_keys`.
   `install` does an atomic rename into place with the requested owner / group / mode, so the
   on-disk file is never momentarily readable by another uid or with the wrong mode.
3. `sudo rm -f <staging-path>` (cleanup; `install` does not consume the source).

This is the only path used for the agent. There is no separate "write as the agent" path on reinit:
one declarative process satisfies both first-time create and subsequent reinit, matching how the
rest of agentworks treats configuration. The admin user retains its existing direct-write flow (no
staging needed, since admin writes to its own home).

The extension to `_reconcile_authorized_keys` is small: add an optional `owner` argument (defaults
to "the connected SSH user," preserving today's behavior); when set, switch to the stage-and-install
path. The exact signature is an LLD concern.

#### Invocation points

```python
# agents/manager._create_agent_on_vm (new step, after user / home creation)
_reconcile_authorized_keys(
    target,                          # admin's ExecTarget
    config,
    home=f"/home/{agent.linux_user}",
    owner=agent.linux_user,          # triggers the staging path
    logger=ssh_logger,
)

# agents/manager.reinit_agent (new step): identical call
```

### Permissions

The agent's `~/.ssh/` directory is mode 0700, owned by the agent. The `authorized_keys` file is mode
0600, owned by the agent. `install` sets both owner and mode atomically; ownership of `~/.ssh/`
itself comes from the agent create path that established the home dir.

### Operator key rotation flow

Per FRD R3: operator updates `operator.ssh_public_key` and / or `operator.extra_ssh_public_keys` in
config, then runs `agent reinit <name>` for each agent that should pick up the change. `vm reinit`
continues to handle admin only; agents are an independent lifecycle (mirroring workspaces). If an
operator wants to refresh all agents at once, that is convenience functionality on top of
`agent reinit` and out of scope here.

## Per-agent operator SSH config

`ssh_config.py` today writes one `Host` block per VM under the operator's SSH config directory
(`Host <prefix><vm>`, e.g. `Host awvm--vm1`). This SDD adds one block per agent on top of that:

```text
Host awvm--vm1--claude
  HostName <vm1-tailscale-host>
  User claude
  IdentityFile ~/.ssh/agentworks_ed25519
```

The HostName, IdentityFile, and other per-VM settings are identical to the admin entry for the same
VM; only `User` differs. The block is written / refreshed / removed by the agent lifecycle:

- **Agent create**: write a new block immediately after the agent is provisioned.
- **Agent reinit**: rewrite the block in place. Same declarative path as create.
- **Agent delete**: remove the block.

Like the admin block, this is operator-machine state, not on-VM state. Touchpoint:
`agentworks/ssh_config.py` gets a per-agent variant of its existing per-VM writer.

## VM hardening

### `/proc` mount with `hidepid=1`

Applied at `vm create` provisioning (via `vms/initializer.*`) and re-applied at `vm reinit`:

```text
# /etc/fstab snippet, written by agentworks at init
proc  /proc  proc  defaults,hidepid=1  0  0
```

Applied immediately without reboot via `mount -o remount,hidepid=1 /proc`.

### Sysctl set

Written to `/etc/sysctl.d/99-agentworks.conf` and loaded via `sysctl --system`:

```text
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 1
kernel.yama.ptrace_scope = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2
kernel.unprivileged_bpf_disabled = 1
```

Most are likely defaults on a recent Debian; the file makes the contract explicit and guarantees the
baseline regardless of upstream defaults drift.

### Idempotency

`vm reinit` applies the hardening identically to `vm create`. Both must converge:

- **fstab edit**. Append a sentinel-tagged line on first run:

  ```text
  proc  /proc  proc  defaults,hidepid=1  0  0  # agentworks: hidepid
  ```

  On subsequent runs, detect the `# agentworks: hidepid` sentinel. If the line matches exactly, do
  nothing (idempotent no-op). If the line is present but the mount-options field differs, rewrite
  that line in place. Never duplicate the line.

- **sysctl file write**. Compute the desired content; compare against the existing file content. If
  identical, no write and no reload. If different (or missing), write atomically and run
  `sysctl --system`.

- **Live remount**. `mount -o remount,hidepid=1 /proc` is naturally idempotent (remount to the same
  options is a no-op).

This keeps `vm reinit` quiet on a steady-state VM and observable when hardening actually drifts.

### Compatibility with existing pid checks

Per FRD R5, the four call sites doing `test -d /proc/<pid>` are expected to continue working
cross-uid under `hidepid=1` because mode 1 restricts access to _files inside_ `/proc/<pid>/`, not
the directory entry itself. Plan phase 1 verifies this empirically before any other work, with a
fall-back to sudo if the assumption turns out to be wrong.

## Migration semantics

No active migration is needed:

- **Running tmux servers on agent uids** persist across the code change. They were started by the
  previous `sudo --login -u agent tmux ...` path, run as the agent uid, and don't care how they were
  spawned.
- **Sockets and `server-access` ACLs** are persistent state on the VM, established at create. The
  group permissions admin uses to attach are unaffected by the SSH-user change for create/restart
  operations.
- **`test -d /proc/<pid>` checks** are not affected by SSH user. They check the kernel's view of pid
  existence, which is independent of who is asking. Under `hidepid=1` they continue to work
  cross-uid for the directory-existence case (R5).
- **First restart** of any session (manual `agw session restart` or batch `--all-stopped`) flows
  through the new code path. The session ends up identical in shape, just spawned via direct
  target-user SSH.

## Touch points by file

| File                                                                             | Change                                                                                                                         |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `agentworks/ssh.py`                                                              | New `agent_exec_target(vm, config, agent)`; shared internal builder under both wrappers                                        |
| `agentworks/ssh_config.py`                                                       | Per-agent SSH config block writer / refresher / remover (mirrors admin block)                                                  |
| `agentworks/vms/initializer.py`                                                  | Extend `_reconcile_authorized_keys` with `owner=` (stage-and-install path); hidepid=1 fstab; sysctl.d file; idempotent reapply |
| `agentworks/agents/manager.py:_create_agent_on_vm`                               | Call `_reconcile_authorized_keys` with `owner=agent.linux_user`; write operator SSH config block                               |
| `agentworks/agents/manager.py:reinit_agent`                                      | Same path as create (declarative parity)                                                                                       |
| `agentworks/agents/manager.py:delete_agent`                                      | Remove operator SSH config block                                                                                               |
| `agentworks/agents/manager.py:shell_agent`                                       | Drop sudo branch; use `agent_exec_target` + interactive                                                                        |
| `agentworks/sessions/tmux.py:create_session` (agent branch)                      | Drop `sudo --login -u <agent>` prefix; agent runs tmux as itself                                                               |
| `agentworks/sessions/manager.py`                                                 | Pass agent identity through to tmux create where needed                                                                        |
| `agentworks/sessions/manager.py:_pid_alive`, batch status compound               | Verified, no change (admin's pid check works cross-uid under hidepid=1; see R5)                                                |
| `agentworks/sessions/tmux.py:force_kill_tmux_server` (two `test -d /proc/<pid>`) | Verified, no change (same)                                                                                                     |

The set of files touched is small. Each change is local and reviewable on its own.

## Interaction with other SDDs

### 2026-04-10 agent-tmux-sockets

Unchanged. The cross-user socket access model that admin uses to attach to agent tmux servers
remains intact. This SDD changes the SSH user for create/restart operations only.

### 2026-06-05 env-and-secrets

Precondition for env-and-secrets. After this SDD lands, agent operations no longer cross a sudo
boundary, which simplifies any future transport that needs to carry environment material into the
agent's shell. The env-and-secrets HLA will reference this SDD's three-mode framing as its baseline
access model. Specifics of env-and-secrets are out of scope here.

## Design decisions

### Direct target-user SSH is the simpler invariant

"SSH as the target user" is a single rule that covers every shell-opening surface. The previous
admin+sudo pattern carried a per-call decision ("is this an agent operation? if so, sudo --login")
that leaked into every shell-opening site. Codifying direct target-user SSH as the rule removes that
branch.

### Sockets stay

The 2026-04-10 work that gave admin reach into agent tmux servers via group permissions is still
load-bearing for batch read operations (`session list`, status checks across all agents on a VM in
one SSH call) and for attach UX. Replacing it would require fan-out SSH (one per agent), which
regresses latency for the most-used command. Direct target-user SSH is the _write_ path; sockets
remain the _read_ path. Both coexist cleanly.

### Authorized keys via the existing reconciliation helper

`_reconcile_authorized_keys` is already generic over `home`. Reusing it for agents avoids two
parallel implementations of the same idea. The header-warning convention, mode 0600 write, and
full-overwrite semantics all carry over.

### `hidepid=1` over `hidepid=2`

Mode 1 closes the cross-uid argv leak (`/proc/<pid>/cmdline` no longer readable by non-owners),
which is the threat. Mode 2 adds pid-existence hiding, which is not in the threat model and risks
breaking tools that enumerate `/proc`. The `gid=proc` group workaround for mode 2 is real but adds
surface area (the group, who is in it, when system users need access) that mode 1 avoids.

### Sysctl file separate from fstab

`/etc/sysctl.d/99-agentworks.conf` is the right place for sysctls; `/etc/fstab` is the right place
for the `/proc` mount option. Keeping them in their conventional homes makes the configuration
discoverable and easy to inspect.
