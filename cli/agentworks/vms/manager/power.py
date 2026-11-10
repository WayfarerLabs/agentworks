"""VM power-state, deletion, and rekey commands."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import NotFoundError, StateError, UserAbort, ValidationError
from agentworks.execution import trust
from agentworks.naming import MAX_SECRET_NAME_LENGTH, NAME_RE, validate_name
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import (
    _guard_failed_vm,
    _lookup_or_synthesize_secret,
    _require_vm,
    _vm_scope,
)
from .boundary import _live_vm_boundary, _platform_ops_ctx, _warn_legacy_release

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy


def start_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start a stopped VM. Clears the operator-stopped flag so the
    activation gate resumes auto-starting on demand.

    Orchestrated, composition only: the graph derives from the VM's
    row and the power ops drive through the node's held platform
    (:func:`_live_vm_boundary`). No activation gate opens here: the
    start IS this command's operation, and the operator-stopped flag
    is CLEARED by it, never consulted.
    """
    import agentworks.vms.manager as _mgr
    from agentworks.bootstrap import load_request_registry

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    # Before the boundary resolves a secret or the start boots the VM: with no
    # host-key record the reconnect check below could only fail, and its
    # failure would send the repair path to spend a Tailscale auth key.
    trust.require_record(name)
    registry = load_request_registry(config, live_database=db)
    vm_node, ops_ctx = _live_vm_boundary(
        db,
        config,
        vm,
        registry=registry,
        interaction=interaction,
    )
    platform = vm_node.site.platform
    # An explicit start is operator intent, whatever the observed state:
    # clear the flag first so a crashed start doesn't leave the gate
    # refusing to auto-resume a VM the operator asked to run.
    db.set_operator_stopped(name, False)
    # Probe status and issue the start BEFORE entering the hold: the
    # WSL2 keepalive subprocess boots a stopped distro as a side effect,
    # which would make status() report RUNNING and mislabel the VM as
    # "already running". The keepalive then anchors the (now running) VM
    # through the Tailscale verification.
    status = platform.status(vm, ops_ctx)
    if status == VMStatus.RUNNING:
        output.info(f"VM '{name}' is already running")
    else:
        platform.start(vm, ops_ctx)
        if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
            db.record_vm_started(name)
        else:
            db.clear_vm_start_observation(name)

    # Probe and conditionally acquire the standalone repair credential inside
    # the same hold that protects the reconnect/rejoin lifecycle.
    with vm_node.hold_active():
        if _mgr._tailscale_rejoin_required(
            db,
            config,
            vm,
            already_running=status == VMStatus.RUNNING,
        ):
            from agentworks.orchestration.secrets import ScopedSecrets
            from agentworks.secrets import resolve_for_command
            from agentworks.vms.templates import resolve_live_template

            template = resolve_live_template(db, registry, vm.name, vm.template)
            auth_key_name = template.tailscale_auth_key
            declaration = _lookup_or_synthesize_secret(registry, auth_key_name)
            values = resolve_for_command(
                [],
                config,
                registry,
                extra_decls=[declaration],
                interaction=interaction,
            )
            try:
                auth_keys = ScopedSecrets(values, (auth_key_name,))
                _mgr._ensure_tailscale(
                    db,
                    config,
                    vm,
                    platform,
                    ops_ctx,
                    auth_keys=auth_keys,
                    auth_key_name=auth_key_name,
                )
            finally:
                values.clear()
    # Confirm a start only on the path that actually started the VM. When
    # status was already RUNNING we already said so above, and Tailscale
    # verification is usually a no-op (handshake already valid), so an
    # extra line is just noise. Starting does not reconcile native setup.
    if status != VMStatus.RUNNING:
        output.info(f"VM '{name}' started")


def stop_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Stop a running VM and record the operator's intent.

    Orchestrated, composition only, mirroring :func:`start_vm`: no
    activation gate (the stop IS the operation), power ops through the
    node's held platform.
    """
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction)
    platform = vm_node.site.platform
    # Record intent BEFORE the already-stopped short-circuit: an
    # operator stopping an already-stopped VM still means "keep it
    # stopped" (e.g. the VM idled out and they don't want the next op
    # to auto-resume it).
    db.set_operator_stopped(name, True)
    status = platform.status(vm, ops_ctx)
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        # Never conflate an auto-stop with an explicit one: when the VM
        # stopped on its own, this command still CHANGED something (the
        # intent flag above) and the message says what.
        if vm.operator_stopped:
            output.info(f"VM '{name}' is already manually stopped")
        else:
            output.info(
                f"VM '{name}' had already stopped on its own; it is now "
                f"marked manually stopped and will not be auto-started"
            )
        return
    # No hold here: stop is the inverse of what the keepalive is for.
    # The platform stop call doesn't need SSH to the VM, and holding a
    # wsl.exe sleep subprocess open would fight `wsl --terminate`.
    platform.stop(vm, ops_ctx)
    output.result(f"VM '{name}' stopped")


def delete_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Delete the backend VM before removing its managed local resources.

    Child-count and confirmation checks run before binding or secret resolution.
    Binding failures warn and continue local cleanup, including stranded sites.
    A bound provider delete that raises retains local state for retry. Deletion
    never starts the guest or tears down individual workloads.
    """
    require_exact_tty_interaction_policy(interaction)
    vm = _require_vm(db, name)

    # Check for workspaces (which contain agents and sessions)
    ws_count = db.count_workspaces_on_vm(name)
    ag_count = db.count_agents_on_vm(name)
    ts_count = db.count_sessions_on_vm(name)
    has_children = ws_count > 0 or ag_count > 0 or ts_count > 0

    if has_children and not force:
        parts = [f"{ws_count} workspace(s)"]
        if ag_count > 0:
            parts.append(f"{ag_count} agent(s)")
        if ts_count > 0:
            parts.append(f"{ts_count} session(s)")
        raise StateError(
            f"VM '{name}' has {', '.join(parts)}.",
            entity_kind="vm",
            entity_name=name,
            hint="Delete them first, or pass --force to also delete the children.",
        )

    if not yes:
        msg = f"Delete VM '{name}'?"
        if has_children:
            parts = [f"{ws_count} workspace(s)"]
            if ag_count > 0:
                parts.append(f"{ag_count} agent(s)")
            if ts_count > 0:
                parts.append(f"{ts_count} session(s)")
            msg += f" ({', '.join(parts)} will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, vm.name):
        try:
            vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction)
        except UserAbort:
            raise
        except Exception:
            # Preserve the existing local retirement path for an unavailable
            # binding. Provider errors after a successful binding still raise.
            output.warn(
                "Backend binding failed; remote VM deletion was not attempted. "
                "Removing local state only. Inspect and remove any remaining backend resources manually."
            )
        else:
            # Whole-VM removal does not depend on guest workload cleanup.
            vm_node.site.platform.delete(vm, ops_ctx)
        if vm.tailscale_host:
            output.warn(
                "The external Tailscale node entry may remain. "
                "Remove the retired node from the Tailscale admin console."
            )

        # Clean up logs
        from agentworks.ssh import LOG_DIR

        vm_logs = list(LOG_DIR.glob(f"{name}-*.log")) if LOG_DIR.exists() else []
        for log in vm_logs:
            log.unlink(missing_ok=True)
        if vm_logs:
            output.info(f"Cleaned up {len(vm_logs)} log(s)")

        for workspace in db.list_workspaces(vm_name=name):
            # Check the character grammar directly. validate_name's creation-time
            # double-hyphen rule would strand safe legacy artifacts.
            if NAME_RE.fullmatch(workspace.name) is None:
                output.warn("skipping VS Code workspace artifact for an invalid persisted workspace name")
                continue
            vscode_path = config.paths.vscode_workspaces / f"{workspace.name}.code-workspace"
            vscode_path.unlink(missing_ok=True)

        # Remove from DB (cascades workspaces and agents), then rebuild SSH config
        db.delete_vm(name)

        from agentworks.ssh_config import sync_ssh_config

        # The store file goes whenever the VM's row goes, even if the SSH config
        # rebuild raises. Reaching here means the platform delete succeeded (it
        # raises, keeping the row and the record, when it cannot remove the
        # backend VM) or backend cleanup was skipped after a binding failure.
        try:
            trust.remove(name)
        finally:
            sync_ssh_config(config, db)
        output.result(f"VM '{name}' deleted")


def rekey_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    wait_for_share: bool = False,
    secret: str | None = None,
    prompt: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin).

    Useful for rotating keys, switching tailnets, or recovering from
    expired ephemeral keys. Uses the platform's native transport
    (out-of-band) since Tailscale connectivity drops during
    the operation.

    ``secret`` selects a declared replacement key for this operation only.
    ``prompt`` forces terminal entry of the template's key instead. Both
    paths retain the ordinary preflight and single secret-resolution boundary;
    the running check and held-active span follow it.
    """
    import ipaddress
    import time
    from dataclasses import replace

    from agentworks.bootstrap import load_request_registry
    from agentworks.orchestration.activation import activation_gate
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.ssh import SSHError
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.transports import native_transport, transport, wait_for_reconnect
    from agentworks.vms.nodes import live_vm_node, vm_template_node
    from agentworks.vms.templates import resolve_live_template

    require_exact_tty_interaction_policy(interaction)
    if secret is not None and prompt:
        raise ValidationError("--secret and --prompt are mutually exclusive")
    if secret is not None:
        validate_name(secret, max_length=MAX_SECRET_NAME_LENGTH)

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    _warn_legacy_release(vm)
    # Before the auth key resolves: a rekey with no host-key record would spend
    # the new key and record the new address before its reconnect check raised.
    trust.require_record(name)

    registry = load_request_registry(config, live_database=db)
    rekey_vm_tmpl = resolve_live_template(db, registry, vm.name, vm.template)
    if secret is not None:
        try:
            registry.lookup("secret", secret)
        except KeyError:
            raise NotFoundError(
                f"secret {secret!r} is not in the resource registry",
                entity_kind="secret",
                entity_name=secret,
                hint="check `agw secret list` for declared and auto-declared names",
            ) from None
        rekey_vm_tmpl = replace(rekey_vm_tmpl, tailscale_auth_key=secret)
    auth_key_name = rekey_vm_tmpl.tailscale_auth_key
    prompt_names = frozenset((auth_key_name,)) if prompt else frozenset()
    resolver = Resolver(config, registry, interaction=interaction, prompt_names=prompt_names)
    vm_node = live_vm_node(db, config, registry, vm, repair_auth_key_name=auth_key_name)
    tmpl_node = vm_template_node(rekey_vm_tmpl)
    nodes = walk(tmpl_node, vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    scope = _vm_scope(db, name)
    preflight_all(
        nodes,
        RunContext(config=config, operation_scope=scope),
        registry=registry,
        interaction=interaction,
        prompt_names=prompt_names,
    )
    resolver.resolve()
    from agentworks.secrets.line_safety import (
        LineOrientedSecretUse,
        require_line_safe_secret,
    )

    ts_auth_key = require_line_safe_secret(
        resolver.get(auth_key_name),
        use=LineOrientedSecretUse.TAILSCALE,
        secret_name=auth_key_name,
    )

    # The running check is an op (a backend status read), so it sits
    # past the boundary: on proxmox it needs the API token, delivered
    # scoped to the site's declared names.
    ops_ctx = _platform_ops_ctx(config, scope, vm_node, resolver)
    platform = vm_node.site.platform
    status = platform.status(vm, ops_ctx)
    if status != VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' is not running (status: {status.value})",
            entity_kind="vm",
            entity_name=name,
        )

    with output.section(f"Rekeying '{name}'"), contextlib.ExitStack() as _stack:
        # The activation gate, opened AFTER the boundary at exactly the
        # point HEAD held ``keep_active``: converge power state (a race
        # from the running check above, as at HEAD), then hold for the
        # rekey's duration (no-op for Lima/Azure/Proxmox; WSL2 anchors
        # the distro against vmIdleTimeout so per-step `time.sleep`s
        # can't let it idle out). Boundary-then-gate means the gate
        # callback must SERVE the boundary's cached values, never
        # resolve or seed (the batch-ops precedent): the union already
        # covers the gate secrets AND the repair path's rejoin key (the
        # auth key is this command's op secret), and ``Resolver.get``
        # refuses anything outside it loudly.
        _stack.enter_context(activation_gate(vm_node, resolver.get))

        # native_transport() composes transient_route (Azure pokes a
        # scoped ephemeral SSH allow on enter and deletes it on exit)
        # with the platform-native transport builder and the 6-attempt
        # reachability probe. The caller-supplied ExitStack scopes the
        # transient state to the duration of the rekey; ``ops_ctx``
        # delivers the SP credential to those NSG calls with no ambient
        # fallback.
        exec_target = native_transport(vm, platform, config, ctx=ops_ctx, stack=_stack)

        # Restart, logout, login, restart. The initial restart clears any
        # stale daemon state (a previous interrupted rekey can leave the
        # daemon in a state where `tailscale logout` hangs waiting for a
        # control plane response that never comes). The final restart
        # fixes a Tailscale bug where the node registers but peers can't
        # reach it after rekeying to a different tailnet.
        # All platforms run systemd (WSL2 enables it via /etc/wsl.conf during
        # provisioning); tailscaled is always a systemd unit. Daemon-side
        # flags like --tun=userspace-networking live in /etc/default/tailscaled,
        # not on `tailscale up`.
        restart_cmd = "systemctl restart tailscaled"
        stabilize_secs = 15  # pause between steps for daemon/network stability

        output.info("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.info("Logging out of current tailnet...")
        exec_target.run("tailscale logout", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.info("Joining new tailnet...")
        from agentworks.capabilities.vm_platform.tailscale_join import join_tailscale_ephemerally

        join_tailscale_ephemerally(exec_target, ts_auth_key, timeout=30)
        time.sleep(stabilize_secs)

        output.info("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.info("Reading new Tailscale IP...")
        result = exec_target.run("tailscale ip -4", sudo=True, timeout=15)
        raw_ip = result.stdout.strip()
        new_ip = raw_ip.splitlines()[0].strip() if raw_ip else ""
        try:
            ipaddress.IPv4Address(new_ip)
        except ValueError:
            raise SSHError(f"tailscale ip -4 returned invalid address: {new_ip!r}\nfull output: {raw_ip}") from None
        output.detail(f"Tailscale IP: {new_ip}")

        # Update DB and SSH config with the new IP (correct regardless of
        # reachability: the old IP is definitely dead after logout)
        db.update_vm_tailscale(name, new_ip)
        sync_ssh_config(config, db)
        db.insert_vm_event(name, "rekey", f"new_ip={new_ip}")

        # If the operator needs to share the VM back, pause before connectivity check
        if wait_for_share:
            output.pause("Share the VM back to your tailnet, then press Enter to verify connectivity...")

        # Always verify Tailscale SSH connectivity to the new IP
        output.info(f"Verifying SSH to {new_ip}...")
        from agentworks.transports import SSHTransport

        ts_target = transport(vm, config)
        # ``transport()`` returns an SSHTransport for Tailscale-backed VMs.
        # Retarget the host in place instead of rebuilding the whole
        # transport; the other fields (user, identity, etc.) are unchanged.
        assert isinstance(ts_target, SSHTransport)
        ts_target.host = new_ip
        if wait_for_reconnect(ts_target):
            output.result(f"VM '{name}' rekeyed successfully. Tailscale IP: {new_ip}")
        else:
            output.warn(
                f"VM '{name}' rekeyed but {new_ip} is not reachable via SSH. "
                "Check tailnet sharing/ACLs. Run 'vm rekey' again to retry."
            )
