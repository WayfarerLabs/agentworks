"""VM power-state, deletion, and rekey commands."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.naming import NAME_RE
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import (
    _guard_failed_vm,
    _mask_env_var_backend_for,
    _require_vm,
    _vm_scope,
)
from .boundary import _live_vm_boundary, _platform_ops_ctx, _warn_legacy_release

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy


def set_vm_desired_runtime_state(db: Database, *, name: str, state: str | DesiredRuntimeState) -> None:
    """Record VM intent without registry, credentials, or runtime work."""
    from agentworks.errors import ValidationError
    from agentworks.harness_setup.locking import native_mutation_guard

    try:
        target = DesiredRuntimeState(state)
    except (ValueError, TypeError):
        raise ValidationError("invalid VM desired runtime state", entity_kind="vm", entity_name=name) from None
    selected = _require_vm(db, name)
    with native_mutation_guard(db.path, name):
        db.set_vm_desired_runtime_state(name, target, expected_vm_uuid=selected.vm_uuid)


def start_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept running intent, then start and repair the retained VM."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.secrets.resolver import Resolver

    from .runtime import prepare_vm_runtime, start_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    selected = _require_vm(db, name)
    _guard_failed_vm(selected)
    with native_mutation_guard(db.path, name) as guard:
        registry = load_request_registry(config, live_database=db)
        db.set_vm_desired_runtime_state(name, DesiredRuntimeState.RUNNING, expected_vm_uuid=selected.vm_uuid)
        vm = _require_vm(db, name)
        resolver = Resolver(config, registry, interaction=interaction)
        context = None
        try:
            context = prepare_vm_runtime(
                db,
                config,
                registry,
                vm,
                resolve_secret=resolver.resolve_gate,
                interaction=interaction,
                held_native_guard=guard,
            )
            start_vm_runtime(context, resolve_secret=resolver.resolve_gate)
        finally:
            if context is not None:
                context.clear_values()
            resolver.close()
        if context.started:
            output.info(f"VM '{name}' started")
        else:
            output.info(f"VM '{name}' is already running")


def stop_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept stopped intent before provider work, preserving it on failure."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.secrets.resolver import Resolver

    from .runtime import prepare_vm_runtime, stop_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    selected = _require_vm(db, name)
    _guard_failed_vm(selected)
    with native_mutation_guard(db.path, name) as guard:
        registry = load_request_registry(config, live_database=db)
        db.set_vm_desired_runtime_state(name, DesiredRuntimeState.STOPPED, expected_vm_uuid=selected.vm_uuid)
        vm = _require_vm(db, name)
        resolver = Resolver(config, registry, interaction=interaction)
        context = None
        try:
            context = prepare_vm_runtime(
                db,
                config,
                registry,
                vm,
                resolve_secret=resolver.resolve_gate,
                interaction=interaction,
                held_native_guard=guard,
            )
            changed = stop_vm_runtime(context)
        finally:
            if context is not None:
                context.clear_values()
            resolver.close()
        if changed:
            output.result(f"VM '{name}' stopped")
        else:
            output.info(f"VM '{name}' is already stopped")


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

    with native_mutation_guard(db.path, vm.name) as guard:
        selected = _require_vm(db, name)
        if selected.vm_uuid != vm.vm_uuid:
            raise StateError("selected VM was replaced", entity_kind="vm", entity_name=name)
        vm = selected
        try:
            vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction, held_native_guard=guard)
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

        sync_ssh_config(config, db)
        output.result(f"VM '{name}' deleted")


def rekey_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    wait_for_share: bool = False,
    ignore_env: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin).

    Useful for rotating keys, switching tailnets, or recovering from
    expired ephemeral keys. Uses the platform's native transport
    (out-of-band) since Tailscale connectivity drops during
    the operation.

    Orchestrated: the walk roots the VM-TEMPLATE node beside the live
    VM node, because the new auth key IS this command's planned op
    (the contrast with reinit, whose graph deliberately excludes the
    template: there the key belongs only to the gate's conditional
    repair path). The sweep predicts the template's declared key and
    the key joins the ONE boundary resolve, mirroring HEAD's interleaved
    preflight-then-single-resolve exactly; this migration is what
    retired the ``preflight_vm_template`` delegate. The running check
    stays past the boundary (a backend status read; on proxmox it
    needs the token), and the activation gate opens AFTER it, exactly
    where HEAD held ``keep_active``: a not-running VM errors before
    any gate, so rekey never auto-starts one outside the same race
    HEAD had.
    """
    import ipaddress
    import time

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

    vm = _require_vm(db, name)
    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, name) as guard:
        selected = _require_vm(db, name)
        if selected.vm_uuid != vm.vm_uuid:
            raise StateError("selected VM was replaced", entity_kind="vm", entity_name=name)
        vm = selected
        _guard_failed_vm(vm)
        _warn_legacy_release(vm)

        # The composition root: construct (registers the site's config
        # secrets), preflight both participating resources (the sweep
        # predicts the new auth key can resolve over the vm-template's
        # declaration; the platform checks its world), then the operation's
        # one resolve pass: the new auth key
        # and any site secret (proxmox's API token) in a single prompt
        # session. The template node roots FIRST so the sweep keeps HEAD's
        # precedence (template readiness before the platform preflight).
        # ``ignore_env`` is honored by temporarily masking the env-var
        # backend for the auth-key secret (the env-var source reads
        # ``os.environ`` during preview and resolution, so removing the var
        # skips it cleanly across BOTH preflight and the
        # resolve, and the prompt backend takes over).
        registry = load_request_registry(config, live_database=db)
        resolver = Resolver(config, registry, interaction=interaction)
        vm_node = live_vm_node(db, config, registry, vm, held_native_guard=guard)
        rekey_vm_tmpl = resolve_live_template(db, registry, vm.name, vm.template)
        tmpl_node = vm_template_node(rekey_vm_tmpl)
        nodes = walk(tmpl_node, vm_node)
        for secret_name in secret_union(nodes):
            resolver.register_name(secret_name)
        # Cache hit by design: the auth key is already in the union (the
        # template node's secret_refs); this call only fetches the DECL,
        # which the --ignore-env env-var mask below needs.
        ts_decl = resolver.register_name(rekey_vm_tmpl.tailscale_auth_key)
        scope = _vm_scope(db, name)
        with _mask_env_var_backend_for(ts_decl, masked=ignore_env):
            preflight_all(
                nodes,
                RunContext(config=config, operation_scope=scope),
                registry=registry,
                interaction=interaction,
            )
            resolver.resolve()
        from agentworks.secrets.line_safety import (
            LineOrientedSecretUse,
            require_line_safe_secret,
        )

        ts_auth_key = require_line_safe_secret(
            resolver.get(rekey_vm_tmpl.tailscale_auth_key),
            use=LineOrientedSecretUse.TAILSCALE,
            secret_name=rekey_vm_tmpl.tailscale_auth_key,
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
