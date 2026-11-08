"""VM power-state, deletion, and rekey commands."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.execution import trust
from agentworks.naming import NAME_RE
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import _guard_incomplete_vm, _operation_log, _require_vm
from .boundary import _auth_key_boundary, _live_vm_boundary, _warn_legacy_release
from .tailscale import VMNotAnswering

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
    import agentworks.execution as execution
    import agentworks.vms.manager as _mgr
    from agentworks.bootstrap import load_request_registry

    vm = _require_vm(db, name)
    _guard_incomplete_vm(vm)
    # The wait below reaches the VM at its tailnet address through
    # agentworks.execution, so a workstation off Tailscale or an unsupported
    # client refuses before anything is resolved or started.
    _mgr.verify_tailscale_available()
    execution.resolve_ssh()
    # A VM with no host-key record is not refused here: the start boots it,
    # and the wait below raises TrustMissing, whose hint names
    # 'agw vm recover', which reads the keys through the platform.
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
    # through the wait for SSH below.
    status = platform.status(vm, ops_ctx)
    if status == VMStatus.RUNNING:
        output.info(f"VM '{name}' is already running")
    else:
        platform.start(vm, ops_ctx)
        # Before the wait, so a wait that fails still says the VM is up.
        output.info(f"VM '{name}' started")
        if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
            db.record_vm_started(name)
        else:
            db.clear_vm_start_observation(name)

    # The keepalive anchors the VM through the wait. A VM that does not answer
    # fails naming 'agw vm recover', which repairs what a start cannot.
    with vm_node.hold_active():
        _mgr._await_tailnet(db, config, vm, already_running=status == VMStatus.RUNNING)


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
    _guard_incomplete_vm(vm)
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

    Child-count and confirmation checks run before binding or secret
    resolution, so a refused or declined delete prompts for and resolves
    nothing; ``interaction`` is checked before the best-effort binding span,
    which would otherwise downgrade a malformed argument to a warning. A
    binding failure (a broken backend, a stranded site, an unresolvable
    secret) warns and continues local cleanup. A bound provider delete that
    raises keeps the row and the host-key record for a retry (the
    ``VMPlatform.delete`` contract), because a removed row would leave a
    surviving backend VM with nothing to target it (#329). The one refusal
    ``--force`` removes the row past is a platform's refusal of a row with no
    platform metadata at all, which only a create that ended before its
    platform returned leaves. Deletion never starts the guest, tears down
    individual workloads, or logs the node out first: a refused delete must
    leave a surviving VM untouched. ``UserAbort`` at the boundary's secret
    prompt keeps the row.
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
        backend_left = False
        try:
            vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction)
        except UserAbort:
            raise
        except Exception as e:
            # Preflight or build failure (unreachable API, missing tool,
            # stranded site, unresolvable secret): warn and skip backend
            # cleanup; broken backends are what delete exists to clean up.
            hint = getattr(e, "hint", None)
            output.warn(f"platform binding failed, skipping backend cleanup: {e}" + (f"\n{hint}" if hint else ""))
        else:
            # Whole-VM removal runs first and alone: no session teardown or
            # Tailscale logout precedes it, so a refused delete leaves the
            # surviving VM's workloads and node as they were. The delete is
            # not best-effort: a platform delete that cannot remove the
            # backend VM raises a typed error (VMPlatform.delete), which
            # aborts here and keeps the row. ``--force`` does not soften
            # this, with one exception: a row the platform refuses because it
            # holds no metadata at all, which only a create that ended before
            # the platform returned leaves (create persists the metadata the
            # moment the platform returns). Nothing can target its backend,
            # so the row is all a retry could ever remove.
            try:
                vm_node.site.platform.delete(vm, ops_ctx)
            except StateError as refusal:
                if vm.platform_metadata:
                    raise
                if not force:
                    refusal.add_hint(
                        f"Its create ended before the platform returned. Remove anything named or tagged "
                        f"'{vm.hostname}' on vm-site '{vm.site}' there, then run "
                        f"'agw vm delete --force {name}' to remove the row."
                    )
                    raise
                output.warn(
                    f"VM '{name}' has no platform metadata, so its backend was not deleted. Resources named "
                    f"or tagged '{vm.hostname}' may remain on vm-site '{vm.site}'; remove them there."
                )
                backend_left = True
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
        output.result(f"VM '{name}' removed; its backend was not deleted" if backend_left else f"VM '{name}' deleted")


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

    Useful for rotating keys or switching tailnets. The rekey script runs
    through the platform's root channel, which works without the VM's
    Tailscale, since its connectivity drops during the operation; then the
    new address is settled, recorded, and waited on over SSH.

    Orchestrated: the walk roots the VM-TEMPLATE node beside the live VM node
    (:func:`_auth_key_boundary`), because the new auth key IS this command's
    planned op, and it joins the ONE boundary resolve. The running check stays
    past the boundary (a backend status read; on proxmox it needs the token),
    and the activation gate opens AFTER it: a not-running VM errors before any
    gate, so rekey never auto-starts one outside the race of a VM stopping in
    between.
    """
    import agentworks.execution as execution
    import agentworks.vms.manager as _mgr
    from agentworks.capabilities.vm_platform.tailscale_join import settle_tailnet_address
    from agentworks.execution import reachability
    from agentworks.execution.access import Admin
    from agentworks.execution.root_script import ChannelUnreachable
    from agentworks.orchestration.activation import activation_gate
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.vms import recovery

    vm = _require_vm(db, name)
    _guard_incomplete_vm(vm)
    _warn_legacy_release(vm)
    # Before the auth key resolves: a rekey with no host-key record would spend
    # the new key and record the new address before its reconnect check raised.
    trust.require_record(name)
    # The settle reads the new address from the VM, but the final wait reaches
    # it over the tailnet, so a workstation off Tailscale is refused before the
    # key is spent, as recover refuses it.
    _mgr.verify_tailscale_available()
    # The final wait runs through agentworks.execution: an unsupported client
    # refuses before a secret or the platform is touched.
    execution.resolve_ssh()

    vm_node, resolver, ops_ctx, auth_key = _auth_key_boundary(
        db, config, vm, ignore_env=ignore_env, interaction=interaction
    )
    platform = vm_node.site.platform
    status = platform.status(vm, ops_ctx)
    if status != VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' is not running (status: {status.value})",
            entity_kind="vm",
            entity_name=name,
        )

    recover_hint = f"Run 'agw vm recover {name}' to rejoin it."
    # The log has the key in its redaction set before its first write. The
    # activation gate converges power state (a race from the running check
    # above), then holds the VM for the rekey's duration (WSL2 anchors the
    # distro against vmIdleTimeout through the script's pauses).
    # Boundary-then-gate means the gate callback SERVES the boundary's cached
    # values, never resolves or seeds: the union covers the gate secrets, and
    # ``Resolver.get`` refuses anything outside it.
    with (
        _operation_log(name, "rekey", redactions=(auth_key,)) as logger,
        output.section(f"Rekeying '{name}'"),
        activation_gate(vm_node, resolver.get),
    ):
        output.info("Logging out and joining the tailnet with the new key (about a minute)...")
        logger.step("Rekey")
        try:
            result = recovery.rekey(platform, vm, ops_ctx, auth_key=auth_key, log=logger.record)
        except ChannelUnreachable as error:
            # The script never ran, so the node is as it was; recover starts a stopped VM and repairs it.
            error.add_hint(f"Run 'agw vm recover {name}'.")
            raise
        recovery.require_success(
            result,
            script="rekey",
            vm_name=name,
            hint=f"Run 'agw vm logs {name}' for its output, then 'agw vm recover {name}'.",
        )
        new_ip = settle_tailnet_address(
            partial(platform.run_as_root, vm, ops_ctx), node=vm.hostname, rerun_hint=recover_hint, log=logger.record
        )

        # Update DB and SSH config with the new IP (correct regardless of
        # reachability: the old IP is definitely dead after logout)
        db.update_vm_tailscale(name, new_ip)
        sync_ssh_config(config, db)
        db.insert_vm_event(name, "rekey", f"new_ip={new_ip}")

        # If the operator needs to share the VM back, pause before connectivity check
        if wait_for_share:
            output.pause("Share the VM back to your tailnet, then press Enter to verify connectivity...")

        output.info(f"Verifying SSH to {new_ip}...")
        access = execution.vm_access(_require_vm(db, name), config, Admin(), log=logger.record)
        # The new address is recorded either way: the old one died with the logout.
        if not reachability.wait(access, wording=reachability.Wording.RECONNECT):
            raise VMNotAnswering(name, new_ip)
        output.result(f"VM '{name}' rekeyed successfully. Tailscale IP: {new_ip}")
