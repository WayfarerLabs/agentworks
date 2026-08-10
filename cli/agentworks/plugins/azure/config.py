"""Azure VM site config, size catalog, and image constants."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple

from pydantic import Field

from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonEmptyStr, PositiveInt, SecretRef


class AzureAmbientAuth(AgwModel):
    """Authenticate with the ambient chain and browser fallback."""

    mode: Literal["ambient"]


class AzureServicePrincipalAuth(AgwModel):
    """Authenticate as an explicit Entra ID service principal."""

    mode: Literal["service-principal"]
    tenant_id: NonEmptyStr
    client_id: NonEmptyStr
    secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Azure service-principal client secret", default_template="azure-client-secret"),
    ]


AzureAuth = Annotated[AzureAmbientAuth | AzureServicePrincipalAuth, Field(discriminator="mode")]


class AzureVMSize(AgwModel):
    """One Azure VM size in a site's selection catalog."""

    cpus: PositiveInt
    memory: PositiveInt
    size: NonEmptyStr


class AzureVMConfig(AgwModel):
    """Where an azure-vm site creates VMs, and as whom."""

    name: Literal["azure-vm"]
    subscription_id: NonEmptyStr = Field(examples=["00000000-0000-0000-0000-000000000000"])
    resource_group: NonEmptyStr = Field(examples=["agw-dev"])
    region: NonEmptyStr = Field(examples=["eastus"])
    vm_sizes: Annotated[list[AzureVMSize], Field(min_length=1)] | None = None
    auth: AzureAuth = AzureAmbientAuth(mode="ambient")


class _VMSize(NamedTuple):
    """One selectable Azure VM size and its capacity."""

    cpus: int
    memory_gib: int
    name: str


_DEFAULT_VM_SIZES: tuple[_VMSize, ...] = (
    _VMSize(1, 2, "Standard_B1ms"),
    _VMSize(2, 4, "Standard_B2s"),
    _VMSize(2, 8, "Standard_B2ms"),
    _VMSize(4, 16, "Standard_B4ms"),
    _VMSize(8, 32, "Standard_B8ms"),
    _VMSize(12, 48, "Standard_B12ms"),
    _VMSize(16, 64, "Standard_B16ms"),
    _VMSize(20, 80, "Standard_B20ms"),
)


def _size_catalog(config: AzureVMConfig) -> tuple[_VMSize, ...]:
    """Return the site's declared catalog or the built-in B-series ladder."""
    if config.vm_sizes is None:
        return _DEFAULT_VM_SIZES
    return tuple(_VMSize(entry.cpus, entry.memory, entry.size) for entry in config.vm_sizes)


def _select_vm_size(catalog: tuple[_VMSize, ...], *, cpus: int, memory_gib: int) -> _VMSize:
    """Return the smallest catalog entry satisfying both requested axes."""
    fits = [size for size in catalog if size.cpus >= cpus and size.memory_gib >= memory_gib]
    if not fits:
        largest = max(catalog, key=lambda size: (size.cpus, size.memory_gib))
        raise ConfigError(
            f"no Azure VM size satisfies the requested {cpus} vCPU / "
            f"{memory_gib} GiB (largest available is {largest.name}: "
            f"{largest.cpus} vCPU / {largest.memory_gib} GiB)",
            hint="shrink the vm-template's cpus/memory, or add a larger "
            "entry to the site's vm_sizes catalog (vm-site platform config)",
        )
    return min(fits, key=lambda size: (size.cpus, size.memory_gib))


IMAGE_PUBLISHER = "Debian"
IMAGE_OFFER = "debian-12"
IMAGE_SKU = "12-gen2"
IMAGE_VERSION = "latest"

# Azure rejects an OS disk smaller than the marketplace image's own disk.
IMAGE_OS_DISK_FLOOR_GIB = 30
