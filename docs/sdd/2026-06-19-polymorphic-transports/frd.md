# Polymorphic transports -- functional requirements

## Background

Agentworks reaches VMs over two distinct channels:

1. **The canonical transport**: Tailscale SSH. Used by every operator-facing operation (shell, exec,
   port-forward, workspace and agent commands, sessions). Same shape across every VM platform.
2. **The provisioner transport**: each platform's native channel (`limactl shell` for Lima, SSH to
   the VM host plus `limactl shell` for remote Lima, `wsl.exe` for WSL2, SSH via a
   temporarily-attached public IP for Azure, the QEMU guest agent for Proxmox -- currently not
   implemented). Used at bootstrap (Phase A) and via the explicit operator opt-in
   `agw vm shell --provisioner`.

The current shape collapses both into a single `ExecTarget` dataclass with four mutually-exclusive
transport fields. Every operation (`run`, `interactive`, `run_as_root`, `copy_to`, `copy_dir_to`)
does a union dispatch on which field is set:

```python
@dataclass(frozen=True)
class ExecTarget:
    ssh: SSHTarget | None = None
    lima: LimaTarget | None = None
    remote_lima: RemoteLimaTarget | None = None
    wsl2: WSL2Target | None = None
    ...

if target.ssh is not None:
    ...
elif target.lima is not None:
    return _lima_xxx(...)
elif target.remote_lima is not None:
    return _remote_lima_xxx(...)
elif target.wsl2 is not None:
    return _wsl2_xxx(...)
```

The four union dispatches each have their own subtly-different per-transport call (e.g. SSH gets
`SetEnv`, Lima uses a `bash -lc` env prefix, WSL2 uses `wsl --user`, RemoteLima needs an
`$SHELL -lc` wrapping to find Homebrew binaries). All of this lives in `cli/agentworks/ssh.py`
despite three of the four transports having nothing to do with SSH.

A prior cleanup ([2026-04-27-exec-target-cleanup](../2026-04-27-exec-target-cleanup/), now locked)
normalized the within-shape exec model: it unified `run()` with `sudo` and `tty` parameters, renamed
`exec_target` to `admin_exec_target`, and stubbed Proxmox. It deliberately did not address the
union-dispatch shape itself. This SDD is that follow-on.

### Symptoms

The shape produced repeated bugs as we extended it. Two examples from PR #118 (the cold-boot DNS
race fix) alone:

1. `interactive()` was hardwired to SSH via `_unwrap_ssh()` plus an assertion. When PR #118 added
   `agw vm shell --provisioner`, the assertion fired for every non-SSH target:
   `AssertionError: ExecTarget has no SSH target`.
2. `_remote_lima_interactive` missed the login-shell wrapping that `remote_lima_run` already had.
   `limactl` wasn't on the SSH default non-login PATH on macOS (Homebrew at `/opt/homebrew/bin`).
   The fix was identical to a fix already applied to the non-interactive sibling -- the duplication
   wasn't visible until an operator tripped it.

Each new dispatch site is a new opportunity to forget one branch. Adding a transport (e.g., Proxmox
guest-agent exec, currently `NotImplementedError`) means touching every dispatch site instead of
adding one file.

## Requirements

### R1: Transport is a polymorphic ABC

Define a `Transport` abstract base class with the operator's I/O surface (command exec + file
movement) as abstract methods. Each platform's transport is a concrete subclass. The current union
dispatch in `ExecTarget` is replaced with virtual method calls on a `Transport` instance.

`Transport` is the right level of generality: the surface covers the operator's I/O channel to the
VM (both shell-style command exec and file movement) because in practice every provisioner gives
both, sharing a delivery mechanism per transport (SSH carries scp; `limactl shell` pairs with
`limactl copy`; wsl.exe carries both).

### R2: Two named factories, no auto-pick

Two explicit factory functions return a `Transport` for a VM:

- `transport(vm, config) -> Transport` -- the canonical path (Tailscale SSH today, may be something
  else tomorrow). Used by every normal operator workflow. Raises a typed error if the canonical
  transport is unavailable for this VM. **Never** falls back to the provisioner transport.
- `provisioner_transport(vm, config) -> Transport` -- the platform-native path. Used only where it's
  structurally required (bootstrap; `vm shell --provisioner`). The operator explicitly opts in.

The pairing makes the binary choice clear: one canonical transport, one explicit opt-in exception.
No "smart" function decides at runtime which to use.

### R3: No automatic failover, ever

This is a critical non-goal. Code that uses the canonical transport must not silently fall back to
the provisioner transport on failure. Reasoning:

- The provisioner transport has different (often broader) blast radius. Azure attaches a temporary
  public IP. RemoteLima tunnels through the operator's SSH session to the VM host. Silently falling
  through changes the security/operational properties of an operation without operator opt-in.
- Failover obscures problems. If the canonical transport isn't working, the operator needs to know
  and fix it -- not have agentworks paper over it and let the underlying issue drift.
- Consistency: operations that work over the canonical transport should fail if the canonical
  transport doesn't work. Operators learn one transport.

A test pins this invariant: when `transport(vm, config)` raises, the function does not call
`provisioner_transport(vm, config)`.

### R4: One module per transport

File layout under `agentworks/transports/`:

- `__init__.py` -- exposes the `Transport` ABC and both factory functions.
- `ssh.py` -- `SSHTransport` plus SSH-specific argv / `SetEnv` / login-shell helpers.
- `lima.py` -- `LimaTransport`.
- `remote_lima.py` -- `RemoteLimaTransport`.
- `wsl2.py` -- `WSL2Transport`.

The current `cli/agentworks/ssh.py` is either deleted or shrunk to genuinely SSH-specific
non-Transport code (e.g. ssh-config-file management, if any remains).

### R5: ExecTarget removed

The `ExecTarget` dataclass and its union-dispatch methods are deleted. Type hints across the
codebase update to `Transport`. The naming collision between `agentworks.ssh.admin_exec_target` and
`VMProvisioner.admin_exec_target` resolves on its own because both functions go away: the former
becomes `transport()`, the latter becomes an internal implementation detail of
`provisioner_transport()`.

### R6: No operator-visible behavior change

This refactor is structural. Operator-visible behavior (CLI surface, command outputs, error
messages, performance characteristics) is identical pre- and post-refactor. The CLI command set is
unchanged. The error rendering is unchanged. The retry semantics on each operation are preserved.

### R7: Tests adapted

Each concrete Transport is tested in isolation against the ABC contract. The no-failover invariant
(R3) is tested explicitly. Existing functional tests for affected callers continue to pass.

## Out of scope

- Implementing a Proxmox Transport. The Proxmox provisioner shell stays as a typed `StateError`
  pointing at the web UI's serial console; this SDD doesn't change that.
- Changing the operator-facing CLI surface in any way.
- Adding a new transport variant or platform.
- Adding the env-injection shim for non-SSH transports (currently dropped on Lima/WSL2/ RemoteLima
  for the provisioner-shell path; documented in `ssh.py` and unchanged by this refactor).
