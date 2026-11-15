"""VM power-state, deletion, and rekey commands."""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import AgentworksError, StateError, UserAbort, ValidationError
from agentworks.execution import trust
from agentworks.naming import MAX_SECRET_NAME_LENGTH, NAME_RE, validate_name
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import _guard_incomplete_vm, _operation_log, _require_known_power_state, _require_vm
from .boundary import _auth_key_boundary, _live_vm_boundary, _warn_legacy_release
from .tailscale import VMNotAnswering

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
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
    """Accept running intent, then start the retained VM and wait for it to answer.

    The intent is recorded first, whatever the observed state, so a crashed
    start does not leave the gate refusing to auto-resume a VM the operator
    asked to run. A VM with no host-key record is refused before that, since
    the wait could only fail; the refusal names ``agw vm recover``, which
    starts a stopped VM and reads its keys through the platform. A VM that
    does not answer fails naming recover too, which repairs what a start
    cannot.
    """
    import agentworks.execution as execution
    import agentworks.vms.manager as _mgr
    from agentworks.bootstrap import load_request_registry
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    from .runtime import prepare_vm_runtime, start_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    selected = _require_vm(db, name)
    # The wait reaches the VM at its tailnet address through
    # agentworks.execution, so a workstation off Tailscale or an unsupported
    # client refuses before anything is resolved or started.
    _mgr.verify_tailscale_available()
    execution.resolve_ssh()
    with native_mutation_guard(db.path, name) as guard:
        registry = load_request_registry(config, live_database=db)
        vm_node = live_vm_node(db, config, registry, selected, held_native_guard=guard)
        # Before the intent write; prepared runtime's own check comes after it.
        _guard_incomplete_vm(vm_node.reload_row())
        trust.require_record(name)
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
                held_native_guard=guard,
                vm_node=vm_node,
            )
            start_vm_runtime(context)
        finally:
            if context is not None:
                context.clear_values()
            resolver.close()


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
    from agentworks.vms.nodes import live_vm_node

    from .runtime import prepare_vm_runtime, stop_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    selected = _require_vm(db, name)
    with native_mutation_guard(db.path, name) as guard:
        registry = load_request_registry(config, live_database=db)
        vm_node = live_vm_node(db, config, registry, selected, held_native_guard=guard)
        # Before the intent write; prepared runtime's own check comes after it.
        _guard_incomplete_vm(vm_node.reload_row())
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
                held_native_guard=guard,
                vm_node=vm_node,
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

    Child-count and confirmation checks run before binding or secret
    resolution, so a refused or declined delete prompts for and resolves
    nothing; ``interaction`` is checked before binding, whose failures are
    handled below and must not swallow a malformed argument. Once the binding
    holds, the node is logged out of the tailnet through the platform's root
    channel, best effort, and the provider delete follows: whoever deletes a
    VM wants it gone, and ``vm rekey`` rejoins one whose delete then fails.
    Deletion never tears down individual workloads, which the provider delete
    ends, and starts the guest only as a side effect of the logout's hold: a
    WSL2 hold boots a stopped distro, unless the VM's intent is stopped, which
    admits no hold.

    Without ``force``, a row is removed only once its backend VM is deleted,
    because a removed row would leave a surviving backend VM with nothing to
    target it (#329): a binding failure (a broken backend, a removed site or
    plugin, an unresolvable secret) and a bound delete that raises (the
    ``VMPlatform.delete`` contract) both keep the row and the host-key
    record, with a hint naming the backend identifiers the row holds. With
    ``force`` the children go too, and the row and record go whatever kept
    the backend delete from completing, with a warning naming those
    identifiers. ``UserAbort`` always propagates and keeps the row.
    """
    import agentworks.vms.manager as _mgr

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
        backend_left = False
        # Why a row with a tailnet address was not logged out, reported once the
        # row goes; None while nothing kept it from that. A row without one is
        # never logged out.
        not_logged_out: str | None = None
        try:
            vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction, held_native_guard=guard)
        except UserAbort:
            raise
        except Exception as e:
            # Preflight or build failure (unreachable API, missing tool,
            # stranded site, unset or unresolvable secret): nothing can reach
            # the backend, which may still run.
            not_logged_out = f"vm-site '{vm.site}' cannot be bound"
            if not force:
                refusal = StateError(
                    f"VM '{name}' was not deleted: vm-site '{vm.site}' cannot be bound: {e}",
                    entity_kind="vm",
                    entity_name=name,
                    hint=getattr(e, "hint", None),
                )
                refusal.add_hint(_finish_delete_hint(vm, fix="Fix the site", logged_out=False))
                raise refusal from e
            _warn_backend_left(vm, f"vm-site '{vm.site}' cannot be bound: {e}")
            backend_left = True
        else:
            platform = vm_node.site.platform
            # The logout, best effort and inside a hold-only span: it wants the
            # VM up if it happens to be (the WSL2 hold boots a stopped distro),
            # but never gates, since a VM with stopped intent would refuse; such
            # a VM is not held either, since the hold could boot it. A platform
            # that cannot confirm the guest is this VM's is asked nothing (the
            # helper reads its status first), and its delete below refuses or
            # finds the guest gone.
            if vm.tailscale_host:
                logout_ran = False
                try:
                    with vm_node.hold_active():
                        not_logged_out = _mgr._tailscale_logout(vm, platform, ops_ctx)
                        logout_ran = True
                except UserAbort:
                    raise
                except Exception as e:
                    # A hold that fails only as it is released, after the logout
                    # ran, leaves the logout's own result standing: the delete
                    # below ends the VM the hold was keeping up.
                    if not logout_ran:
                        not_logged_out = f"the VM could not be held for its logout ({e})"
            # NOT best-effort: a platform delete that cannot remove the backend
            # VM raises (VMPlatform.delete), which keeps the row unless forced.
            try:
                platform.delete(vm, ops_ctx)
            except UserAbort:
                raise
            except Exception as failure:
                if not force:
                    if isinstance(failure, AgentworksError):
                        failure.add_hint(
                            _finish_delete_hint(
                                vm,
                                fix="Fix what the provider refused",
                                logged_out=bool(vm.tailscale_host) and not_logged_out is None,
                            )
                        )
                    raise
                _warn_backend_left(vm, str(failure))
                backend_left = True
        if vm.tailscale_host and not_logged_out is not None:
            _warn_not_logged_out(vm, not_logged_out)

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
        # backend VM) or --force removed a row whose backend was not deleted.
        try:
            trust.remove(name)
        finally:
            sync_ssh_config(config, db)
        output.result(f"VM '{name}' removed; its backend was not deleted" if backend_left else f"VM '{name}' deleted")


def _finish_delete_hint(vm: VMRow, *, fix: str, logged_out: bool) -> str:
    """How to finish a delete whose backend was not deleted, then, after a logout, how to undo it instead."""
    hint = (
        f"{fix} and run 'agw vm delete {vm.name}' again, or remove its backend yourself "
        f"{_backend_named(vm)} and run 'agw vm delete {vm.name} --force'."
    )
    if logged_out:
        hint += (
            f" The VM was logged out of Tailscale before its delete failed; to put it back on Tailscale "
            f"instead, run 'agw vm rekey {vm.name}'."
        )
    return hint


def _warn_not_logged_out(vm: VMRow, reason: str) -> None:
    """The warning for a deleted VM whose Tailscale node was not logged out, so may still be listed."""
    output.warn(
        f"Tailscale node {vm.hostname} was not logged out: {reason}; "
        f"remove '{vm.hostname}' in the Tailscale admin console if it is listed there."
    )


def _warn_backend_left(vm: VMRow, reason: str) -> None:
    """The warning ``--force`` gives for a row it removes whose backend was not deleted."""
    output.warn(
        f"The backend of VM '{vm.name}' was not deleted ({reason}). It may still run "
        f"{_backend_named(vm)}; remove it there."
    )


def _backend_named(vm: VMRow) -> str:
    """Where to find a VM's backend by hand: its site, the hostname platforms name or tag it with, and its metadata."""
    identifiers = [f"hostname '{vm.hostname}'"]
    identifiers += [f"{key} '{value}'" for key, value in sorted(vm.platform_metadata.items())]
    return f"on vm-site '{vm.site}' ({', '.join(identifiers)})"


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

    ``secret`` selects a declared replacement key for this operation only;
    ``prompt`` forces terminal entry of the template's key instead. Both are
    checked here, before anything is touched, and applied in the boundary.
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

    require_exact_tty_interaction_policy(interaction)
    if secret is not None and prompt:
        raise ValidationError("--secret and --prompt are mutually exclusive")
    if secret is not None:
        validate_name(secret, max_length=MAX_SECRET_NAME_LENGTH)

    vm = _require_vm(db, name)
    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, name) as guard, contextlib.ExitStack() as cleanup:
        selected = _require_vm(db, name)
        if selected.vm_uuid != vm.vm_uuid:
            raise StateError("selected VM was replaced", entity_kind="vm", entity_name=name)
        vm = selected
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
            db, config, vm, secret=secret, prompt=prompt, interaction=interaction, held_native_guard=guard
        )
        cleanup.callback(resolver.close)
        platform = vm_node.site.platform
        status = platform.status(vm, ops_ctx)
        _require_known_power_state(status, name)
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
        # distro against vmIdleTimeout through the script's pauses). A VM whose
        # intent is stopped admits no hold, since a hold could boot it, so a
        # running one is rekeyed without a hold: on WSL2 it can idle out between calls.
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
                # The script never ran, so the node is as it was: once the channel's own hint gets the VM
                # answering, the rekey itself is what is left to do.
                # Not a rebuilt command line: one that dropped any option, a global one included, would
                # change the key, the pause or the interaction policy of the retry.
                error.add_hint(
                    f"Once it answers, run the same 'agw vm rekey {name}' command again, with every option you gave it."
                )
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
