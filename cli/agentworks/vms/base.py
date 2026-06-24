"""Base interface for VM provisioners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow, VMStatus
    from agentworks.transports import Transport


@dataclass
class ProvisionResult:
    """Result of VM provisioning: provisioner transport plus platform metadata."""

    provisioner_transport: Transport
    azure_resource_id: str | None = None
    wsl_distro_name: str | None = None
    proxmox_vmid: str | None = None
    bootstrap_complete: bool = False
    tailscale_ip: str | None = None


class VMProvisioner(ABC):
    """Interface that each platform provisioner must implement."""

    @abstractmethod
    def create(self, vm_name: str, config: Config) -> ProvisionResult:
        """Create a raw VM and return provisioning result for the initializer."""

    @abstractmethod
    def start(self, vm: VMRow) -> None:
        """Start a stopped VM."""

    @abstractmethod
    def stop(self, vm: VMRow) -> None:
        """Stop a running VM."""

    @abstractmethod
    def delete(self, vm: VMRow) -> None:
        """Delete a VM and clean up platform resources."""

    @abstractmethod
    def status(self, vm: VMRow) -> VMStatus:
        """Query the live runtime status of a VM."""

    @abstractmethod
    def provisioner_transport(self, vm: VMRow, *, config: object | None = None) -> Transport:
        """Return the platform-native :class:`Transport` for a running VM.

        Used at bootstrap (Phase A) and via ``vm shell --provisioner``.
        Callers should reach for this through
        :func:`agentworks.transports.provisioner_transport` which wraps
        the call in :meth:`transient_route` for the polymorphic lifecycle
        (Azure's transient public IP, etc.) and applies the reachability
        probe.

        ``config`` is optional; Azure needs it for the SSH identity file
        when connecting via public IP. Raises ``NotImplementedError``
        from the Proxmox provisioner (one-shot QEMU guest agent exec
        can't host an interactive shell).
        """

    def post_tailscale_ready(self, vm: VMRow) -> None:  # noqa: B027 -- intentional concrete no-op
        """Hook called once the VM's Tailscale node is up during create.

        Default no-op. Azure overrides to detach the cloud-init public
        IP at the moment Tailscale becomes reachable, minimizing the
        window the VM is exposed to the internet. The asymmetry vs.
        :meth:`transient_route` is genuine: the matching attach lives
        inside :meth:`create` (cloud-init bootstrap needs the IP) and
        the detach fires at an async Tailscale-ready point inside
        :func:`initialize_vm`, neither of which is an ExitStack-shaped
        lifecycle.
        """

    def transient_route(self, vm: VMRow) -> AbstractContextManager[None]:
        """Hold any platform-native transient network state while the
        provisioner transport is in use.

        Default no-op (:func:`contextlib.nullcontext`) for platforms
        whose provisioner transport works without setup (Lima, WSL2,
        Proxmox). Azure overrides to attach a public IP on enter and
        detach on exit so the transient state is bounded by the
        caller's :class:`contextlib.ExitStack` scope.

        Always called from
        :func:`agentworks.transports.provisioner_transport` before the
        per-platform :meth:`provisioner_transport` builder runs, so
        polymorphism replaces what used to be an
        ``isinstance(prov, AzureProvisioner)`` branch in the caller.
        """
        return nullcontext()

    def vm_active(
        self, vm: VMRow, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        """Hold the VM in an active, reachable state for the duration of the context.

        Default no-op for platforms whose VMs don't disappear under us (Lima,
        Azure, Proxmox). WSL2 overrides to anchor the distro against
        ``vmIdleTimeout`` and -- if the VM already has a Tailscale IP --
        wait for SSH to be reachable before yielding so callers see a
        ready VM.

        Every manager-layer function that touches a VM wraps in this
        context via ``keep_vm_active(db, config, vm)`` (or
        ``keep_vms_active(...)`` for multi-VM operations) defined in
        ``vms/manager.py``. Don't call ``vm_active`` directly outside
        the helper; the helper handles the provisioner dispatch.
        Exceptions to the greedy-wrap rule are deliberately not wrapped
        and documented at their call sites: ``stop_vm`` (would fight
        ``wsl --terminate``), all ``describe_*`` (degrade silently when
        the VM is unreachable, or auto-boot via ``_ensure_vm_running``),
        and the multi_console best-effort ops (forcing a boot would
        change their semantics).
        """
        return nullcontext()
