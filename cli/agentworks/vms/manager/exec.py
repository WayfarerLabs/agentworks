"""Direct admin-user access commands: shell and exec."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._helpers import (
    _guard_incomplete_vm,
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

# ssh's status for a dropped connection, and a status a shell's last command
# can exit with too, so the notice names it without claiming the cause.
_STATUS_CONNECTION_MAY_HAVE_DROPPED = 255

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
    native_shell: bool = False,
    workspace_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> int:
    """Open a shell on a VM as the admin user.

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`exec_vm`.

    By default attaches over SSH through ``agentworks.execution``. Pass
    ``native_shell=True`` (the ``vm shell --platform`` flag) to open the
    platform's own shell instead (:func:`_shell_native`), the way in when
    Tailscale itself is what needs fixing.

    When ``workspace_name`` is set, the shell ``cd``s into the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`): the graph derives from
    the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the whole interactive session.
    """
    import agentworks.execution as execution
    import agentworks.vms.manager as _mgr
    from agentworks.env import ResourceContext, compose_env
    from agentworks.execution.access import Admin
    from agentworks.execution.requests import LoginShell

    if native_shell:
        return _shell_native(db, config, name, workspace_name=workspace_name, interaction=interaction)

    # Before any database read or guard: an unsupported client refuses first.
    execution.resolve_ssh()
    vm = _require_vm(db, name)
    # Init failure warns instead of blocks: shelling into a partially-
    # initialized VM is exactly the kind of operation that lets the
    # operator diagnose what failed or apply a manual fix (e.g. healing
    # the issue #117 latched DNS state) before re-running reinit. Same
    # rationale applies to `vm exec` (see exec_vm below).
    _guard_incomplete_vm(vm, allow_failed_init=True)

    # Resolve workspace before the transport-state guard: a cross-VM
    # mismatch is more diagnostic than "no Tailscale", so it should
    # surface first. The scope chain also needs the workspace before
    # secret resolution.
    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint=(
                "VM init may not be complete; check 'vm describe' for status. "
                "If Tailscale itself is the problem you're trying to reach the "
                "VM to fix, run with --platform to use the platform's own shell "
                "instead, or 'agw vm recover' to repair it."
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

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_mgr._vm_secret_target(scopes, label=f"vm-shell={vm.name}")],
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
        access = execution.vm_access(vm, config, Admin())
        return access.attach(LoginShell(cwd=ws.workspace_path if ws else None, env=env))


def _shell_native(
    db: Database,
    config: Config,
    name: str,
    *,
    workspace_name: str | None,
    interaction: TtyInteractionPolicy,
) -> int:
    """``vm shell --platform``: the platform's own shell, which works without the VM's Tailscale.

    Refused, before anything resolves, when combined with a workspace (the
    native shell carries no environment and no working directory) and on a
    platform that offers no native shell. The boundary composes no
    environment target, so no environment secret resolves (R7): the shell
    gets the account's own login environment. Inside the platform-recovery
    gate, the platform runs the shell under the terminal guard with the
    boundary's op-start context, which a cloud needs for its route.
    """
    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.capabilities.vm_platform.base import require_native_shell
    from agentworks.terminal import guarded_terminal
    from agentworks.vms.sites import resolve_site

    if workspace_name is not None:
        raise ValidationError(
            "--workspace cannot be combined with --platform",
            entity_kind="vm",
            entity_name=name,
            hint="Open the native shell without --workspace, then 'cd' into the workspace directory inside it.",
        )
    vm = _require_vm(db, name)
    _guard_incomplete_vm(vm, allow_failed_init=True)
    registry = load_request_registry(config, live_database=db)
    require_native_shell(resolve_site(vm.site, registry), vm.name)

    with gated_vm_platform_recovery_boundary(db, config, registry, vm, interaction=interaction) as (
        vm_node,
        _resolver,
        ops_ctx,
    ):
        with guarded_terminal() as guard:
            code = vm_node.site.platform.native_shell(vm, ops_ctx)
            guard.clean_exit = code == 0
        # After the guard: its exit pass leaves the alternate screen, which
        # would wipe anything written before it closes (I6).
        if code == _STATUS_CONNECTION_MAY_HAVE_DROPPED:
            output.warn(
                f"the native shell ended with status {code} (the connection may have dropped); terminal restored."
            )
        return code


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

    Streams the command's output live and copies the operator's stdin to
    it. Returns the remote exit code: a command that cannot start is 127
    (not found) or 126 (found but not runnable), with the shell's message, and
    125 means the workspace directory is missing, with one line from agw.

    One argument is a shell command line and several are shell-joined into one
    (``shell_command_line``), run by the admin's default shell, so builtins,
    functions and a leading ``NAME=value`` work.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`), mirroring
    :func:`shell_vm`: the gate opens before the preflight sweep and
    the held-active span covers the streamed remote command.
    """
    import agentworks.execution as execution
    import agentworks.vms.manager as _mgr
    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import normalize_exec_command
    from agentworks.execution.access import Admin, stream_to_operator
    from agentworks.execution.requests import Script, Shell, shell_command_line

    # Before any database read or guard: an unsupported client refuses first.
    execution.resolve_ssh()
    command = normalize_exec_command(command, kind="vm", name=name)

    vm = _require_vm(db, name)
    # Init failure warns instead of blocks. exec is the non-interactive
    # twin of shell: both are diagnostic primitives, and running
    # `agw vm exec failed-vm cat /var/log/cloud-init.log` is precisely
    # the kind of investigation an operator does on a failed-init VM.
    _guard_incomplete_vm(vm, allow_failed_init=True)

    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    # Guard first so the operator gets the VM-specific hint before any gate opens.
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

        access = execution.vm_access(vm, config, Admin())
        cwd = ws.workspace_path if ws else None
        source = command[0] if len(command) == 1 else shell_command_line(command)
        request = Script(source, Shell.USER_DEFAULT, cwd=cwd, env=env)
        return stream_to_operator(access, request)
