# Polymorphic transports -- high-level architecture

## The Transport ABC

Lives in `agentworks/transports/base.py`. The package `__init__.py` re-exports it for
`from agentworks.transports import Transport`. The ABC is the full operation surface every transport
supports; helpers built on top (`wait_for_reconnect`, typically) stay as module-level functions
taking a `Transport`.

```python
# agentworks/transports/base.py

class Transport(abc.ABC):
    """Operator I/O channel to a VM: command exec and file movement.

    Each concrete subclass implements the surface for one delivery mechanism
    (SSH, limactl shell, wsl.exe, etc.). Callers obtain a Transport via the
    factory functions exported from this package: ``transport(vm, config)``
    for the canonical admin path, ``agent_transport(vm, config, agent)`` for
    the canonical agent path, ``provisioner_transport(vm, config, *, stack)``
    for the platform-native opt-in.
    """

    @abc.abstractmethod
    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SSHResult:
        """Run ``command`` and return its result. ``sudo=True`` wraps in
        ``sudo -n bash -c '...'`` so compound commands run wholly as root.
        ``tty=None`` is transport default; ``tty=True/False`` overrides."""

    @abc.abstractmethod
    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run an interactive session with a TTY. Empty ``command`` opens a
        login shell. ``env`` is best-effort: SSH carries it via SetEnv; the
        non-SSH transports drop it (their interactive APIs don't expose env
        injection). Returns the process exit code."""

    @abc.abstractmethod
    def copy_to(self, local: Path | str, remote: str) -> None:
        """Copy a local file to the remote path on the VM."""

    @abc.abstractmethod
    def copy_from(self, remote: str, local: Path | str, *, timeout: int | None = None) -> None:
        """Copy a remote file from the VM to a local path. SSH uses scp;
        Lima uses ``limactl copy``; WSL2 uses ``wsl ... cat`` to stdout;
        RemoteLima two-hops. ``backup.py`` is the canonical consumer."""

    @abc.abstractmethod
    def copy_dir_to(
        self,
        local: Path,
        remote: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        """Copy a local directory tree to the VM."""

    @abc.abstractmethod
    def write_file(self, remote_path: str, content: str, *, mode: str | None = None) -> None:
        """Write a small string (rcfile, authorized_keys, etc.) atomically
        to ``remote_path``. ~15 call sites today; load-bearing for init."""

    @abc.abstractmethod
    def call_streaming(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command with inherited stdio (no buffering). Used by
        ``vm exec`` and ``agent exec`` so the operator sees output stream
        in real time. SSH wraps the SSH invocation with inherited stdio;
        the non-SSH transports do the same with their respective subprocess.
        Returns the remote exit code."""
```

`run_as_root` is **not** a separate abstract method. Per the prior locked SDD
(`2026-04-27-exec-target-cleanup`), `run(sudo=True)` is the unified shape; we preserve that.

`wait_for_reconnect` stays as a module-level function in `agentworks/transports/__init__.py` taking
a `Transport`, not an ABC method. It's a higher-level operation (probe + retry) layered on top of
`run()`; it doesn't need polymorphic dispatch beyond what `run()` already provides.

The `SSHResult` name is preserved. We add a one-line comment at its definition explaining the name
predates the polymorphic shape; renaming to `TransportResult` is out of scope to bound this
refactor's diff.

Each Transport subclass takes optional `logger: SSHLogger | None` and `default_timeout: int | None`
in its constructor. These move from today's `ExecTarget` fields into the per-class constructor
signatures.

### Env handling and SSH specifics

`run()`'s `env` parameter is method-level, not constructor-level. `SSHTransport.run()` translates
`env` into `-o SetEnv="K1=V1" "K2=V2" ...` argv at call time; the SSH-specific helpers for SetEnv
argv building (today `_set_env_args` in `ssh.py`) move into `agentworks/transports/ssh.py`. Lima /
WSL2 / RemoteLima prepend `K1=V1 ...` to the bash -lc payload for non-interactive runs. The non-SSH
transports drop `env` on interactive sessions (documented behavior; matches today's post-PR-#118
shape).

## Concrete transports

```python
# agentworks/transports/ssh.py

class SSHTransport(Transport):
    def __init__(
        self,
        host: str,
        *,
        user: str | None = None,
        identity_file: Path | None = None,
        port: int | None = None,
        proxy_jump: str | None = None,
        force_tty: bool = False,
        login_shell: bool = False,
        default_timeout: int | None = None,
        logger: SSHLogger | None = None,
    ) -> None:
        ...

    # plus the SSH-specific argv builders, SetEnv handling, sudo wrap.
```

```python
# agentworks/transports/lima.py

class LimaTransport(Transport):
    def __init__(self, vm_name: str, *, logger=None, default_timeout=None) -> None:
        ...

# agentworks/transports/remote_lima.py

class RemoteLimaTransport(Transport):
    def __init__(self, vm_name: str, vm_host_ssh: str, *, logger=None, default_timeout=None) -> None:
        ...
    # All commands wrap in '$SHELL -lc' on the VM host so login-shell PATH
    # finds limactl (Homebrew at /opt/homebrew/bin etc.). Both the
    # interactive and non-interactive paths use the same wrapping.

# agentworks/transports/wsl2.py

class WSL2Transport(Transport):
    def __init__(self, distro_name: str, user: str, *, logger=None, default_timeout=None) -> None:
        ...
```

For Azure: the provisioner's `provisioner_transport(vm, config)` returns an `SSHTransport`
configured against the temporarily-attached public IP. There is no separate `AzureTransport` --
Azure's provisioning path is SSH over a transient IP, so it reuses `SSHTransport`. The IP lifecycle
(attach / detach) lives in `provisioner_transport()` exactly as it does in PR #118's
`_provisioner_shell_target`, including the ExitStack discipline and the reachability-probe retry
loop.

For Proxmox: `provisioner_transport(vm, config)` raises a typed `StateError` with the same "use the
Proxmox web UI's serial console" hint that PR #118 shipped. A real Proxmox transport would need
guest-agent-exec integration that's out of scope; the typed error stays.

## Factory functions

Three named factories plus one low-level helper, all exported from
`agentworks/transports/__init__.py`:

```python
# agentworks/transports/__init__.py

def transport(
    vm: VMRow, config: Config, *,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """The canonical transport for a VM as the admin user (Tailscale SSH
    today). Used by every normal operator workflow. Raises StateError if
    the canonical transport is unavailable. Never falls back to the
    provisioner transport -- see the SDD's R3.
    """
    return transport_for_user(
        vm, config,
        user=vm.admin_username,
        identity_file=config.operator.ssh_private_key,
        default_timeout=default_timeout,
        logger=logger,
    )


def agent_transport(
    vm: VMRow, config: Config, agent: AgentRow, *,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """The canonical transport for a VM as a named agent's Linux user.
    Same Tailscale SSH mechanism as ``transport()``; just a different
    SSH user and identity file. Used by every operator-facing operation
    that targets an agent (agent shell, agent exec, agent-mode sessions).
    """
    return transport_for_user(
        vm, config,
        user=agent.linux_user,
        identity_file=_agent_identity_file(config, agent),
        default_timeout=default_timeout,
        logger=logger,
    )


def transport_for_user(
    vm: VMRow, config: Config, *,
    user: str,
    identity_file: Path | None = None,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """Lower-level helper: build an SSHTransport for ``user`` on this VM.
    The two named factories above call this. Direct use is reserved for
    the mid-create case (today's only direct caller is in agents/manager.py
    where the agent row doesn't exist yet because we're building it).

    Raises a typed ``StateError`` if the VM has no Tailscale IP. (Today's
    code asserts, which disappears under ``python -O``; the new factory
    promotes this to a typed error. R6 allows this as an improvement
    over misleading-or-disappearing-assert behavior.)
    """
    if vm.tailscale_host is None:
        raise StateError(...)
    return SSHTransport(
        host=vm.tailscale_host,
        user=user,
        identity_file=identity_file,
        force_tty=(sys.platform == "win32"),
        default_timeout=default_timeout,
        logger=logger,
    )


def provisioner_transport(
    db: Database, vm: VMRow, config: Config, *,
    stack: contextlib.ExitStack,
) -> Transport:
    """The platform-native transport for a VM. Used only at bootstrap and
    via the explicit operator opt-in ``vm shell --provisioner``. Most code
    should not reach for this -- see the SDD's R3.

    Calls the provisioner's ``transient_route(vm)`` context manager (see
    below) so any platform-specific transient setup (Azure attaches a
    public IP, others are no-op) lives polymorphically inside the
    provisioner rather than as an isinstance check here.

    ``db`` is needed by ``get_provisioner_for_vm`` to resolve the
    RemoteLima case (``vm.vm_host_name`` -> SSH host via
    ``db.get_vm_host``). The three canonical factories don't need it,
    which is why only this one takes it.
    """
    prov = get_provisioner_for_vm(db, vm, config)
    stack.enter_context(prov.transient_route(vm))
    try:
        return prov.provisioner_transport(vm, config=config)
    except NotImplementedError as e:
        raise StateError(...) from e


def wait_for_reconnect(
    target: Transport, *, max_attempts: int = 16,
) -> bool:
    """Probe the transport with ``echo ok`` and retry until reachable or
    out of attempts. Polymorphic over any Transport via its ``run()``.
    Lives at the package root because it composes ``Transport.run()``
    rather than implementing transport-specific behavior."""
    ...
```

`provisioner_transport()` requires an `ExitStack` for the transient lifecycle. Callers pass their
own stack so the cleanup is bounded by the caller's scope (today's `shell_vm` and bootstrap both
already use a stack).

`VMProvisioner.admin_exec_target` is renamed to `VMProvisioner.provisioner_transport` to match the
public-facing name, and returns a `Transport`.

### The `transient_route` hook on VMProvisioner

```python
# agentworks/vms/base.py

class VMProvisioner(abc.ABC):
    ...

    def transient_route(self, vm: VMRow) -> AbstractContextManager[None]:
        """Hold any platform-native transient network state for the
        duration of the context.

        Default no-op. Azure overrides to attach a temporary public IP on
        enter and detach on exit. Other provisioners (Lima, RemoteLima,
        WSL2, Proxmox) accept the default: their provisioner transports
        don't need transient setup.
        """
        return contextlib.nullcontext()
```

Azure overrides:

```python
class AzureProvisioner(VMProvisioner):
    ...

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow) -> Iterator[None]:
        self.attach_public_ip(vm)
        try:
            yield
        finally:
            self.detach_public_ip(vm)
```

This composes cleanly with `stack.enter_context(prov.transient_route(vm))` in the factory: each
provisioner declares both halves of "what platform-native setup do I need before my transport
works." The factory has no isinstance check.

### `vm_active` vs `transient_route`

`VMProvisioner` will have two context-manager methods after this refactor:

- `vm_active(vm, *, config) -> AbstractContextManager[None]` (existing): "keep this VM reachable as
  a resource for the duration of the context." Today's sole non-default override is WSL2's keepalive
  against `vmIdleTimeout`. Concerns: lifecycle, idleness, resume-from-suspend.
- `transient_route(vm) -> AbstractContextManager[None]` (new in this refactor): "open a
  platform-native network route to the VM for the duration of the context." Today's sole non-default
  override is Azure's attach/detach of a public IP. Concerns: networking, routing, transport
  reachability.

They're orthogonal: a VM can be "active" (running) without a "transient route" (Azure VM running
with no public IP attached, reachable only over Tailscale); the route is useless without an active
VM (WSL2 distro shut down by idle timeout has no transport, public IP or otherwise). The factory
pattern is "enter both" when both are needed. Folding them into a single method would conflate two
unrelated concerns and force callers to opt into both even when they only need one.

## File layout

```text
cli/agentworks/transports/
    __init__.py          # Re-exports Transport (from base.py); defines
                         #   transport(), agent_transport(),
                         #   provisioner_transport(), transport_for_user(),
                         #   and wait_for_reconnect(). Re-exports the
                         #   concrete classes for typing.
    base.py              # Transport ABC definition.
    ssh.py               # SSHTransport + SSH-specific helpers.
    lima.py              # LimaTransport.
    remote_lima.py       # RemoteLimaTransport.
    wsl2.py              # WSL2Transport.
```

`cli/agentworks/ssh.py` is reduced to whatever genuinely-SSH-specific non-Transport code remains
(today: `SSHLogger`, the `SSHResult` dataclass, top-level `run` / `run_as_root` / `copy_to` /
`copy_from` / `write_file` functions if any callers outside ExecTarget still use them). Most of
these are absorbed into `transports/ssh.py` during Phase 4; `SSHLogger` and `SSHResult` likely stay
in `cli/agentworks/ssh.py` (or move to a `transports/types.py` module) because they're shared across
transports.

`wait_for_reconnect` moves to `cli/agentworks/transports/__init__.py` and takes a `Transport`
instead of an `ExecTarget`.

`_unwrap_ssh` is deleted entirely in Phase 4. Its only consumer today is
`cli/agentworks/vms/backup.py`, which uses it to obtain a raw `SSHTarget` for driving local-side
`scp`. Post-refactor, `backup.py` consumes the polymorphic `Transport.copy_from` instead (newly
added to the ABC, see above); the SSH implementation of `copy_from` is the same scp call, just
behind the polymorphic surface. No `_unwrap_ssh` callers remain.

## Migration sequencing

The refactor is structural but the existing surface (`ExecTarget`, `admin_exec_target`) is
load-bearing for ~237 call sites across 23 files. To keep the diff reviewable, the migration runs in
phases (see plan.md):

1. **Phase 1**: Build the new `agentworks/transports/` package alongside the existing code. Define
   `Transport`, add concrete subclasses. Test each in isolation against the ABC. Not yet wired up to
   any caller.
2. **Phase 2**: Add the two factory functions. Wire `VMProvisioner.provisioner_transport` on each
   provisioner. Test the no-failover invariant.
3. **Phase 3**: Migrate every `ExecTarget` / `admin_exec_target` call site to the new factories and
   `Transport` typing.
4. **Phase 4**: Delete the legacy code (`ExecTarget`, per-transport helpers in `ssh.py`, the
   `_unwrap_ssh` shim if its callers all migrated).
5. **Phase 5**: Final tests, docs, lint, PR.

Each phase is a logical commit. The final PR is the merged result.

## What is _not_ changing

- `SSHResult` keeps its name. A one-line comment at its definition explains the post-refactor
  reading. The rename to `TransportResult` is out of scope.
- The CLI command surface. Nothing operator-visible moves.
- `vm shell --provisioner`'s observable behavior, including the (now-polymorphic) Azure
  attach/detach lifecycle and the Proxmox web-console hint. The shape changes (no more isinstance
  check; `VMProvisioner.transient_route` is the new hook) but the operator sees the same outputs.
- `SSHTarget`, `LimaTarget`, `RemoteLimaTarget`, `WSL2Target` -- these data-only classes have small
  enough surfaces that the transport classes can absorb their fields directly into constructors. The
  named-target classes go away.

### Note on the `sessions/tmux.py` `RunCommand` Protocol

The Protocol at `cli/agentworks/sessions/tmux.py:33` is a callable
`(command, *, check, env) -> result`-shaped Protocol that the manager layer satisfies by passing a
partial of `transport.run`. It does **not** use `_unwrap_ssh` (an earlier draft of this SDD claimed
it did, which was wrong). The Protocol survives the refactor untouched: pass a partial of
`Transport.run` and it works.

### Note on `_unwrap_ssh`

Deleted in Phase 4. The only consumer today is `vms/backup.py`, which migrates to the polymorphic
`Transport.copy_from` in Phase 3.
