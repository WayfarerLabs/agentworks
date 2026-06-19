# Polymorphic transports -- high-level architecture

## The Transport ABC

```python
# agentworks/transports/__init__.py (or transports/base.py)

class Transport(abc.ABC):
    """Operator I/O channel to a VM: command exec and file movement.

    Each concrete subclass implements the surface for one delivery mechanism
    (SSH, limactl shell, wsl.exe, etc.). Callers obtain a Transport via the
    two factory functions in this package: ``transport(vm, config)`` for the
    canonical path, ``provisioner_transport(vm, config)`` for the platform-
    native opt-in.
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
        ...

    @abc.abstractmethod
    def interactive(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        ...

    @abc.abstractmethod
    def copy_to(self, local: Path | str, remote: str) -> None:
        ...

    @abc.abstractmethod
    def copy_dir_to(
        self,
        local: Path,
        remote: str,
        *,
        delete: bool = True,
        timeout: int | None = None,
    ) -> None:
        ...
```

The `SSHResult` name is preserved (semantically it's "the result of a command on the transport";
renaming it to `TransportResult` would churn every call site for marginal gain). The `env` parameter
on `interactive()` is documented as best-effort across transports: respected by SSH via `SetEnv`;
not propagated by `limactl shell` / `wsl.exe` for interactive sessions because their interactive
APIs don't expose env-injection. This is unchanged from the post-PR-#118 behavior.

The logger and default-timeout fields that today live on `ExecTarget` move to each Transport
subclass's constructor (most callers don't set them; the SSH-bootstrap path that does threads them
through as before).

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

```python
# agentworks/transports/__init__.py

def transport(vm: VMRow, config: Config) -> Transport:
    """The canonical transport for a VM (Tailscale SSH today). Used by every
    normal operator workflow. Raises StateError if the canonical transport is
    unavailable for this VM. Never falls back to the provisioner transport --
    see the SDD's R3 for why.
    """
    if vm.tailscale_host is None:
        raise StateError(...)
    return SSHTransport(
        host=vm.tailscale_host,
        user=vm.admin_username,
        identity_file=config.operator.ssh_private_key,
        force_tty=(sys.platform == "win32"),
    )


def provisioner_transport(
    vm: VMRow, config: Config, *, stack: contextlib.ExitStack,
) -> Transport:
    """The platform-native transport for a VM. Used only at bootstrap and via
    the explicit operator opt-in ``vm shell --provisioner``. Most code should
    not reach for this -- see the SDD's R3.

    Azure: attaches a temporary public IP via stack.callback; the detach
    runs on stack exit regardless of how the caller unwinds. Reachability
    probe (echo ok with retries) before returning to give the IP time to
    propagate. Proxmox: raises StateError with the web-UI-console hint.
    """
    prov = get_provisioner_for_vm(...)
    if isinstance(prov, AzureProvisioner):
        prov.attach_public_ip(vm)
        stack.callback(prov.detach_public_ip, vm)
    try:
        return prov.provisioner_transport(vm, config=config)
    except NotImplementedError as e:
        raise StateError(...) from e
```

Note: `provisioner_transport()` requires an `ExitStack` for the Azure cleanup lifecycle. Callers
pass their own stack so the cleanup is bounded by the caller's scope (today's `shell_vm` and
bootstrap both already use a stack). This is the same shape PR #118 settled on; it survives the
refactor unchanged.

`VMProvisioner.admin_exec_target` is renamed to `VMProvisioner.provisioner_transport` to match the
public-facing name, and returns a `Transport`. The isinstance check on `AzureProvisioner` stays for
now -- a base-class hook is a follow-up best deferred until Proxmox is implemented (it'd be the
second user of any such hook).

## File layout

```text
cli/agentworks/transports/
    __init__.py          # Transport ABC, transport(), provisioner_transport(),
                         #   plus re-exports of the concrete classes for typing.
    ssh.py               # SSHTransport + SSH-specific helpers.
    lima.py              # LimaTransport.
    remote_lima.py       # RemoteLimaTransport.
    wsl2.py              # WSL2Transport.
```

`cli/agentworks/ssh.py` is reduced to whatever genuinely-SSH-specific non-Transport code remains
(today: `SSHLogger`, `wait_for_reconnect`, plus the standalone `_unwrap_ssh` shim that the
sessions/tmux `RunCommand` callback uses). If any of those move to `transports/ssh.py` naturally,
they do; otherwise `cli/agentworks/ssh.py` stays as a small SSH-specific utility module.

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

- `SSHResult` keeps its name. It's already a transport-agnostic result shape.
- The CLI command surface. Nothing operator-visible moves.
- `vm shell --provisioner`'s behavior, including the Azure attach/detach lifecycle and the Proxmox
  web-console hint. They get re-expressed in terms of `provisioner_transport()` but semantically
  unchanged.
- `SSHTarget`, `LimaTarget`, `RemoteLimaTarget`, `WSL2Target` -- these data-only classes have small
  enough surfaces that the transport classes can absorb their fields directly into constructors. The
  named-target classes go away.
- The `_unwrap_ssh()` shim used by `sessions/tmux.py`'s `RunCommand` callback pattern. That pattern
  wants an `SSHTarget`-like object, not a Transport, and the prior SDD explicitly deferred its
  cleanup as out of scope. Same here.
