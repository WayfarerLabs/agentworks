"""Azure VM site config, size catalog, and image constants."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple

from pydantic import Field

from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonBlankStr, NonEmptyStr, PositiveInt, SecretRef


class AzureAmbientAuth(AgwModel):
    """Authenticate with the ambient chain: ``az login``, ``AZURE_*``, or a managed identity.

    The interactive-browser fallback runs when none of those can get a
    token.
    """

    mode: Literal["ambient"]


class AzureServicePrincipalAuth(AgwModel):
    """Authenticate as an explicit Entra ID service principal.

    It replaces the ambient chain entirely for this site: a rejected
    client secret fails the command rather than falling back to some other
    identity.
    """

    mode: Literal["service-principal"]
    tenant_id: NonBlankStr
    """The Entra ID tenant the principal lives in."""

    client_id: NonBlankStr
    """The principal's application (client) id."""

    secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the Azure service-principal client secret", default_template="azure-client-secret"),
    ]
    """The secret containing the principal's client secret. The default
    maps to ``AW_SECRET_AZURE_CLIENT_SECRET`` in the env-var backend."""


AzureAuth = Annotated[AzureAmbientAuth | AzureServicePrincipalAuth, Field(discriminator="mode")]


class AzureVMSize(AgwModel):
    """One Azure VM size in a site's selection catalog."""

    cpus: PositiveInt
    """The vCPUs the SKU provides."""

    memory: PositiveInt
    """The memory (GiB) the SKU provides."""

    size: NonBlankStr
    """The Azure SKU name (e.g. ``Standard_B2ms``)."""


class AzureVMConfig(AgwModel):
    """Where an azure-vm site creates VMs, and as whom."""

    name: Literal["azure-vm"]
    """The platform this config is for."""

    subscription_id: NonBlankStr = Field(examples=["00000000-0000-0000-0000-000000000000"])
    """The subscription new VMs are created in."""

    resource_group: NonBlankStr = Field(examples=["agw-dev"])
    """The resource group new VMs are created in."""

    region: NonBlankStr = Field(examples=["eastus"])
    """The Azure region new VMs are created in."""

    vm_sizes: Annotated[list[AzureVMSize], Field(min_length=1)] | None = None
    """An override of the built-in B-series catalog ``vm create`` picks
    from. Order does not matter; selection uses the smallest matching
    entry. Must contain at least one entry."""

    auth: AzureAuth = AzureAmbientAuth(mode="ambient")
    """How this site authenticates to Azure: ``{mode: ambient}`` for the
    ambient credential chain, or ``{mode: service-principal, ...}`` for an
    explicit principal. Defaults to ambient."""


class _VMSize(NamedTuple):
    """One Azure VM size in the selection catalog: a SKU name plus the
    cpus and memory (GiB) it provides."""

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
    """The site's VM-size catalog: its declared override, else the built-in
    B-series ladder. The default ladder is domain knowledge rather than
    schema."""
    if config.vm_sizes is None:
        return _DEFAULT_VM_SIZES
    return tuple(_VMSize(entry.cpus, entry.memory, entry.size) for entry in config.vm_sizes)


def _select_vm_size(catalog: tuple[_VMSize, ...], *, cpus: int, memory_gib: int) -> _VMSize:
    """Return the smallest catalog entry satisfying both requested axes.

    Selection uses ``min`` so the result is independent of catalog order.
    Raises ``ConfigError`` when the request exceeds every entry.
    """
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


class AzureImage(NamedTuple):
    """One complete Azure Marketplace image selector and its disk floor."""

    publisher: str
    offer: str
    sku: str
    version: str
    os_disk_floor_gib: int


AZURE_IMAGES: dict[DebianRelease, AzureImage] = {
    DebianRelease.TRIXIE: AzureImage(
        publisher="Debian",
        offer="debian-13",
        sku="13-gen2",
        version="latest",
        os_disk_floor_gib=30,
    ),
}
