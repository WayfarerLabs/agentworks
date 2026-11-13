"""Phase A: core's first contact with a VM its platform just created.

:func:`bootstrap` runs over ``agentworks.execution`` alone, inside one
fail-closed span:

1. Record: validate the platform's address (a plugin's value), mark
   provisioning in progress, record the address and re-read the row, since an
   access copies the address when it is built.
2. First contact: a polled ``true`` in Enroll mode, the only connections that
   may record the VM's host key.
3. The enrollment post-condition: the store holds a key.
4. The VM contract's base checks over VM-mode connections, release first; the
   attested release is recorded.
5. Provisioning complete.

Any failure in the span marks provisioning failed, records the
``provisioning_failed`` event and re-raises. An interrupt there leaves the
row as it found it: complete is written only once the contract passed, and
every ordinary command refuses a VM that is not complete. However the span
ends, the platform then closes the VM's provisioning access
(``close_provisioning_access``) exactly once. After a span that passed comes
a wait for the VM to answer again.

The log is typed as the small protocol below, which ``SSHLogger`` satisfies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.vm_platform.base import close_provisioning_access_or_warn, parse_tailnet_address
from agentworks.db import ProvisioningStatus
from agentworks.errors import ProvisioningError
from agentworks.execution import reachability, trust
from agentworks.execution.access import Admin
from agentworks.interrupts import interrupts_deferred
from agentworks.path_rendering import format_host_path
from agentworks.vms import contract

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.debian import DebianRelease
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result


class OperationLog(Protocol):
    """What Phase A writes to the create's operation log (``SSHLogger`` satisfies it)."""

    @property
    def display_path(self) -> str: ...

    def step(self, name: str) -> None: ...

    def output(self, text: str) -> None: ...

    def record(self, request: RunRequest, result: Result) -> None: ...


def bootstrap(
    db: Database,
    config: Config,
    vm_name: str,
    platform: VMPlatform,
    ctx: RunContext,
    log: OperationLog,
    *,
    admin_username: str,
    tailnet_address: str,
    expected_release: DebianRelease,
) -> tuple[str, DebianRelease]:
    """Run Phase A and return the recorded address and the attested release.

    ``ctx`` is the create's own scoped context, whose secrets were resolved
    before the platform ran, so even the interrupt arm resolves nothing for
    the first time. The caller owns ``log`` and the availability hold.
    """
    try:
        address = _record_address(db, vm_name, tailnet_address, log)
        vm = db.get_vm(vm_name)
        assert vm is not None, "create_vm inserts the row before Phase A"

        log.step("Verify Tailscale SSH")
        if not reachability.wait(
            execution.enrollment_access(vm, config, log=log.record), wording=reachability.Wording.VERIFY
        ):
            raise ProvisioningError(
                f"VM '{vm_name}' did not become reachable over SSH at {address}",
                entity_kind="vm",
                entity_name=vm_name,
            )
        # A first contact that left no key would leave every later connection
        # refused for want of a record, so fail here, where the cause is known.
        if not trust.has_record(vm_name):
            raise ProvisioningError(
                f"the host key of VM '{vm_name}' could not be recorded in "
                f"'{format_host_path(trust.store_file(vm_name))}'",
                entity_kind="vm",
                entity_name=vm_name,
                hint=f"Run 'agw vm delete {vm_name}' and create it again.",
            )

        log.step("Check the VM contract")
        access = execution.vm_access(vm, config, Admin(), log=log.record)
        report = contract.check_base(access, expected_release=expected_release, admin=admin_username, agents=())
        db.update_vm_debian_release(vm_name, report.release)
        contract.enforce(report, vm_name)

        with db.transaction():
            db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)
            db.insert_vm_event(vm_name, "provisioning_complete", address)
    except Exception as error:
        # Nothing here may replace the original failure. An operator
        # interrupt escapes this arm, leaving the row never complete, which
        # every ordinary command refuses.
        try:
            with db.transaction():
                db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
                db.insert_vm_event(vm_name, "provisioning_failed", str(error))
        except Exception as mark_error:
            output.warn(f"could not mark VM '{vm_name}' failed: {mark_error}")
        output.warn(f"Log: {log.display_path}")
        raise
    finally:
        # The VM is kept whatever happened, so its provisioning access
        # closes on every path: once the contract passed, after a failure,
        # and after an interrupt anywhere in the span.
        _close_provisioning_access(db, vm_name, platform, ctx)

    # A route change can unsettle the network briefly, so wait for the VM to
    # answer again. The VM is attested and complete by now: a wait that runs
    # out warns and proceeds, and one that raises (a changed host key, a
    # refused key) propagates with the row still complete, which create
    # reports as an initialization failure with reinit advice.
    if not reachability.wait(access, wording=reachability.Wording.RECONNECT):
        output.warn("Tailscale SSH did not reconnect after ~240s, proceeding anyway")

    return address, report.release


def _record_address(db: Database, vm_name: str, tailnet_address: str, log: OperationLog) -> str:
    """Validate the platform's address and record it: the platform is a plugin boundary."""
    try:
        address = parse_tailnet_address(tailnet_address)
    except ValueError:
        raise ProvisioningError(
            "the vm-platform returned a tailnet address that is not an assignable IPv4 address",
            entity_kind="vm",
            entity_name=vm_name,
        ) from None
    db.insert_vm_event(vm_name, "provisioning_started", f"ssh {address}")
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)
    log.step("Bootstrap (platform)")
    log.output(f"Tailscale IP: {address}")
    db.update_vm_tailscale(vm_name, address)
    return address


def _close_provisioning_access(db: Database, vm_name: str, platform: VMPlatform, ctx: RunContext) -> None:
    """Close the kept VM's provisioning access, best effort: a failure warns, and what is propagating keeps going.

    The ordinary-command gate refuses a VM that Phase A did not complete,
    ``vm shell --platform`` included, so the operator reaches it through the
    platform's console. An interrupt waits for the close, the row's read
    included (:func:`close_provisioning_access_or_warn`).
    """
    with interrupts_deferred():
        try:
            vm = db.get_vm(vm_name)
        except Exception as read_error:
            output.warn(f"could not close the provisioning access of VM '{vm_name}': {read_error}")
            return
        assert vm is not None, "create_vm inserts the row before Phase A"
        close_provisioning_access_or_warn(platform, vm, ctx)
