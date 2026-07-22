"""Direct admin-user access commands: shell, exec, and git-credential add."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.errors import StateError, ValidationError

from ._helpers import (
    _credential_line_key,
    _guard_failed_vm,
    _require_vm,
    _resolve_workspace_for_vm,
    _vm_scope,
)
from .boundary import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database

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
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _mgr._resolve_vm_admin_env_scopes(registry, vm, ws=ws)

    with contextlib.ExitStack() as stack:
        vm_node, resolver = stack.enter_context(
            gated_vm_boundary(
                db,
                config,
                registry,
                vm,
                targets=[_mgr._vm_secret_target(scopes, label=f"vm-shell={vm.name}")],
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

        target = (
            native_transport(vm, vm_node.site.platform, config, stack=stack)
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
    from agentworks.exec_validation import reject_dash_prefixed_command
    from agentworks.transports import transport

    reject_dash_prefixed_command(command, kind="vm", name=name)

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
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _mgr._resolve_vm_admin_env_scopes(registry, vm, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_mgr._vm_secret_target(scopes, label=f"vm-exec={vm.name}")],
    ) as (_vm_node, resolver):
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


def add_git_credential(db: Database, config: Config, name: str, credential_name: str) -> None:
    """Add or update a git credential on a VM.

    This is the first ORCHESTRATED command: its graph is DERIVED from
    the DB row and the declared references by the ``vms/nodes.py``
    factories (zero hand-wired edges), the activation gate replaces
    this command's ``keep_active`` use (opening BEFORE the preflight
    sweep and seeding the boundary resolver with its just-in-time
    values), secrets are delivered scoped to each node's declared
    names, and a rejected token is FATAL (a plain uncaught raise: the
    operator asked to add exactly this one credential, unlike vm/agent
    provisioning's skip-and-degrade).

    The tracer's three documented interim seams are CLOSED with the
    resolver retirement: the walk union is the boundary's only source
    (construct-time registration is gone), prediction is central at
    the node preflights, and the platform's power ops read the
    context (``ctx.secret``, with the gate's scoped reader as the
    source for gate-driven ops).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.transports import transport
    from agentworks.vms.nodes import live_vm_node

    # build_registry runs first so framework miss-policies (e.g.
    # GitCredentialKind's error policy on a typo'd credential name)
    # surface before any DB / VM / config-key business logic.
    registry = build_registry(config)

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry)

    # BUILD: the command names its direct resources (this VM, this
    # credential); everything else enters through the derived graph
    # (the row's site field, the decl's references).
    cred_node = git_credential_node(registry, credential_name)
    provider = cred_node.provider

    entry = provider.helper_entry()
    if entry.repos or entry.owner:
        # Scoped credentials need the helper's embedded selection map
        # rebuilt: a single-line store merge can't provide that. The
        # full-rebuild path (reinit) can. Guarded before the VM node is
        # built and before the gate, preserving the imperative error
        # precedence (at HEAD the site bound after this guard, so a bad
        # site never preempted this error) and ensuring a scoped
        # credential never costs a prompt or a VM start.
        raise ValidationError(
            f"git credential '{credential_name}' is scoped (fine-grained "
            f"PAT); add it to the admin or agent template and run "
            f"'agw vm reinit {name}' instead of add-git-credential"
        )

    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node, cred_node)
    # The walk supplies the boundary union.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = _vm_scope(db, name)

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # PREFLIGHT-ALL against the one command-start context, then the
        # boundary resolve: the walk-away point.
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()

        def scoped_ctx(node_secret_refs: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(resolver.values, node_secret_refs),
            )

        # add-git-credential is a single explicit add, so a rejected
        # token is fatal here (unlike vm/agent provisioning, which
        # skips and continues to partial): the operator asked to add
        # exactly this one credential.
        if config.defaults.runup_git_credentials:
            output.info(f"Performing runup test for git-credential/{credential_name}...")
            cred_node.runup(scoped_ctx(cred_node.secret_refs()))
        # The materials-write op reads its token through the node's
        # SCOPED delivery: only the credential's declared secret names.
        token = scoped_ctx(cred_node.secret_refs()).secret(provider.secret_name)
        new_lines = provider.credential_lines(token)

        target = transport(vm, config)

        # Read existing credentials, filter out entries this credential
        # replaces. The key is (username, host/path): scoped github
        # lines are path-less and share the host, so a host-only key
        # would evict every github line including the scoped ones.
        result = target.run("cat ~/.git-credentials 2>/dev/null || true")
        existing = result.stdout.strip().splitlines() if result.stdout.strip() else []

        new_keys = {_credential_line_key(line) for line in new_lines} - {None}
        filtered = [e for e in existing if _credential_line_key(e) not in new_keys]

        # New (always unscoped, see the guard above) lines go FIRST:
        # a username-less query takes the first matching store line, so
        # the host-level fallback must precede username-tagged scoped
        # lines that may already be on the VM.
        all_lines = new_lines + filtered
        cred_content = "\n".join(all_lines) + "\n"
        target.write_file("~/.git-credentials", cred_content, mode="600")
        # This single-line merge does NOT regenerate the credential helper
        # script (it stays from the last full init/reinit). The scoped
        # guard above forces scoped credentials through reinit, so the
        # added line is always unscoped and selection needs no helper
        # change; its only gap is that a rejection of a credential added
        # post-init falls to the helper's generic (unnamed) diagnosis
        # until the next reinit rebuilds the script. Acceptable.
        # Never downgrade the helper slot: on a helper-provisioned VM
        # the agentworks helper stays registered (reverting to store
        # would reintroduce its erase-on-rejection self-destruct for
        # EVERY credential); on an old VM without the helper script,
        # keep store working until the next reinit installs the helper.
        from agentworks.git_credentials import GIT_CRED_HELPER_PATH

        target.run(
            f"if [ -x {GIT_CRED_HELPER_PATH} ]; then "
            f"git config --global --replace-all credential.helper '!{GIT_CRED_HELPER_PATH}'; "
            f"else git config --global credential.helper store; fi"
        )

    output.result(f"Git credential '{credential_name}' configured on VM '{name}'")
