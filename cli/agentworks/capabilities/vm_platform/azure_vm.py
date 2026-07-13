"""The Azure VM platform: creates and manages VMs via the Azure SDK."""

from __future__ import annotations

import base64
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES, generate_cloud_init
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, ProvisioningError, StateError
from agentworks.ssh import SSHError
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport


class _HasSubscriptionId(Protocol):
    """Structural protocol for anything with subscription_id (AzureConfig or _MinimalAzureConfig)."""

    @property
    def subscription_id(self) -> str: ...


class AzureError(ProvisioningError):
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
        output.info("No Azure credentials found, opening browser for login...")
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


_AZURE_REQUIRED_KEYS = ("subscription_id", "resource_group", "region")


class AzureVMPlatform(VMPlatform):
    """Runs VMs on the Azure Virtual Machines service via the Azure
    Python SDK. Named ``azure-vm``, not ``azure``: the capability is
    one specific Azure service, and other Azure services could plausibly
    back platforms of their own someday."""

    name: ClassVar[str] = "azure-vm"
    description: ClassVar[str] = "Azure Virtual Machines (subscription + resource group)"

    # No preflight override: azure has no config secrets (the base's
    # prediction pass is a no-op) and no unauthenticated readiness
    # check worth making. A credential probe is deliberately NOT one:
    # verifying credentials before the resolve/credential stage forks
    # behavior on where they happen to come from (a non-interactive
    # chain passes, the browser-login fallback can't be probed without
    # BEING the interaction). Credential and reachability failures
    # surface at the op with typed errors (``_wrap_azure_error``),
    # which is the contract: preflight is capped at what it can check
    # without resolved credentials.

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        for key in _AZURE_REQUIRED_KEYS:
            value = config.get(key)
            if not isinstance(value, str) or not value:
                raise ConfigError(
                    f"{owner}.{key} is required for the azure-vm platform and "
                    f"must be a non-empty string"
                )
        unknown = sorted(set(config) - set(_AZURE_REQUIRED_KEYS))
        if unknown:
            raise ConfigError(
                f"{owner}: unknown azure-vm platform field(s): {', '.join(unknown)}"
            )
        return ()

    @classmethod
    def legacy_platform_metadata(
        cls, row: Mapping[str, Any], legacy: Mapping[str, Any]
    ) -> dict[str, str]:
        if row["azure_resource_id"]:
            return {"resource_id": str(row["azure_resource_id"])}
        return {}

    def create(self, request: ProvisionRequest) -> ProvisionResult:
        from types import SimpleNamespace

        # The site's platform_config, shaped like the old AzureConfig so
        # the SDK-call body below stays byte-identical.
        az = SimpleNamespace(
            subscription_id=str(self.platform_config["subscription_id"]),
            resource_group=str(self.platform_config["resource_group"]),
            region=str(self.platform_config["region"]),
        )

        azure_vm_size = request.azure_vm_size or "Standard_B2s"
        disk = request.disk_gib if request.disk_gib is not None else 50
        swap = request.swap_gib if request.swap_gib is not None else 0
        admin_username = request.admin_username
        tailscale_auth_key = request.tailscale_auth_key
        ssh_pub_key = request.ssh_public_key

        # Platform-owned naming with the slug as the
        # namespacing token; azure resource names are the primary
        # identifier, so a collision is an error.
        vm_name = (
            f"{request.system_slug}-{request.vm_name}"
            if request.system_slug
            else request.vm_name
        )

        output.info("Connecting to Azure...")
        compute = _compute_client(az)
        network = _network_client(az)

        if self._vm_exists(compute, az.resource_group, vm_name):
            raise StateError(
                f"an Azure VM named '{vm_name}' already exists in resource "
                f"group '{az.resource_group}'",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint="delete it first or pick a different VM name",
            )

        output.info(f"Provisioning Azure VM '{vm_name}' in {az.region} (size: {azure_vm_size})...")
        if swap > 0:
            output.detail(f"Swap: {swap} GiB")

        # Generate the same bootstrap script used by Lima, wrapped in
        # cloud-init write_files + runcmd for delivery via Azure custom_data.
        if tailscale_auth_key:
            bootstrap = generate_bootstrap_script(
                admin_username=admin_username,
                ssh_public_key=ssh_pub_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=tailscale_auth_key,
                hostname=request.hostname,
                swap=swap,
            )
            cloud_init = generate_cloud_init(bootstrap)
        else:
            # No Tailscale key: minimal cloud-init, bootstrap deferred to Phase A
            cloud_init = "#cloud-config\npackage_update: true\npackages:\n  - openssh-server\n"
        cloud_init_b64 = base64.b64encode(cloud_init.encode()).decode()

        try:
            # Create public IP
            output.detail("Creating public IP...")
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
            output.detail("Creating network security group...")
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
            output.detail("Creating network interface...")

            # Need a subnet: use default VNet or create one
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
            output.detail("Creating VM...")
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
                            "disk_size_gb": disk,
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
            output.detail("Cleaning up resources...")
            _cleanup_vm_resources(compute, network, az.resource_group, vm_name)
            raise _wrap_azure_error(exc) from exc

        output.detail(f"Azure VM '{vm_name}' provisioned (IP: {public_ip}).")

        import sys

        prov_transport = SSHTransport(
            host=public_ip,
            user=admin_username,
            identity_file=request.ssh_private_key,
            force_tty=sys.platform == "win32",
        )

        # If bootstrap was embedded in cloud-init, wait for it to finish
        # and extract the Tailscale IP.
        tailscale_ip = None
        bootstrap_complete = False
        if tailscale_auth_key:
            tailscale_ip = self._wait_for_bootstrap(prov_transport, vm_name)
            if tailscale_ip:
                bootstrap_complete = True

        metadata = {"resource_id": resource_id} if resource_id else {}
        return ProvisionResult(
            native_transport=prov_transport,
            platform_metadata=metadata,
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    @staticmethod
    def _vm_exists(
        compute: ComputeManagementClient, resource_group: str, vm_name: str
    ) -> bool:
        """Pre-flight: does a VM with this name exist in the group?"""
        try:
            compute.virtual_machines.get(resource_group, vm_name)
        except Exception:
            return False
        return True

    def _wait_for_bootstrap(self, target: Transport, vm_name: str) -> str | None:
        """Wait for cloud-init to finish and return the Tailscale IP.

        SSH may not be immediately available after VM creation, so we retry.
        Returns None if we cannot get the IP (Phase A will handle it).
        """
        import time

        output.detail("Waiting for cloud-init bootstrap to complete (this may take several minutes)...")

        for attempt in range(30):
            try:
                target.run("echo ok", check=True, timeout=10)
                break
            except SSHError:
                if attempt == 29:
                    output.warn("SSH not available, deferring bootstrap to Phase A")
                    return None
                time.sleep(10)

        try:
            target.run("cloud-init status --wait", check=True, timeout=600)
        except SSHError as e:
            output.warn(f"cloud-init wait failed: {e}")
            output.warn("Deferring bootstrap to Phase A")
            return None

        try:
            result = target.run("sudo tailscale ip -4", check=True, timeout=15)
            tailscale_ip = result.stdout.strip()
            output.detail(f"Tailscale IP: {tailscale_ip}")
            return tailscale_ip
        except SSHError as e:
            output.warn(f"could not retrieve Tailscale IP: {e}")
            return None

    def start(self, vm: VMRow) -> None:
        # Idempotent by construction (the ABC flags start): the Azure
        # begin_start operation no-ops on an already-running VM.
        output.info(f"Starting Azure VM '{vm.name}'...")
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = _compute_client(az_cfg)
            compute.virtual_machines.begin_start(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        # Idempotent by construction (the ABC flags stop): the Azure
        # begin_deallocate operation no-ops on a deallocated VM.
        output.info(f"Deallocating Azure VM '{vm.name}'...")
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = _compute_client(az_cfg)
            compute.virtual_machines.begin_deallocate(rg, name).result()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        output.info(f"Azure VM '{vm.name}' deallocated")

    def delete(self, vm: VMRow) -> None:
        output.info(f"Deleting Azure VM '{vm.name}'...")
        if not vm.platform_metadata.get("resource_id"):
            output.warn("no Azure resource ID, skipping Azure cleanup")
            return

        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        compute = _compute_client(az_cfg)
        network = _network_client(az_cfg)

        # Delete VM first (must complete before dependent resources)
        with contextlib.suppress(Exception):
            compute.virtual_machines.begin_delete(rg, name).result()

        _cleanup_vm_resources(compute, network, rg, name)

        output.info(f"Azure VM '{vm.name}' deleted")

    def attach_public_ip(self, vm: VMRow) -> str:
        """Attach a temporary public IP to the VM's NIC. Returns the IP address."""
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        network = _network_client(az_cfg)

        try:
            # Create (or re-create) the public IP
            output.detail("Attaching temporary public IP...")
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
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        network = _network_client(az_cfg)

        output.detail("Removing public IP...")
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

    def display_backend_name(self, vm: VMRow) -> str:
        resource_id = vm.platform_metadata.get("resource_id")
        if not resource_id:
            return vm.name
        _rg, name, _cfg = _parse_resource_id(resource_id)
        return name

    def native_transport(
        self, vm: VMRow, *, config: Config | None = None,
    ) -> Transport | None:
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
        try:
            compute = _compute_client(az_cfg)
            vm_info = compute.virtual_machines.get(
                rg,
                name,
                expand="instanceView",
            )
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

        # Walk NICs to find the public IP (may not exist if detached). An
        # empty string here propagates to ``SSHTransport(host="")`` which
        # the transports.native_transport factory catches with a typed
        # StateError; on the canonical path this method is reached only
        # inside the transient_route context manager which guarantees a
        # public IP is attached, so the empty case is a defensive guard.
        public_ip = _get_vm_public_ip(vm_info, az_cfg)
        import sys

        # Include identity file if config is available (needed for SSH auth
        # via public IP, e.g., during Tailscale logout on delete).
        identity_file = None
        if config is not None:
            identity_file = getattr(getattr(config, "operator", None), "ssh_private_key", None)

        return SSHTransport(
            host=public_ip,
            user=vm.admin_username,
            identity_file=identity_file,
            force_tty=sys.platform == "win32",
        )

    def post_tailscale_ready(self, vm: VMRow) -> None:
        """Detach the cloud-init public IP now that Tailscale is up.

        The attach happens inside :meth:`create` (Azure needs the IP to
        drive cloud-init bootstrap); this hook fires at the async
        Tailscale-ready point inside ``initialize_vm`` to close the
        public-exposure window.
        """
        self.detach_public_ip(vm)

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow) -> Iterator[None]:
        """Attach a transient public IP for the duration of the context.

        The native transport for Azure reaches the VM via a temporary
        public IP. Attach on enter, detach on exit (regardless of how
        the caller unwinds). The
        :func:`agentworks.transports.native_transport` factory wraps
        this around the per-platform :meth:`native_transport` call so
        the lifecycle stays polymorphic.
        """
        self.attach_public_ip(vm)
        try:
            yield
        finally:
            self.detach_public_ip(vm)

    def status(self, vm: VMRow) -> VMStatus:
        if not vm.platform_metadata.get("resource_id"):
            return VMStatus.UNKNOWN
        rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
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


def _resource_id(vm: VMRow) -> str:
    """The VM's Azure resource ID from platform metadata, or a typed error."""
    resource_id = vm.platform_metadata.get("resource_id")
    if not resource_id:
        raise StateError(
            f"VM '{vm.name}' has no azure resource_id in its platform "
            f"metadata; the DB row is incomplete",
            entity_kind="vm",
            entity_name=vm.name,
        )
    return str(resource_id)


def _get_vm_location(vm: VMRow) -> str:
    """Get the Azure region for a VM by querying the compute API."""
    rg, name, az_cfg = _parse_resource_id(_resource_id(vm))
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
