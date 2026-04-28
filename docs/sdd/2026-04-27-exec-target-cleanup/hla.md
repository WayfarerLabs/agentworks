# Provisioner shell and exec model cleanup -- high-level architecture

## Renames

| Before | After | Rationale |
|--------|-------|-----------|
| `VMProvisioner.exec_target()` | `admin_exec_target()` | Clarifies it returns a shell for the admin user. Leaves room for `agent_exec_target()` in the future. |

## ExecTarget after cleanup

```python
class ExecTarget:
    ssh: SSHTarget | None = None
    lima: LimaTarget | None = None
    remote_lima: RemoteLimaTarget | None = None
    wsl2: WSL2Target | None = None
    default_timeout: int | None = None
    logger: SSHLogger | None = None

    def run(self, command, *, sudo=False, tty=None, check=True, timeout=None) -> SSHResult:
        """Run a command on the target.

        sudo: wrap in sudo -n bash -c '...' (entire command runs as root).
        tty:  None = transport default, True = request TTY, False = suppress TTY.
        """
```

### Single method, clear parameters

There is no separate `sudo_run` or `run_as_root`. The `sudo` flag on `run()` handles root
escalation. The `tty` flag handles TTY allocation. Logging is built into `run()` via the
`logger` field on ExecTarget -- there is no `_run_logged` helper.

Before:
```python
_run_logged(target, "apt-get update", logger, as_root=True)
_run_logged(target, "tailscale down && tailscale logout", logger, as_root=True)
target.run_as_root("nohup /bin/bash script.sh &")
target.run("echo hello")
```

After:
```python
target.run("apt-get update", sudo=True)
target.run("tailscale down && tailscale logout", sudo=True)
target.run("/bin/bash script.sh", sudo=True, tty=False)  # detached pattern
target.run("echo hello")
```

### sudo implementation

When `sudo=True`, `run()` wraps the command:

```python
if sudo:
    command = f"sudo -n bash -c {shlex.quote(command)}"
```

This ensures compound commands (`cmd1 && cmd2`) run entirely as root. The `bash -c` wrapper
eliminates the long-standing bug where only the first command in a chain got root privileges.

### TTY resolution (SSH transport)

```text
tty=True   -> always add -tt
tty=False  -> never add -tt (overrides force_tty)
tty=None   -> add -tt only if SSHTarget.force_tty is True
```

Other transports ignore the tty parameter. `force_tty` stays on SSHTarget as a transport-level
default for the Windows quirk. It is not exposed in the `run()` API and can be removed later
without caller changes.

### Logging

ExecTarget already has a `logger: SSHLogger | None` field. The transport-level run functions
already call `logger.log_command()` when a logger is present. This means `run()` logs
automatically -- the `_run_logged` helper in initializer.py is redundant and will be deleted.

The initializer already attaches the logger to the ExecTarget at construction time
(`replace(exec_target, logger=logger)`), so all commands through that target are logged.

### What goes away

| Removed | Replacement |
|---------|-------------|
| `ExecTarget.run_as_root()` | `run(command, sudo=True)` |
| `_run_logged()` in initializer.py | `target.run()` (logging built in) |
| `_run_logged(..., as_root=True)` | `target.run(command, sudo=True)` |
| Standalone `run_as_root()` function in ssh.py | `run(command, sudo=True)` |
| Manual `sudo -n` in command strings | `run(command, sudo=True)` |
| `force_tty=False` target replacement in run_detached | `run(command, tty=False)` |
| WSL2 `--user root` special case | `sudo -n bash -c` (same as all transports) |

### Migration strategy: run_new

To avoid a big-bang refactor:

1. Add `run_new()` with the clean signature (sudo, tty, logging).
2. Migrate call sites incrementally (run, run_as_root, _run_logged -> run_new).
3. Delete old methods (run, run_as_root, _run_logged, standalone run_as_root).
4. Rename `run_new()` -> `run()`.

Tests pass at every step. Old and new coexist during migration.

## Proxmox admin_exec_target

Proxmox's `admin_exec_target()` currently returns:

```python
ExecTarget(ssh=SSHTarget(host=vm.tailscale_host, ...))  # Wrong: Tailscale
```

Should return a guest-agent-backed ExecTarget. Until implemented, raises `NotImplementedError`.

The guest agent integration is straightforward: `ProxmoxAPIClient` already has
`guest_agent_exec_wait(node, vmid, command, timeout)` which returns exitcode + stdout + stderr.
This maps cleanly to `SSHResult`. Future work: add a `ProxmoxAgentTarget` to ExecTarget or
wrap the API client in a callable that fits the existing dispatch pattern.

## VM rekey flow

```text
agentworks vm rekey <name> [--wait-for-share]

1. Look up VM, validate running
2. Get provisioner, get admin_exec_target (provisioning shell)
3. For Azure: attach public IP
4. Collect new auth key (env var or prompt)
5. target.run("tailscale down && tailscale logout", sudo=True)
6. target.run("tailscale up --auth-key <key>", sudo=True)
7. result = target.run("tailscale ip -4", sudo=True)
8. new_ip = result.stdout.strip()
9. If --wait-for-share:
   a. Print new IP, prompt operator to share VM
   b. Wait for Enter
   c. Verify SSH connectivity to new_ip
10. db.update_vm_tailscale(vm_name, new_ip)
11. sync_ssh_config()
12. For Azure: detach public IP
```

Uses `provisioner.admin_exec_target()` -- the same abstraction used by `_tailscale_logout` and
`_ensure_tailscale`. No new provisioner method needed.

## Deferred: shell consistency

The transports currently use different shells: SSH passes commands raw (interpreted by the
remote user's default shell), while Lima and WSL2 explicitly use `bash -lc`. Non-interactive
agentworks operations assume bash semantics but don't enforce it on SSH.

This is not addressed in this cleanup. Some install commands detect the running shell and only
configure that shell, so changing shell semantics would introduce subtle inconsistencies. This
is a separate concern to address deliberately.
