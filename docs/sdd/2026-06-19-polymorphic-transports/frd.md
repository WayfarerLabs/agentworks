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

### R2: Three named factories, no auto-pick

Three explicit factory functions return a `Transport` for a VM:

- `transport(vm, config) -> Transport` -- the canonical admin path (Tailscale SSH today, may be
  something else tomorrow). Used by every normal operator workflow on the admin user. Raises a typed
  error if the canonical transport is unavailable for this VM. **Never** falls back to the
  provisioner transport.
- `agent_transport(vm, config, agent) -> Transport` -- the canonical path for a named agent's Linux
  user. Same transport mechanism as `transport()` (Tailscale SSH), different SSH user. Used by every
  operator-facing operation that targets an agent (agent shell, agent exec, session operations on
  agent-mode sessions).
- `provisioner_transport(vm, config, *, stack) -> Transport` -- the platform-native path. Used only
  where it's structurally required (bootstrap; `vm shell --provisioner`). The operator explicitly
  opts in.

Plus one shared low-level helper for the rare case where a Linux username is known but no agent row
exists yet (today's `exec_target_for_user`, called once during agent creation):
`transport_for_user(vm, config, *, user, identity_file=None, logger=None) -> Transport`. The two
admin/agent factories are thin wrappers over this. Most code uses the named factories;
`transport_for_user` is only for the mid-create exception.

The pairing makes the binary choice clear: canonical transport for the operator's normal work
(targeting admin or a specific agent), one explicit opt-in exception for platform-native access. No
"smart" function decides at runtime which to use.

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

A test pins this invariant: when `transport(vm, config)` or `agent_transport(vm, config, agent)`
raises, the calling function does not call `provisioner_transport(...)`.

One acknowledged carve-out: the `_on_tailscale_ready` callback inside `create_vm` keeps a direct
`AzureProvisioner.detach_public_ip` call (rather than routing through `transient_route`). The attach
happens inside `provisioner.create()` because Azure needs the IP to drive cloud-init bootstrap, and
the matching detach needs to fire at the asynchronous Tailscale-ready point inside `initialize_vm`.
That isn't an ExitStack-shaped lifecycle, so the polymorphic hook doesn't fit cleanly. A future
refactor could expose `VMProvisioner.post_tailscale_ready(vm)`; this SDD doesn't take that on.

### R4: One module per transport

File layout under `agentworks/transports/`:

- `__init__.py` -- exposes the `Transport` ABC (re-exported from `base.py`), the three factory
  functions (`transport`, `agent_transport`, `provisioner_transport`), the `transport_for_user`
  low-level helper, and `wait_for_reconnect`.
- `base.py` -- the `Transport` ABC definition.
- `ssh.py` -- `SSHTransport` plus SSH-specific argv / `SetEnv` / login-shell helpers.
- `lima.py` -- `LimaTransport`.
- `remote_lima.py` -- `RemoteLimaTransport`.
- `wsl2.py` -- `WSL2Transport`.

The current `cli/agentworks/ssh.py` is shrunk to genuinely SSH-specific non-Transport code that
other code shares: `SSHLogger`, `SSHResult` (preserved name), and any standalone SSH helpers not
absorbed by `SSHTransport`.

### R5: ExecTarget removed; `_unwrap_ssh` shim deleted

The `ExecTarget` dataclass and its union-dispatch methods are deleted. Type hints across the
codebase update to `Transport`. The naming collision between `agentworks.ssh.admin_exec_target` and
`VMProvisioner.admin_exec_target` resolves on its own because both functions go away: the former
becomes `transport()`, the latter becomes an internal implementation detail of
`provisioner_transport()`.

The `_unwrap_ssh` shim is also deleted. Its only external caller today is
`cli/agentworks/vms/backup.py`, which uses it to obtain a raw `SSHTarget` for driving `scp` from the
local box. Post-refactor, `backup.py` consumes the polymorphic
`Transport.copy_from(remote_path, local_path)` method (newly added to the ABC, see R1's surface);
the underlying implementation for SSH is the same scp call wrapped behind the polymorphic surface,
so backup becomes platform-agnostic for free. No `_unwrap_ssh` callers remain.

### R6: No operator-visible behavior change (except misleading error text)

This refactor is structural. Operator-visible behavior is preserved pre- and post-refactor with one
explicit carve-out:

- **Preserved**: CLI surface, command behavior, exception classes, returncodes, retry semantics,
  performance characteristics.
- **May be improved**: error message text in cases where the current text is misleading. The
  canonical example is `SSHError("Lima command failed ...")` raised by Lima failures: the message
  correctly names the platform but the exception class names SSH. Polymorphic transports give each
  platform a natural home to raise from, and improving the wording while the code is being moved is
  cheaper than a follow-up sweep.
- **May be improved**: asserts that validate caller-supplied state at factory or ABC entry points
  get promoted to typed errors. Today's `assert vm.tailscale_host is not None` in
  `exec_target_for_user` becomes a typed `StateError` in the new `transport_for_user` factory. The
  assert disappears under `python -O`; the typed error doesn't, and surfaces a clean operator
  message via the existing CLI error wrapper rather than an `AssertionError` traceback. Scope-bound:
  this carve-out applies to entry-point asserts only. Asserts inside transport implementations (e.g.
  defensive postconditions in `SSHTransport.run` after we expect an invariant to hold) stay as
  asserts; they encode programmer-internal invariants that should fail loudly during development.

The carve-outs are bounded: each change to error text or assert-to-typed promotion must be called
out in the PR description so the reviewer can confirm each change improves rather than obscures.

### R7: Tests adapted

Each concrete Transport is tested in isolation against the ABC contract. The no-failover invariant
(R3) is tested explicitly. The polymorphic `copy_from` (new on the ABC, see R1) is tested
per-transport. Existing functional tests for affected callers continue to pass.

## Acceptance criteria from issue #128

Mapped against this SDD's sections:

| Acceptance criterion (issue #128)                            | Addressed by        |
| ------------------------------------------------------------ | ------------------- |
| One module per transport under `agentworks/transports/`      | R4, HLA layout      |
| `Transport` Protocol or ABC (locked to ABC)                  | R1, HLA             |
| Explicit factory functions; naming collision resolved        | R2, R5              |
| No automatic failover; code review catches attempts          | R3                  |
| Tests per transport in isolation; failover-prevention tested | R7, plan Phase 1+2  |
| No behavioral change visible to operators                    | R6 (with carve-out) |

## Out of scope

- Implementing a Proxmox Transport. The Proxmox provisioner shell stays as a typed `StateError`
  pointing at the web UI's serial console; this SDD doesn't change that.
- Changing the operator-facing CLI surface in any way.
- Adding a new transport variant or platform.
- Adding the env-injection shim for non-SSH transports (currently dropped on Lima / WSL2 /
  RemoteLima for the provisioner-shell path; documented in `ssh.py` and unchanged by this refactor).
- Renaming `SSHResult` to `TransportResult`. The reviewer suggested this is the time for the churn;
  we've chosen to bound scope and keep the existing name. A short comment at the `SSHResult`
  definition will explain the post-refactor reading.
