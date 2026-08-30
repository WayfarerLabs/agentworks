"""Direct admin-user access commands: shell and exec."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks.errors import StateError

from ._helpers import (
    _guard_failed_vm,
    _require_vm,
    _resolve_workspace_for_vm,
)
from .boundary import (
    gated_vm_boundary,
    gated_vm_platform_recovery_boundary,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy

# NOTE on ``_resolve_vm_admin_env_scopes`` / ``_vm_secret_target``: both
# are defined in ``_helpers.py``, and tests monkeypatch them as
# attributes of the PACKAGE (``agentworks.vms.manager._resolve_vm_admin_env_scopes``
# / ``._vm_secret_target``). A plain ``from ._helpers import
# _resolve_vm_admin_env_scopes`` here would bind a local name in this
# module's namespace, invisible to a monkeypatch of the package
# attribute, so ``shell_vm`` / ``exec_vm`` call them through
# ``import agentworks.vms.manager as _mgr`` instead.


def shell_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    platform_transport: bool = False,
    workspace_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> int:
    """Open a shell on a VM as the admin user.

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`exec_vm`.

    By default uses the Tailscale SSH transport. Pass
    ``platform_transport=True`` (the ``vm shell --platform`` flag) to
    use the platform-native transport instead (``limactl shell`` for
    Lima, ``wsl.exe`` for WSL2, SSH via public IP for Azure). That is
    the right choice when Tailscale connectivity is the thing you need
    to fix (e.g. healing the issue #117 latched DNS state, which
    involves restarting tailscaled itself).

    When ``workspace_name`` is set, the shell ``cd``s into the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`): the graph derives from
    the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the whole interactive session.
    """
    import shlex

    import agentworks.vms.manager as _mgr
    from agentworks.env import ResourceContext, compose_env
    from agentworks.transports import native_transport, transport

    vm = _require_vm(db, name)
    # Init failure warns instead of blocks: shelling into a partially-
    # initialized VM is exactly the kind of operation that lets the
    # operator diagnose what failed or apply a manual fix (e.g. healing
    # the issue #117 latched DNS state) before re-running reinit. Same
    # rationale applies to `vm exec` (see exec_vm below).
    _guard_failed_vm(vm, allow_failed_init=True)

    # Resolve workspace before the transport-state guard: a cross-VM
    # mismatch is more diagnostic than "no Tailscale", so it should
    # surface first. The scope chain also needs the workspace before
    # secret resolution.
    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    if not platform_transport and vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint=(
                "VM init may not be complete; check 'vm describe' for status. "
                "If Tailscale itself is the problem you're trying to reach the "
                "VM to fix, run with --platform to use the platform-native "
                "transport instead."
            ),
        )

    # The orchestrated composition root (gated_vm_boundary): the admin
    # shell's env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), and every node's
    # preflight (missing tool, stranded site, unresolvable secret)
    # fails before any prompt. The same scope dicts feed both the
    # SecretTarget (via _vm_secret_target) and compose_env so the two
    # consumers can't drift. Crucially the vm scope comes from
    # vm.template (DB row), not the config-default template, which may
    # not match and would silently route the wrong env into a shell on
    # a non-default-template VM.
    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config, live_database=db)
    scopes = _mgr._resolve_vm_admin_env_scopes(db, registry, vm, ws=ws)

    boundary = gated_vm_platform_recovery_boundary if platform_transport else gated_vm_boundary
    with contextlib.ExitStack() as stack:
        vm_node, resolver, ops_ctx = stack.enter_context(
            boundary(
                db,
                config,
                registry,
                vm,
                targets=[_mgr._vm_secret_target(scopes, label=f"vm-shell={vm.name}")],
                interaction=interaction,
            )
        )

        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=vm.admin_username,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            admin=scopes.admin,
        )

        # --platform builds the platform-native transport, which on a
        # cloud backend is an authenticated call: it gets the boundary's
        # op-start context (site secrets scoped to the site's declared
        # names), the same one the no-gate power commands hand their ops.
        target = (
            native_transport(vm, vm_node.site.platform, config, ctx=ops_ctx, stack=stack)
            if platform_transport
            else transport(vm, config)
        )
        if ws is not None:
            cmd = f"cd {shlex.quote(ws.workspace_path)} && exec $SHELL -l"
            return target.interactive(cmd, env=env)
        return target.interactive("", env=env)


def exec_vm(
    db: Database,
    config: Config,
    name: str,
    command: list[str],
    *,
    workspace_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> int:
    """Execute a command on a VM as the admin user via direct admin SSH.

    Uses inherited stdio for streaming output without buffering.
    Returns the remote exit code.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`), mirroring
    :func:`shell_vm`: the gate opens before the preflight sweep and
    the held-active span covers the streamed remote command.
    """
    import shlex

    import agentworks.vms.manager as _mgr
    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import normalize_exec_command
    from agentworks.transports import transport

    command = normalize_exec_command(command, kind="vm", name=name)

    vm = _require_vm(db, name)
    # Init failure warns instead of blocks. exec is the non-interactive
    # twin of shell: both are diagnostic primitives, and running
    # `agw vm exec failed-vm cat /var/log/cloud-init.log` is precisely
    # the kind of investigation an operator does on a failed-init VM.
    _guard_failed_vm(vm, allow_failed_init=True)

    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    # transport() raises StateError when tailscale_host is None; guard first so
    # the operator gets an actionable StateError instead of an AssertionError
    # (which also disappears under python -O).
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    # The orchestrated composition root (gated_vm_boundary): the exec
    # env-chain secrets join the ONE boundary resolve (site secrets +
    # env secrets, one prompt session), after every node's preflight.
    # The same scope dicts feed both the SecretTarget and compose_env
    # so the two consumers can't drift. The vm scope comes from
    # vm.template (DB row), not the config-default template.
    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config, live_database=db)
    scopes = _mgr._resolve_vm_admin_env_scopes(db, registry, vm, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_mgr._vm_secret_target(scopes, label=f"vm-exec={vm.name}")],
        interaction=interaction,
    ) as (_vm_node, resolver, _ops_ctx):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=vm.admin_username,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            admin=scopes.admin,
        )

        target = transport(vm, config)
        remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
        if ws is not None:
            remote_cmd = f"cd {shlex.quote(ws.workspace_path)} && {remote_cmd}"
        return target.call_streaming(remote_cmd, env=env)
