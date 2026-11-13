"""``agw vm recover``: repair what a start cannot, through the platform's root channel.

In order: start the VM when it is stopped; re-establish the VM's host-key
record when it is missing, under the operator's confirmation; run the repair
script; read the tailnet address, settling it (and logging the node out when
it cannot be read) only when the repair joined, and sync the SSH config, whose
aliases name the address and the configured key; wait once for the VM to
answer over SSH; then record the configured SSH identity as the VM's.
"""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.vm_platform.base import offers_native_shell
from agentworks.capabilities.vm_platform.tailscale_join import read_tailnet_address, settle_tailnet_address
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import ConnectivityError, StateError, UserAbort
from agentworks.execution import reachability, trust
from agentworks.execution.access import Admin
from agentworks.vms import recovery

from ._helpers import _guard_incomplete_vm, _operation_log, _require_known_power_state, _require_vm
from .boundary import _auth_key_boundary

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.execution.root_script import RootScriptResult
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.applied_state import PreparedConfiguredSSHIdentity

_OPERATION = "vm-recover"
"""The operation an applied-state slice recover writes is recorded under."""


def recover_vm(db: Database, config: Config, name: str, *, interaction: TtyInteractionPolicy) -> None:
    """Repair a VM's SSH access, python3 and Tailscale node, starting it first when it is stopped.

    Refuses before any secret resolves a VM whose provisioning did not
    complete (delete and recreate it), and after the boundary a VM whose
    power state is unknown. A stopped VM is started through the platform,
    with no wait for SSH, since recover reaches it through the platform's own
    channel: a VM with no host-key record could otherwise never be recovered
    once it stopped, since start's own wait refuses it. Like an explicit
    start, that records running intent. The auth key resolves in the
    boundary, since the repair joins the tailnet when the node needs a login.
    """
    import agentworks.vms.manager as _mgr
    from agentworks.ssh_config import sync_ssh_config_or_warn
    from agentworks.vms.applied_state import prepare_configured_ssh_identity
    from agentworks.vms.initializer.ssh_keys import authorized_keys_content

    vm = _require_vm(db, name)
    _guard_incomplete_vm(vm, allow_failed_init=True)
    if not trust.has_record(name) and not output.is_interactive():
        # Recording keys read through the platform needs the operator to confirm them.
        raise StateError(
            f"VM '{name}' has no recorded host key, and recording one needs an interactive confirmation",
            entity_kind="vm",
            entity_name=name,
            hint=(
                f"Run 'agw vm recover {name}' interactively, or 'agw vm import-host-keys {name} "
                "--known-hosts FILE' with a file whose fingerprints you verified against the VM's console."
            ),
        )
    _mgr.verify_tailscale_available()
    # The final wait runs through agentworks.execution: an unsupported client
    # refuses before a secret or the platform is touched.
    execution.resolve_ssh()
    prepared = prepare_configured_ssh_identity(config.operator.ssh_public_key, config.operator.ssh_private_key)
    authorized_keys = authorized_keys_content(prepared.public_text, config.operator.extra_ssh_public_keys)

    from agentworks.harness_setup.locking import native_mutation_guard

    # The VM family's guard spans the repair, as it does every lifecycle change;
    # the live node's hold shares it.
    with native_mutation_guard(db.path, name) as guard, contextlib.ExitStack() as cleanup:
        selected = _require_vm(db, name)
        if selected.vm_uuid != vm.vm_uuid:
            raise StateError("selected VM was replaced", entity_kind="vm", entity_name=name)
        if (selected.provisioning_status, selected.init_status) != (vm.provisioning_status, vm.init_status):
            # Only a status that changed before the guard needs checking again.
            _guard_incomplete_vm(selected, allow_failed_init=True)
        vm = selected
        vm_node, resolver, ops_ctx, auth_key = _auth_key_boundary(
            db, config, vm, interaction=interaction, held_native_guard=guard
        )
        cleanup.callback(resolver.close)
        platform = vm_node.site.platform
        status = platform.status(vm, ops_ctx)
        _require_known_power_state(status, name)

        with _operation_log(name, "recover", redactions=(auth_key,)) as log:
            # Started before the hold, as start_vm does: WSL2's keepalive boots a
            # stopped distro as a side effect of holding it.
            if status is not VMStatus.RUNNING:
                log.step("Start the VM")
                db.set_vm_desired_runtime_state(name, DesiredRuntimeState.RUNNING, expected_vm_uuid=vm.vm_uuid)
                platform.start(vm, ops_ctx)
                db.record_vm_started(name)
                output.info(f"VM '{name}' was {status.value}; started it")
            with vm_node.hold_active():
                if not trust.has_record(name):
                    log.step("Record the host keys")
                    _reestablish_trust(db, config, vm, platform, ops_ctx)

                log.step("Repair")
                output.info("Repairing SSH access, python3 and Tailscale (this may take several minutes)...")
                result = recovery.repair(
                    platform, vm, ops_ctx, authorized_keys=authorized_keys, auth_key=auth_key, log=log.record
                )
                recovery.require_success(
                    result, script="repair", vm_name=name, hint=_repair_hint(platform, name, result)
                )
                joined = recovery.joined(result)

                log.step("Read the tailnet address")
                run_as_root = partial(platform.run_as_root, vm, ops_ctx)
                if joined:
                    output.info("Tailscale node rejoined the tailnet")
                    address = settle_tailnet_address(
                        run_as_root, node=vm.hostname, rerun_hint=f"Run 'agw vm recover {name}'.", log=log.record
                    )
                else:
                    # A flaky channel never logs out a node that was working.
                    try:
                        address = read_tailnet_address(run_as_root, node=vm.hostname, log=log.record)
                    except ConnectivityError as error:
                        error.add_hint(f"Run 'agw vm logs {name}' for what each read reported.")
                        raise
                if address != vm.tailscale_host:
                    db.update_vm_tailscale(name, address)
                # Whatever the address did: the repair installed the configured key, which may be a new one.
                # The VM is repaired by now, so a failed local write warns rather than failing the recover.
                sync_ssh_config_or_warn(config, db)

                log.step("Wait for SSH")
                wording = reachability.Wording.RECONNECT if joined else reachability.Wording.VERIFY
                access = execution.vm_access(_require_vm(db, name), config, Admin(), log=log.record)
                if not reachability.wait(access, wording=wording):
                    raise ConnectivityError(
                        f"VM '{name}' was repaired but does not answer over SSH at {address}",
                        entity_kind="vm",
                        entity_name=name,
                        hint="Check tailnet sharing and ACLs; if it still does not answer, delete and recreate the VM.",
                    )
                log.step("Record the SSH identity")
                _record_ssh_identity(db, name, prepared)
    output.result(f"VM '{name}' recovered")
    _note_agent_keys(db, name)


def _note_agent_keys(db: Database, name: str) -> None:
    """Say which agent accounts keep their previous key, worded for a key change recover cannot detect.

    The repair rewrites only the admin's ``authorized_keys``. Whether the
    operator's key changed since the agents were set up is not recorded per
    agent, so the note is conditional and printed whenever the VM has agents.
    """
    agents = sorted(agent.name for agent in db.list_agents(vm_name=name))
    if agents:
        output.info(
            f"Recover repairs only the admin's keys. If operator.ssh_private_key changed, the agent accounts on "
            f"VM '{name}' ({', '.join(agents)}) still accept only the previous key until you run "
            "'agw agent reinit <agent>' for each."
        )


def _repair_hint(platform: VMPlatform, name: str, result: RootScriptResult) -> str:
    """What to do after a repair that did not succeed: its log, then a way in by hand.

    A repair that exited with a status ran, so the channel works: the
    platform's native shell goes through the same one (on Lima, its
    persistent connection still reaches a VM whose sshd has stopped). One
    whose outcome is unknown may have lost a channel that needs the guest's
    sshd, which then needs a way in that does not use it.
    """
    hint = f"Run 'agw vm logs {name}' for its output."
    if result.status is not None:
        if offers_native_shell(platform):
            return f"{hint} To fix it by hand (a broken SSH configuration, say), run 'agw vm shell --platform {name}'."
        return f"{hint} {platform.native_shell_unavailable_hint or ''}".rstrip()
    if platform.recovery_needs_guest_sshd:
        hint += (
            " This platform's channel goes through the VM's SSH server: if that is broken, it needs a way in that "
            "does not use it, such as a cloud provider's serial console, or recreating the VM."
        )
    return hint


def _record_ssh_identity(db: Database, name: str, prepared: PreparedConfiguredSSHIdentity) -> None:
    """Record the configured SSH identity as the VM's, as reinit does once its key write is proven.

    The repair installed the configured key and the wait logged in with it
    alone, so the VM accepts it; recording it is what lets a VM whose
    operator changed keys pass the identity check every SSH command makes.
    The proof is reinit's own (``build_vm_initialization_slices``), which
    reads the private key again, so a key replaced while recover ran is not
    recorded: recover fails instead, and a rerun records the key in place.
    """
    from agentworks.db.instance_state import AppliedStateKey
    from agentworks.vms.applied_state import build_vm_initialization_slices
    from agentworks.vms.initializer.ssh_keys import AuthorizedKeysApplied

    slices = build_vm_initialization_slices(
        AuthorizedKeysApplied(prepared.identity, prepared.private_key_ref), include_hardware=False
    )
    if AppliedStateKey.SSH_IDENTITY not in slices:
        raise StateError(
            f"the configured SSH identity changed or became unreadable while VM '{name}' was recovered, "
            "so it was not recorded",
            entity_kind="vm",
            entity_name=name,
            hint=f"Run 'agw vm recover {name}' again.",
        )
    db.instance_state.replace_applied_slices("vm", name, _OPERATION, slices)


def _reestablish_trust(db: Database, config: Config, vm: VMRow, platform: VMPlatform, ctx: RunContext) -> None:
    """Record the VM's host keys, read through its root channel, once the operator confirms them.

    A cloud reads them over SSH under an Unverified file, so nothing here is
    verified: the operator compares the fingerprints with the VM's console. A
    refusal records nothing and stops.
    """
    from agentworks.ssh_config import sync_ssh_config

    keys = platform.read_host_keys(vm, ctx)
    output.info(f"VM '{vm.name}' has no recorded host key. It reports:")
    for key in keys:
        output.info(f"  {key.key_type} {trust.fingerprint(key)}")
    output.info("Compare these with the VM's console (a cloud's serial console or system log) before recording them.")
    if not output.confirm(f"Record these host keys for VM '{vm.name}'?", default=False):
        raise UserAbort("host keys not recorded")
    trust.write_records(vm.name, keys)
    sync_ssh_config(config, db)
