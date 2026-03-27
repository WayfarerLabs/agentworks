"""Azure VM provisioner -- creates and manages VMs via the Azure SDK."""

from __future__ import annotations

import base64
import contextlib
from typing import TYPE_CHECKING, Protocol

import typer

from agentworks.db import VMStatus
from agentworks.ssh import ExecTarget, SSHTarget
from agentworks.vms.base import ProvisionResult, VMProvisioner

if TYPE_CHECKING:
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    from agentworks.config import Config
    from agentworks.db import VMRow


class _HasSubscriptionId(Protocol):
    """Structural protocol for anything with subscription_id (AzureConfig or _MinimalAzureConfig)."""

    @property
    def subscription_id(self) -> str: ...


from agentworks.ssh import SSHError
from agentworks.vms.bootstrap_script import generate_bootstrap_script
from agentworks.vms.cloud_init import SYSTEM_PACKAGES, generate_cloud_init


class AzureError(RuntimeError):
    """An Azure API operation failed.

    Attributes:
        summary: A concise, user-facing error message.
        detail: The full error details (for logs).
    """

    def __init__(self, summary: str, detail: str) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


def _wrap_azure_error(exc: Exception) -> AzureError:
    """Convert an Azure SDK exception into an AzureError."""
    from azure.core.exceptions import HttpResponseError

    if isinstance(exc, HttpResponseError):
        # Walk inner errors to find the most specific message
        code = exc.error.code if exc.error else None
        message = exc.error.message if exc.error else str(exc)

        if exc.error and exc.error.details:
            inner = exc.error.details[0]
            code = inner.code or code
            message = inner.message or message

        summary = f"{code}: {_trim_message(str(message))}" if code else _trim_message(str(message))
        return AzureError(summary, detail=str(exc))

    return AzureError(str(exc), detail=str(exc))


def _trim_message(message: str) -> str:
    """Trim an Azure error message to the first meaningful sentence."""
    # Cut at first URL or "Learn more" / "Submit a request" noise
    for marker in [". Setup Alerts", ". Learn more", ". Submit a request", " https://"]:
        idx = message.find(marker)
        if idx != -1:
            return message[: idx + 1] if marker.startswith(".") else message[:idx]
    return message


def _get_credential() -> object:
    """Get an Azure credential.

    Tries DefaultAzureCredential first (picks up az login, env vars,
    managed identity, etc.). Falls back to interactive browser login
    if nothing else works.

    Returns object to avoid a hard import of azure.core at module load time.
    Callers cast to the appropriate type when constructing SDK clients.
    """
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

    cred = DefaultAzureCredential()
    try:
        cred.get_token("https://management.azure.com/.default")
        return cred
    except ClientAuthenticationError:
        typer.echo("No Azure credentials found, opening browser for login...")
        return InteractiveBrowserCredential()


def _compute_client(az: _HasSubscriptionId) -> ComputeManagementClient:
    """Create a ComputeManagementClient."""
    from azure.mgmt.compute import ComputeManagementClient

    # _get_credential() returns a TokenCredential-compatible object; the cast
    # avoids a hard azure.core import at module load time.
    return ComputeManagementClient(_get_credential(), az.subscription_id)  # type: ignore[arg-type]


def _network_client(az: _HasSubscriptionId) -> NetworkManagementClient:
    """Create a NetworkManagementClient."""
    from azure.mgmt.network import NetworkManagementClient

    # Same as _compute_client: credential is TokenCredential-compatible at runtime.
    return NetworkManagementClient(_get_credential(), az.subscription_id)  # type: ignore[arg-type]


class AzureProvisioner(VMProvisioner):
    """Provisions Azure VMs via the Azure Python SDK."""

    def create(
        self,
        vm_name: str,
        config: Config,
        *,
        azure_vm_size: str = "Standard_B2s",
        admin_username: str = "agentworks",
        tailscale_auth_key: str | None = None,
    ) -> ProvisionResult:
        assert config.azure is not None, "Azure config is required"
        az = config.azure

        typer.echo("Connecting to Azure...")
        typer.echo(f"Provisioning Azure VM '{vm_name}' in {az.region} (size: {azure_vm_size})...")
        if config.vm.swap_gb > 0:
            typer.echo(f"  Swap: {config.vm.swap_gb} GiB")

        ssh_pub_key = config.user.ssh_public_key.read_text().strip()

        # Generate the same bootstrap script used by Lima, wrapped in
        # cloud-init write_files + runcmd for delivery via Azure custom_data.
        if tailscale_auth_key:
            bootstrap = generate_bootstrap_script(
                admin_username=admin_username,
                ssh_public_key=ssh_pub_key,
                system_packages=SYSTEM_PACKAGES,
                tailscale_auth_key=tailscale_auth_key,
                swap_gb=config.vm.swap_gb,
            )
            cloud_init = generate_cloud_init(bootstrap)
        else:
            # No Tailscale key -- minimal cloud-init, bootstrap deferred to Phase A
            cloud_init = (
                "#cloud-config\n"
                "package_update: true\n"
                "packages:\n"
                "  - openssh-server\n"
            )
        cloud_init_b64 = base64.b64encode(cloud_init.encode()).decode()

        compute = _compute_client(az)
        network = _network_client(az)

        try:
            # Create public IP
            typer.echo("  Creating public IP...")
            ip_poller = network.public_ip_addresses.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-ip",
                {
                    "location": az.region,
                    "sku": {"name": "Standard"},
                    "public_ip_allocation_method": "Static",
                    "tags": {"owner": "agentworks"},
                },
            )
            ip_result = ip_poller.result()
            public_ip = ip_result.ip_address or ""

            # Create NSG with SSH rule
            typer.echo("  Creating network security group...")
            nsg_poller = network.network_security_groups.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-nsg",
                {
                    "location": az.region,
                    "security_rules": [
                        {
                            "name": "SSH",
                            "protocol": "Tcp",
                            "source_port_range": "*",
                            "destination_port_range": "22",
                            "source_address_prefix": "*",
                            "destination_address_prefix": "*",
                            "access": "Allow",
                            "priority": 1000,
                            "direction": "Inbound",
                        }
                    ],
                    "tags": {"owner": "agentworks"},
                },
            )
            nsg_result = nsg_poller.result()

            # Create NIC
            typer.echo("  Creating network interface...")

            # Need a subnet -- use default VNet or create one
            vnet_name = f"{vm_name}-vnet"
            subnet_name = "default"
            vnet_poller = network.virtual_networks.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                vnet_name,
                {
                    "location": az.region,
                    "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                    "subnets": [
                        {
                            "name": subnet_name,
                            "address_prefix": "10.0.0.0/24",
                        }
                    ],
                    "tags": {"owner": "agentworks"},
                },
            )
            vnet_result = vnet_poller.result()
            subnet_id = vnet_result.subnets[0].id

            nic_poller = network.network_interfaces.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                f"{vm_name}-nic",
                {
                    "location": az.region,
                    "ip_configurations": [
                        {
                            "name": "default",
                            "subnet": {"id": subnet_id},
                            "public_ip_address": {"id": ip_result.id},
                        }
                    ],
                    "network_security_group": {"id": nsg_result.id},
                    "tags": {"owner": "agentworks"},
                },
            )
            nic_result = nic_poller.result()

            # Create VM
            typer.echo("  Creating VM...")
            vm_poller = compute.virtual_machines.begin_create_or_update(  # type: ignore[call-overload]
                az.resource_group,
                vm_name,
                {
                    "location": az.region,
                    "hardware_profile": {"vm_size": azure_vm_size},
                    "storage_profile": {
                        "image_reference": {
                            "publisher": "Debian",
                            "offer": "debian-12",
                            "sku": "12-gen2",
                            "version": "latest",
                        },
                        "os_disk": {
                            "create_option": "FromImage",
                            "managed_disk": {"storage_account_type": "StandardSSD_LRS"},
                        },
                    },
                    "os_profile": {
                        "computer_name": vm_name,
                        "admin_username": admin_username,
                        "custom_data": cloud_init_b64,
                        "linux_configuration": {
                            "disable_password_authentication": True,
                            "ssh": {
                                "public_keys": [
                                    {
                                        "path": f"/home/{admin_username}/.ssh/authorized_keys",
                                        "key_data": ssh_pub_key,
                                    }
                                ]
                            },
                        },
                    },
                    "network_profile": {
                        "network_interfaces": [{"id": nic_result.id}],
                    },
                    "tags": {"owner": "agentworks"},
                },
            )
            vm_result = vm_poller.result()
            resource_id = vm_result.id or ""

        except Exception as exc:
            typer.echo("  Cleaning up resources...")
            _cleanup_vm_resources(compute, network, az.resource_group, vm_name)
            raise _wrap_azure_error(exc) from exc

        typer.echo(f"  Azure VM '{vm_name}' provisioned (IP: {public_ip}).")

        import sys

        exec_target = ExecTarget(
            ssh=SSHTarget(
                host=public_ip,
                user=admin_username,
                identity_file=config.user.ssh_private_key,
                force_tty=sys.platform == "win32",
            )
        )

        # If bootstrap was embedded in cloud-init, wait for it to finish
        # and extract the Tailscale IP.
        tailscale_ip = None
        bootstrap_complete = False
        if tailscale_auth_key:
            tailscale_ip = self._wait_for_bootstrap(exec_target, vm_name)
            if tailscale_ip:
                bootstrap_complete = True

        return ProvisionResult(
            exec_target=exec_target,
            azure_resource_id=resource_id or None,
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def _wait_for_bootstrap(self, exec_target: ExecTarget, vm_name: str) -> str | None:
        """Wait for cloud-init to finish and return the Tailscale IP.

        SSH may not be immediately available after VM creation, so we retry.
        Returns None if we cannot get the IP (Phase A will handle it).
        """
        import time

        from agentworks.ssh import run as ssh_run

        typer.echo("  Waiting for cloud-init bootstrap to complete (this may take several minutes)...")

        # Wait for SSH to become available
        assert exec_target.ssh is not None
        for attempt in range(30):
            try:
                ssh_run(exec_target.ssh, "echo ok", check=True, timeout=10)
                break
            except SSHError:
                if attempt == 29:
                    typer.echo("  Warning: SSH not available, deferring bootstrap to Phase A", err=True)
                    return None
                time.sleep(10)

        # Wait for cloud-init to finish
        try:
            ssh_run(
                exec_target.ssh,
                "cloud-init status --wait",
                check=True,
                timeout=600,
            )
        except SSHError as e:
            typer.echo(f"  Warning: cloud-init wait failed: {e}", err=True)
            typer.echo("  Deferring bootstrap to Phase A", err=True)
            return None

        # Get Tailscale IP
        try:
            result = ssh_run(exec_target.ssh, "sudo tailscale ip -4", check=True, timeout=15)
            tailscale_ip = result.stdout.strip()
            typer.echo(f"  Tailscale IP: {tailscale_ip}")
            return tailscale_ip
        except SSHError as e:
            typer.echo(f"  Warning: could not retrieve Tailscale IP: {e}", err=True)
            return None

    def start(self, vm: VMRow) -> None:
        typer.echo(f"Starting Azure VM '{vm.name}'...")
        assert vm.azure_resource_id is not None
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        try:
            compute = _compute_client(az_cfg)
            compute.virtual_machines.begin_start(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        typer.echo(f"Azure VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        typer.echo(f"Deallocating Azure VM '{vm.name}'...")
        assert vm.azure_resource_id is not None
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        try:
            compute = _compute_client(az_cfg)
            compute.virtual_machines.begin_deallocate(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        typer.echo(f"Azure VM '{vm.name}' deallocated")

    def delete(self, vm: VMRow) -> None:
        typer.echo(f"Deleting Azure VM '{vm.name}'...")
        if vm.azure_resource_id is None:
            typer.echo("Warning: no Azure resource ID, skipping Azure cleanup")
            return

        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        compute = _compute_client(az_cfg)
        network = _network_client(az_cfg)

        # Delete VM first (must complete before dependent resources)
        with contextlib.suppress(Exception):
            compute.virtual_machines.begin_delete(rg, name).result()

        _cleanup_vm_resources(compute, network, rg, name)

        typer.echo(f"Azure VM '{vm.name}' deleted")

    def attach_public_ip(self, vm: VMRow) -> str:
        """Attach a temporary public IP to the VM's NIC. Returns the IP address."""
        assert vm.azure_resource_id is not None
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        network = _network_client(az_cfg)

        try:
            # Create (or re-create) the public IP
            typer.echo("  Attaching temporary public IP...")
            ip_poller = network.public_ip_addresses.begin_create_or_update(  # type: ignore[call-overload]
                rg,
                f"{name}-ip",
                {
                    "location": _get_vm_location(vm),
                    "sku": {"name": "Standard"},
                    "public_ip_allocation_method": "Static",
                    "tags": {"owner": "agentworks"},
                },
            )
            ip_result = ip_poller.result()

            # Attach to NIC
            nic = network.network_interfaces.get(rg, f"{name}-nic")
            if nic.ip_configurations:
                # Azure SDK accepts dict for PublicIPAddress at runtime despite type stubs
                nic.ip_configurations[0].public_ip_address = {"id": ip_result.id}  # type: ignore[assignment]
            network.network_interfaces.begin_create_or_update(
                rg,
                f"{name}-nic",
                nic,
            ).result()

        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

        return ip_result.ip_address or ""

    def detach_public_ip(self, vm: VMRow) -> None:
        """Detach and delete the public IP from the VM's NIC."""
        assert vm.azure_resource_id is not None
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        network = _network_client(az_cfg)

        typer.echo("  Removing public IP...")
        # Detach from NIC
        with contextlib.suppress(Exception):
            nic = network.network_interfaces.get(rg, f"{name}-nic")
            if nic.ip_configurations:
                nic.ip_configurations[0].public_ip_address = None
            network.network_interfaces.begin_create_or_update(
                rg,
                f"{name}-nic",
                nic,
            ).result()

        # Delete the public IP resource
        with contextlib.suppress(Exception):
            network.public_ip_addresses.begin_delete(rg, f"{name}-ip").result()

    def exec_target(self, vm: VMRow) -> ExecTarget:
        assert vm.azure_resource_id is not None
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        try:
            compute = _compute_client(az_cfg)
            vm_info = compute.virtual_machines.get(
                rg,
                name,
                expand="instanceView",
            )
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

        # Walk NICs to find the public IP (may not exist if detached)
        public_ip = _get_vm_public_ip(vm_info, az_cfg)
        import sys

        return ExecTarget(
            ssh=SSHTarget(host=public_ip, user=vm.admin_username, force_tty=sys.platform == "win32"),
        )

    def status(self, vm: VMRow) -> VMStatus:
        if vm.azure_resource_id is None:
            return VMStatus.UNKNOWN
        rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
        try:
            compute = _compute_client(az_cfg)
            instance = compute.virtual_machines.instance_view(rg, name)
        except Exception:
            return VMStatus.UNKNOWN

        for s in instance.statuses or []:
            code = s.code or ""
            if code == "PowerState/running":
                return VMStatus.RUNNING
            if code == "PowerState/stopped":
                return VMStatus.STOPPED
            if code == "PowerState/deallocated":
                return VMStatus.DEALLOCATED
        return VMStatus.UNKNOWN


def _get_vm_public_ip(vm_info: object, az_cfg: _HasSubscriptionId) -> str:
    """Resolve the public IP address for a VM from its NIC."""
    network = _network_client(az_cfg)

    nic_refs = (
        getattr(
            getattr(vm_info, "network_profile", None),
            "network_interfaces",
            [],
        )
        or []
    )
    for nic_ref in nic_refs:
        nic_id = nic_ref.id
        if not nic_id:
            continue
        # Parse NIC resource group and name from ID
        parts = nic_id.split("/")
        rg_idx = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
        nic_rg = parts[rg_idx + 1]
        nic_name = parts[-1]

        nic = network.network_interfaces.get(nic_rg, nic_name)
        for ip_config in nic.ip_configurations or []:
            pip_ref = ip_config.public_ip_address
            if pip_ref and pip_ref.id:
                pip_parts = pip_ref.id.split("/")
                pip_rg_idx = next(i for i, p in enumerate(pip_parts) if p.lower() == "resourcegroups")
                pip_rg = pip_parts[pip_rg_idx + 1]
                pip_name = pip_parts[-1]
                pip = network.public_ip_addresses.get(pip_rg, pip_name)
                if pip.ip_address:
                    return pip.ip_address
    return ""


def _cleanup_vm_resources(
    compute: ComputeManagementClient,
    network: NetworkManagementClient,
    rg: str,
    name: str,
) -> None:
    """Best-effort cleanup of all resources associated with a VM."""
    for cleanup in [
        lambda: network.network_interfaces.begin_delete(rg, f"{name}-nic").result(),
        lambda: network.public_ip_addresses.begin_delete(rg, f"{name}-ip").result(),
        lambda: network.network_security_groups.begin_delete(rg, f"{name}-nsg").result(),
        lambda: network.virtual_networks.begin_delete(rg, f"{name}-vnet").result(),
    ]:
        with contextlib.suppress(Exception):
            cleanup()  # type: ignore[no-untyped-call]

    # OS disk name is generated by Azure, find by tag
    with contextlib.suppress(Exception):
        for disk in compute.disks.list_by_resource_group(rg):
            disk_name = disk.name or ""
            if disk.tags and disk.tags.get("owner") == "agentworks" and name in disk_name and disk_name:
                compute.disks.begin_delete(rg, disk_name).result()


def _get_vm_location(vm: VMRow) -> str:
    """Get the Azure region for a VM by querying the compute API."""
    assert vm.azure_resource_id is not None
    rg, name, az_cfg = _parse_resource_id(vm.azure_resource_id)
    compute = _compute_client(az_cfg)
    vm_info = compute.virtual_machines.get(rg, name)
    return vm_info.location or "eastus"


class _MinimalAzureConfig:
    """Minimal config for SDK clients, parsed from a resource ID."""

    def __init__(self, subscription_id: str) -> None:
        self.subscription_id = subscription_id


def _parse_resource_id(resource_id: str) -> tuple[str, str, _MinimalAzureConfig]:
    """Extract resource group, VM name, and a config from an Azure resource ID."""
    parts = resource_id.split("/")
    sub_idx = next(i for i, p in enumerate(parts) if p.lower() == "subscriptions")
    rg_idx = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
    name_idx = next(i for i, p in enumerate(parts) if p.lower() == "virtualmachines")
    cfg = _MinimalAzureConfig(parts[sub_idx + 1])
    return parts[rg_idx + 1], parts[name_idx + 1], cfg
