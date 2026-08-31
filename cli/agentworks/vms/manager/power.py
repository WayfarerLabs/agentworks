"""VM power-state, deletion, and rekey commands."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.naming import NAME_RE
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import (
    _guard_failed_vm,
    _lookup_or_synthesize_secret,
    _mask_env_var_backend_for,
    _require_vm,
    _vm_scope,
)
from .boundary import _live_vm_boundary, _platform_ops_ctx, _warn_legacy_release

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import LiveVMNode


def start_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Start a VM under shared checkpoint exclusion."""

    from .operation_guard import shared_vm_operation_guard

    with shared_vm_operation_guard(db, name, operation="start VM"):
        _start_vm(db, config, name, interaction=interaction)


def _start_vm(
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
    # Only emit "is ready" on the path that actually started the VM. When
    # status was already RUNNING we already said so above, and Tailscale
    # verification is usually a no-op (handshake already valid), so an
    # extra "is ready" line is just noise. On the real-work path it
    # confirms tailscaled finished its handshake.
    if status != VMStatus.RUNNING:
        output.info(f"VM '{name}' is ready")


def stop_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Stop a VM under shared checkpoint exclusion."""

    from .operation_guard import shared_vm_operation_guard

    with shared_vm_operation_guard(db, name, operation="stop VM"):
        _stop_vm(db, config, name, interaction=interaction)


def _stop_vm(
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
    """Delete a VM while excluding checkpoint and upgrade operations."""

    from .operation_guard import exclusive_vm_operation_guard

    with exclusive_vm_operation_guard(db, name, operation="delete VM"):
        _delete_vm(
            db,
            config,
            name,
            force=force,
            yes=yes,
            interaction=interaction,
        )


def _delete_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Delete a VM, cleaning up all associated resources.

    Orchestrated, composition only: the child-count guard and the
    confirm gate stay pre-boundary (zero prompts and zero resolves on
    a refused or declined delete), then the whole build-and-boundary
    composition (:func:`_live_vm_boundary`) is BEST-EFFORT: a broken
    backend, a stranded site, or an unresolvable secret warns and
    skips backend cleanup, because broken states are exactly what
    delete exists to clean up. The backend delete op itself is the
    one step that is NOT best-effort: a platform delete that cannot
    remove the backend VM raises (the ``VMPlatform.delete`` contract)
    and the row survives for a retry, because a deleted row leaves a
    surviving backend VM orphaned with nothing to target it (#329).
    No activation gate ever opens (an operator-stopped VM would
    refuse; deletion never starts a stopped VM), and the Tailscale
    logout uses a hold-only span. ``UserAbort`` is the one exception
    the best-effort spans may not downgrade: an abort at the
    boundary's secret prompt or inside an op span must keep the row.
    ``interaction`` is checked HERE rather than being left to the
    boundary's resolver: the boundary sits inside the best-effort span,
    which would downgrade the rejection to a warning and then delete the
    row and the operator's SSH entry while skipping the backend delete,
    orphaning the VM the span exists to protect (#329). The span
    tolerates a broken backend, not a malformed argument.
    """
    import agentworks.vms.manager as _mgr

    require_exact_tty_interaction_policy(interaction)
    vm = _require_vm(db, name)

    # Check for workspaces (which contain agents and sessions)
    ws_count = db.count_workspaces_on_vm(name)
    ag_count = db.count_agents_on_vm(name)
    ts_count = db.count_sessions_on_vm(name)
    has_children = ws_count > 0

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

    if not yes and not force:
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

    checkpoint_pending = db.get_vm_checkpoint(name) is not None
    if checkpoint_pending:
        output.info(f"Deleting VM '{name}' managed checkpoint first...")

    # Resolve once for checkpoint, platform, and Tailscale cleanup.
    vm_node: LiveVMNode | None
    ops_ctx: RunContext | None = None
    try:
        vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction)
    except UserAbort:
        # Ctrl-C at the boundary's secret prompt must keep the SIGINT
        # contract: abort the whole delete rather than orphaning the
        # backend VM behind a warn. (The boundary helper runs the
        # preflight sweep and the resolve pass, so the prompt happens
        # inside it.)
        raise
    except Exception as e:
        if checkpoint_pending and not force:
            # A managed checkpoint is an independently billed/recoverable
            # provider artifact. Keep both rows when its exact boundary cannot
            # be resolved; best-effort VM-row cleanup must not orphan it.
            raise
        if checkpoint_pending:
            from .checkpoints import _abandon_checkpoint

            _abandon_checkpoint(db, name, error=e, yes=True)
        # Preflight or build failure (unreachable API, missing tool,
        # stranded site, unresolvable secret): warn and skip backend
        # cleanup; broken backends are what delete exists to clean up.
        vm_node = None
        hint = getattr(e, "hint", None)
        output.warn(f"platform binding failed, skipping backend cleanup: {e}" + (f"\n{hint}" if hint else ""))

    if vm_node is not None:
        assert ops_ctx is not None  # set beside vm_node above
        platform = vm_node.site.platform
        if checkpoint_pending:
            from .checkpoints import _delete_checkpoint_with_boundary

            _delete_checkpoint_with_boundary(
                db,
                vm,
                platform,
                ops_ctx,
                yes=True,
                force=force,
            )
        # Tailscale logout (best-effort, hold-only): the logout wants
        # the VM alive if it happens to be, but delete must NOT gate:
        # an operator-stopped VM would raise. (The WSL2 hold does boot a
        # stopped distro; the logout genuinely needs the VM up.) The
        # whole hold+logout span is best-effort: broken states (e.g. a
        # manually unregistered WSL2 distro whose hold raises) are
        # exactly what `vm delete` exists to clean up, so nothing here
        # may skip the delete below. UserAbort is the one exception the
        # catch-alls must NOT downgrade: a swallowed abort would fall
        # through and delete the DB row the operator just declined.
        if vm.tailscale_host:
            try:
                with vm_node.hold_active():
                    _mgr._tailscale_logout(vm, config, platform, ops_ctx)
            except UserAbort:
                raise
            except Exception as e:
                output.warn(f"tailscale logout skipped: {e}")

        # NOT best-effort, unlike the spans above: the platform's delete
        # contract (VMPlatform.delete) is that a delete which cannot
        # remove the backend VM raises a typed error, and that error
        # aborts the command HERE, keeping the row. Warning past it and
        # deleting the row would orphan a surviving backend VM with
        # nothing left to target it (#329). ``--force`` does not soften
        # this: force skips the child-count guard and the confirm
        # prompt, never a failed backend delete.
        platform.delete(vm, ops_ctx)

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
    """Rekey a VM under shared checkpoint exclusion."""

    from .operation_guard import shared_vm_operation_guard

    with shared_vm_operation_guard(db, name, operation="rekey VM"):
        _rekey_vm(
            db,
            config,
            name,
            wait_for_share=wait_for_share,
            ignore_env=ignore_env,
            interaction=interaction,
        )


def _rekey_vm(
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
    vm_node = live_vm_node(db, config, registry, vm)
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
