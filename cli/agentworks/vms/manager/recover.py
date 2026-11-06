"""``agw vm recover``: repair what a start cannot, through the platform's root channel.

In order: start the VM when it is stopped; re-establish the VM's host-key
record when it is missing, under the operator's confirmation; run the repair
script; read the tailnet address, settling it (and logging the node out when
it cannot be read) only when the repair joined; wait once for the VM to answer
over SSH; then record the configured SSH identity as the VM's.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.vm_platform.tailscale_join import read_tailnet_address, settle_tailnet_address
from agentworks.db import VMStatus
from agentworks.errors import ConnectivityError, StateError, UserAbort
from agentworks.execution import reachability, trust
from agentworks.execution.access import Admin
from agentworks.vms import recovery

from ._helpers import _guard_incomplete_vm, _operation_log, _require_vm
from .boundary import _auth_key_boundary

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
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
    start, that clears the operator-stopped flag. The auth key resolves in the
    boundary, since the repair joins the tailnet when the node needs a login.
    """
    import agentworks.vms.manager as _mgr
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.vms.applied_state import prepare_configured_ssh_identity

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

    vm_node, _resolver, ops_ctx, auth_key = _auth_key_boundary(db, config, vm, interaction=interaction)
    platform = vm_node.site.platform
    status = platform.status(vm, ops_ctx)
    _require_known_state(status, name)

    with _operation_log(name, "recover", redactions=(auth_key,)) as log:
        # Started before the hold, as start_vm does: WSL2's keepalive boots a
        # stopped distro as a side effect of holding it.
        if status is not VMStatus.RUNNING:
            log.step("Start the VM")
            db.set_operator_stopped(name, False)
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
                platform, vm, ops_ctx, public_key=prepared.public_text, auth_key=auth_key, log=log.record
            )
            hint = f"Run 'agw vm logs {name}' for its output."
            if platform.recovery_needs_guest_sshd:
                hint += (
                    " A broken SSH server on this platform needs a way in that does not use it (a cloud "
                    "provider's serial console), or recreating the VM."
                )
            recovery.require_success(result, script="repair", vm_name=name, hint=hint)
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
                sync_ssh_config(config, db)

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


def _require_known_state(status: VMStatus, name: str) -> None:
    if status is VMStatus.UNKNOWN:
        raise StateError(
            f"the platform could not report the power state of VM '{name}'",
            entity_kind="vm",
            entity_name=name,
            hint=f"Check the VM in its provider, or run 'agw vm describe {name}'.",
        )


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
