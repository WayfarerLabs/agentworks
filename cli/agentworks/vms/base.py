"""Base interface for VM provisioners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow, VMStatus
    from agentworks.ssh import ExecTarget


@dataclass
class ProvisionResult:
    """Result of VM provisioning -- exec target plus platform metadata."""

    admin_exec_target: ExecTarget
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
    def admin_exec_target(self, vm: VMRow, *, config: object | None = None) -> ExecTarget:
        """Return an ExecTarget for the admin user for a running VM (provisioning transport).

        config is optional; Azure needs it for the SSH identity file when
        connecting via public IP (e.g., during Tailscale logout on delete).
        """

    def vm_active(
        self, vm: VMRow, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        """Hold the VM in an active, reachable state for the duration of the context.

        Default no-op for platforms whose VMs don't disappear under us (Lima,
        Azure, Proxmox). WSL2 overrides to anchor the distro against
        ``vmIdleTimeout`` and -- if the VM already has a Tailscale IP --
        wait for SSH to be reachable before yielding so callers see a
        ready VM.

        Currently wrapped at ``initialize_vm`` only. The design intent is
        for every agentworks operation that touches a VM (reinit, shell,
        exec, agent/workspace/session/console ops, multi-VM probes) to
        wrap in this context so WSL2 idle-shutdown can't bite mid-command
        and Tailscale readiness is verified before the operation starts.
        That sweep is queued as a follow-up PR. For commands that touch
        multiple VMs (e.g. ``session list --status``), enter one
        ``vm_active`` per VM via ``ExitStack``.
        """
        return nullcontext()
