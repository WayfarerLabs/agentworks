# Direct user SSH access -- high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

Three architectural changes, each independently small but composing into a cleaner access surface:

1. **Access modes codified.** A small helper builds an SSH target keyed on subject identity (admin
   or agent's Linux user). All shell-opening sites use this; the `sudo --login` detour disappears.
2. **Agent authorized_keys integrated into the existing reconciliation path.** The current
   `_reconcile_authorized_keys` helper is generic over `home`; agent create / reinit call it with
   the agent's home dir.
3. **VM hardening at init.** `hidepid=1` on `/proc` via `/etc/fstab`; an opinionated sysctl set in
   `/etc/sysctl.d/99-agentworks.conf`. Applied at `vm create`, reapplied at `vm reinit`.

Existing tmux socket / `server-access` infrastructure for cross-user attach is unchanged.

## Access mode plumbing

### Subject-keyed SSH target builder

A small extension to the existing SSH target machinery in `agentworks.ssh`:

```python
def ssh_target_for_subject(
    vm: VMRow,
    config: Config,
    *,
    subject: str,  # admin username or agent linux_user
    interactive: bool = False,
) -> SSHTarget:
    """Build an SSH target for a specific Linux user on the VM."""
    ...
```

- `subject` chooses the SSH user. Admin operations pass `vm.admin_username`; agent operations pass
  `agent.linux_user`.
- `interactive=True` flips to the `-t` form (used by Mode 2 surfaces). Default is non-interactive
  (Modes 1 and 3, plus all `target.run(...)` plumbing).
- All other options (identity file, proxy jump, Tailscale host) are derived as today.

The existing `admin_exec_target(vm, config)` becomes a thin wrapper that calls
`ssh_target_for_subject(vm, config, subject=vm.admin_username)`. A parallel
`agent_exec_target(vm, config, agent)` calls it with the agent's user. Call sites pick one or the
other; both produce the same `ExecTarget` type, so downstream code is identical.

### Mode 1: provisioning (admin only)

Unchanged from today: `admin_exec_target(vm, config)`, `target.run(...)` for scripts (stdin will
carry the script body once the env-and-secrets SDD lands; this SDD doesn't change the transport).

### Mode 2: interactive shells

Surfaces: `vm shell`, `workspace shell`, `agent shell`, `workspace console`.

```python
# vm shell: admin
target = admin_exec_target(vm, config)
interactive(target)

# agent shell: agent
target = agent_exec_target(vm, config, agent)
interactive(target)  # ssh -t <agent-user>@vm with no command -- login shell
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

# agent-mode session: agent user (NEW -- no more sudo --login)
target = agent_exec_target(vm, config, agent)
target.run(f"tmux -S {q_sock} new-session -d -s {q_session} ...")
```

The `sudo --login -u <agent> tmux ...` form at `sessions/tmux.py:336-340` is replaced. The tmux
server, the panes inside it, and any commands they spawn all run as the agent uid -- same end state
as today, reached via one less indirection.

The socket-permission tweak (`chmod g+rwx <sock>`) that previously ran via admin's sudo path now
runs as the agent (the socket's owner). Same effect, no sudo needed.

## Authorized keys for agents

### What changes

`vms/initializer._reconcile_authorized_keys` already accepts a `home` parameter and is otherwise
user-agnostic. The change is to invoke it from agent-side flows:

```python
# agents/manager._create_agent_on_vm (new step, after user creation)
_reconcile_authorized_keys(
    target,        # admin_exec_target with sudo capability for writing into agent's home
    config,
    home=f"/home/{agent.linux_user}",
    logger=ssh_logger,
)

# agents/manager.reinit_agent (new step)
# same call as above
```

Writing into the agent's home dir is done by admin via sudo'd write -- agent doesn't have SSH access
yet at create time, so the bootstrap relies on admin doing the initial setup. Subsequent reinit can
either continue using admin's sudo path (same as today's admin-keys reconciliation) or switch to
writing as the agent once SSH is established. Admin-via-sudo is simpler and consistent with how
every other agent-side artifact gets written today.

### Permissions

The agent's `~/.ssh/` directory is mode 0700, owned by the agent. The `authorized_keys` file is mode
0600, owned by the agent. `_reconcile_authorized_keys`'s `target.write_file(..., mode="600")` call
handles the mode; ownership comes from the create path that established the home dir.

### Existing operator key rotation flow

`agentworks doctor`-style or `vm reinit` flows that re-sync admin's keys today get an additive
operation: walk all agents for that VM and re-sync each. This is a small loop in `vm reinit`'s
post-init pass.

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

### Compatibility with existing pid checks

Per the FRD R5, the four call sites doing `test -d /proc/<pid>` are expected to continue working
cross-uid under `hidepid=1` because mode 1 restricts access to _files inside_ `/proc/<pid>/`, not
the directory entry itself. Plan phase 1 is to verify this empirically before any other work, with a
fall-back to sudo if the assumption turns out to be wrong.

## Migration semantics

No active migration is needed:

- **Running tmux servers on agent uids** persist across the code change. They were started by the
  previous `sudo --login -u agent tmux ...` path, run as the agent uid, and don't care how they were
  spawned.
- **Sockets and `server-access` ACLs** are persistent state on the VM, established at create. The
  group permissions admin uses to attach are unaffected by the SSH-user change for create/restart
  operations.
- **`test -d /proc/<pid>` checks** are not affected by SSH user -- they check the kernel's view of
  pid existence, which is independent of who's asking. Under `hidepid=1` they continue to work
  cross-uid for the directory-existence case (R5).
- **First restart** of any session (manual `agw session restart` or batch `--all-stopped`) flows
  through the new code path. The session ends up identical in shape, just spawned via direct-user
  SSH.

## Touch points by file

| File                                                        | Change                                                    |
| ----------------------------------------------------------- | --------------------------------------------------------- |
| `agentworks/ssh.py`                                         | `ssh_target_for_subject(...)` helper; `agent_exec_target` |
| `agentworks/agents/manager.py:_create_agent_on_vm`          | Call `_reconcile_authorized_keys` for agent's home        |
| `agentworks/agents/manager.py:reinit_agent`                 | Same -- pick up key rotation                              |
| `agentworks/agents/manager.py:shell_agent`                  | Drop sudo branch; use `agent_exec_target` + `interactive` |
| `agentworks/sessions/tmux.py:create_session` (agent branch) | Drop `sudo --login -u <agent>` prefix                     |
| `agentworks/sessions/manager.py`                            | Pass agent identity through to tmux create where needed   |
| `agentworks/vms/manager.py:reinit_vm`                       | Walk agents; reconcile their authorized_keys              |
| `agentworks/vms/initializer.py`                             | hidepid=1 fstab entry; sysctl.d file; apply on reinit     |

The set of files touched is small. Each change is local and reviewable on its own.

## Interaction with other SDDs

### 2026-04-10 agent-tmux-sockets

Unchanged. The cross-user socket access model that admin uses to attach to agent tmux servers
remains intact. This SDD changes the SSH user for create/restart operations only.

### 2026-06-05 env-and-secrets

This SDD is a precondition. The env-and-secrets HLA assumes the new access model:

- Mode 3 agent sessions no longer have a sudo boundary stripping env, so the stdin script prefix (or
  the eventual forwarded-socket transport) propagates naturally.
- Mode 2 interactive shells SSH as the subject directly; the env-injection mechanism applies
  uniformly to admin and agent shells without per-mode branching.

The env-and-secrets HLA will be updated to reference this SDD's three-mode framing.

## Design decisions

### Direct-user SSH is the simpler invariant

"SSH as the subject" is a single rule that covers every shell-opening surface. The previous
admin+sudo pattern carried a per-call decision ("is this an agent operation? if so, sudo --login")
that leaked into every shell-opening site. Codifying direct-user as the rule removes that branch.

### Sockets stay

The 2026-04-10 work that gave admin reach into agent tmux servers via group permissions is still
load-bearing for batch read operations (`session list`, status checks across all agents on a VM in
one SSH call) and for attach UX. Replacing it would require fan-out SSH (one per agent), which
regresses latency for the most-used command. Direct-user SSH is the _write_ path; sockets remain the
_read_ path. Both coexist cleanly.

### Authorized keys via the existing reconciliation helper

`_reconcile_authorized_keys` is already generic over `home`. Reusing it for agents avoids two
parallel implementations of the same idea. The header-warning convention, mode 0600 write, and
full-overwrite semantics all carry over.

### `hidepid=1` over `hidepid=2`

Mode 1 closes the cross-uid argv leak (`/proc/<pid>/cmdline` no longer readable by non-owners),
which is the threat. Mode 2 adds pid-existence hiding, which is not in the threat model and risks
breaking tools that enumerate `/proc`. The `gid=proc` group workaround for mode 2 is real but adds
surface area (the group, who's in it, when system users need access) that mode 1 avoids.

### Sysctl file separate from fstab

`/etc/sysctl.d/99-agentworks.conf` is the right place for sysctls; `/etc/fstab` is the right place
for the `/proc` mount option. Keeping them in their conventional homes makes the configuration
discoverable and easy to inspect.
