# Provisioner shell and exec model cleanup -- functional requirements

## Background

Agentworks communicates with VMs over two distinct transports:

1. **Tailscale SSH**: The primary path for initialization and day-to-day operations. Requires the VM
   to be joined to a Tailscale tailnet.
2. **Provisioning transport**: The out-of-band path native to each platform (limactl shell, WSL2
   command, Azure public IP SSH, Proxmox guest agent). Works without Tailscale.

The provisioning transport is exposed via `VMProvisioner.exec_target()`, which returns an
`ExecTarget`. Callers use `run()` / `run_as_root()` on the target. This model is sound but has
accumulated several issues:

### Issue 0: Naming doesn't convey intent

`exec_target()` doesn't communicate that it returns a shell for the admin user. As we add more
execution contexts (e.g., future `agent_exec_target()` for direct-to-agent execution), the generic
name becomes ambiguous. Similarly, `run_as_root()` hides the mechanism -- it's just `sudo -n`
under the hood, and `sudo_run()` says that directly.

### Issue 1: Proxmox exec_target returns the wrong transport

Proxmox's `exec_target()` returns a Tailscale SSH target instead of the native provisioning
transport (QEMU guest agent). Operations that need the provisioning shell (Tailscale logout, rekey)
silently use the wrong transport on Proxmox.

### Issue 2: run_as_root has inconsistent per-transport implementations

`ExecTarget.run_as_root()` dispatches to four different implementations: SSH wraps with `sudo -n`,
Lima wraps with `sudo -n`, Remote Lima wraps with `sudo -n`, and WSL2 switches to `--user root`.
But `run_as_root` is conceptually just `run("sudo -n " + command)`. There is no reason for
per-transport dispatch -- it should be a thin wrapper over `run()`.

### Issue 2a: Naive sudo prepend breaks compound commands

`run_as_root("cmd1 && cmd2")` becomes `sudo -n cmd1 && cmd2`, where only `cmd1` runs as root.
This has caused repeated bugs. The fix is to wrap via `sudo -n bash -c '<command>'` so the
entire compound command runs in a root shell. `shlex.quote` handles escaping.

Additionally, several call sites bypass `run_as_root` entirely and manually prepend `sudo` to
commands passed to `run()`. These should migrate to `sudo_run()` for consistency and to benefit
from the `bash -c` wrapping. Known instances:

- `initializer.py`: `sudo -n /bin/bash`, `sudo chown -R`
- `azure.py`: `sudo tailscale ip -4`
- `tmux.py`: `sudo -n tmux`, `sudo rm -f`, `sudo mkdir -p`, `sudo tee`
- `sessions/manager.py`: `sudo rm -f`

Note: `sudo -u <user>` (agent user switching in tmux.py) is NOT the same as `sudo_run` and
should remain as-is -- it switches to a non-root user, not to root.

### Issue 3: TTY control is at the wrong level

The `force_tty` flag lives on `SSHTarget` (transport-level), meaning every command through that
target gets the same TTY behavior. This caused problems with `run_detached` where nohup'd commands
were killed by SIGHUP from the PTY close. The workaround creates a new target with
`force_tty=False` for each call -- clunky and error-prone.

TTY allocation should be controllable per-call. The caller's intent ("I need a TTY" or "no TTY
please") is separate from the transport's platform quirk ("Windows SSH needs -tt by default").

### Motivating use case: Tailscale rekey

The operator needs to switch a VM from one Tailscale tailnet to another (e.g., personal to work).
This requires running `tailscale logout` followed by `tailscale up --authkey=<new-key>` on the VM.
Tailscale SSH is unavailable between logout and re-join, so the entire operation must use the
provisioning transport. There is no existing CLI command for this.

## Requirements

### R1: Rename exec_target -> admin_exec_target

`VMProvisioner.exec_target()` -> `admin_exec_target()`: clarifies that this returns a shell for
the admin user, leaves room for `agent_exec_target()` in the future. Propagates to all
provisioner implementations and call sites.

### R2: Proxmox admin_exec_target returns the provisioning transport

Proxmox's `admin_exec_target()` must return an ExecTarget that uses the QEMU guest agent, not
Tailscale SSH. Until the guest agent ExecTarget variant is implemented, it should raise a clear
error. This is a bug fix, not a new feature.

### R3: Single run() method with sudo and tty parameters

`ExecTarget.run()` accepts `sudo` and `tty` parameters:

```python
target.run(command, sudo=False, tty=None, check=True, timeout=None)
```

When `sudo=True`, the command is wrapped in `sudo -n bash -c '...'` via `shlex.quote`. This
ensures compound commands (`cmd1 && cmd2`) run entirely as root. No per-transport dispatch for
sudo -- all transports use the same mechanism. The WSL2 `--user root` special case is removed.

`ExecTarget.run_as_root()` is eliminated. `_run_logged()` in initializer.py is eliminated
(logging is already built into `run()` via the `logger` field on ExecTarget). All call sites
that manually prepend `sudo` to command strings migrate to `sudo=True`.

The exception is `sudo -u <user>` for agent user switching in tmux.py, which is a different
operation (switching to a non-root user) and remains as-is.

### R4: TTY control at the run() call level

`run()` accepts a `tty` parameter with three-state semantics:

- `tty=None` (default): use the transport's default behavior. For SSH, this respects the
  `force_tty` flag on SSHTarget. For other transports, no TTY.
- `tty=True`: request a TTY. The transport allocates one if it can.
- `tty=False`: suppress TTY. Overrides `force_tty` on the transport. Used by `run_detached` and
  other non-interactive operations that must not have a PTY.

The `force_tty` flag remains on SSHTarget as a transport-level default (Windows workaround) but is
no longer the only way to control TTY allocation. It can be removed later if the Windows quirk
turns out to be unnecessary.

### R5: VM rekey command

A new `agentworks vm rekey` command assigns a new Tailscale auth key to a VM. This is useful for
rotating keys, switching tailnets, or recovering from expired ephemeral keys.

- Accepts a VM name and optional `--wait-for-share` flag.
- Sources the new Tailscale auth key from `TAILSCALE_AUTH_KEY` env var or interactive prompt (same
  pattern as `vm create`).
- Runs `tailscale down && tailscale logout` via the provisioner shell.
- Runs `tailscale up --authkey=<key>` via the provisioner shell.
- Reads the new Tailscale IP via `tailscale ip -4`.
- Updates the DB and SSH config with the new IP.
- With `--wait-for-share`: pauses for the operator to share the VM back to their tailnet, then
  verifies SSH connectivity before updating the DB.
