"""GCP GCE site config, machine catalog, and image-family selection."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple

from pydantic import AfterValidator, Field

from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonEmptyStr, PositiveInt, SecretRef


def _reject_whitespace_only(value: str) -> str:
    """Keep provider identifiers literal while rejecting blank-looking input."""
    if not value.strip():
        raise ValueError("must contain a non-whitespace character")
    return value


GcpNonBlankStr = Annotated[NonEmptyStr, AfterValidator(_reject_whitespace_only)]


class GcpAmbientAuth(AgwModel):
    """Authenticate through Google Application Default Credentials."""

    mode: Literal["ambient"]


class GcpServiceAccountAuth(AgwModel):
    """Authenticate as the service account in one complete JSON secret."""

    mode: Literal["service-account"]

    secret: Annotated[
        GcpNonBlankStr,
        SecretRef(
            usage="the complete Google service-account JSON document",
            default_template="gcp-service-account-key",
        ),
    ]
    """The framework secret containing the whole service-account document."""


GcpAuth = Annotated[GcpAmbientAuth | GcpServiceAccountAuth, Field(discriminator="mode")]


class GcpMachineType(AgwModel):
    """One entry in a GCE machine-type selection catalog."""

    cpus: PositiveInt
    """The vCPUs the type provides."""

    memory: PositiveInt
    """The memory in GiB the type provides."""

    type: GcpNonBlankStr
    """The literal Compute Engine machine type."""

    arch: Literal["x86_64", "arm64"]
    """The guest CPU architecture."""


class GcpGCEConfig(AgwModel):
    """Where a gcp-gce site creates instances, and as whom."""

    name: Literal["gcp-gce"]
    """The platform this config is for."""

    project_id: GcpNonBlankStr = Field(examples=["agentworks-dev"])
    """The target Google Cloud project."""

    zone: GcpNonBlankStr = Field(examples=["us-central1-a"])
    """The Compute Engine zone for new instances."""

    subnet: GcpNonBlankStr | None = Field(default=None, examples=["app-subnet"])
    """A subnetwork in the zone's region. Omit for the default network."""

    machine_types: Annotated[list[GcpMachineType], Field(min_length=1)] | None = None
    """An optional non-empty override of the built-in E2 catalog."""

    auth: GcpAuth = GcpAmbientAuth(mode="ambient")
    """The site identity. Omission selects ambient credentials."""


class MachineTypeSelection(NamedTuple):
    """One normalized catalog entry used by provider helpers."""

    cpus: int
    memory_gib: int
    type: str
    arch: Literal["x86_64", "arm64"]


DEFAULT_MACHINE_TYPES: tuple[MachineTypeSelection, ...] = (
    MachineTypeSelection(2, 8, "e2-standard-2", "x86_64"),
    MachineTypeSelection(4, 16, "e2-standard-4", "x86_64"),
    MachineTypeSelection(8, 32, "e2-standard-8", "x86_64"),
    MachineTypeSelection(16, 64, "e2-standard-16", "x86_64"),
    MachineTypeSelection(32, 128, "e2-standard-32", "x86_64"),
)

IMAGE_PROJECT = "debian-cloud"
IMAGE_FAMILIES: dict[Literal["x86_64", "arm64"], str] = {
    "x86_64": "debian-12",
    "arm64": "debian-12-arm64",
}


def machine_catalog(config: GcpGCEConfig) -> tuple[MachineTypeSelection, ...]:
    """Return the declared catalog, or the immutable built-in E2 ladder."""
    if config.machine_types is None:
        return DEFAULT_MACHINE_TYPES
    return tuple(MachineTypeSelection(e.cpus, e.memory, e.type, e.arch) for e in config.machine_types)


def select_machine_type(
    catalog: tuple[MachineTypeSelection, ...],
    *,
    cpus: int,
    memory_gib: int,
) -> MachineTypeSelection:
    """Select the smallest deterministic entry satisfying both axes."""
    fits = [entry for entry in catalog if entry.cpus >= cpus and entry.memory_gib >= memory_gib]
    if not fits:
        largest = max(catalog, key=lambda entry: (entry.cpus, entry.memory_gib, entry.type, entry.arch))
        raise ConfigError(
            f"no GCE machine type satisfies the requested {cpus} vCPU / {memory_gib} GiB "
            f"(largest available is {largest.type}: {largest.cpus} vCPU / {largest.memory_gib} GiB)",
            hint=("shrink the vm-template's cpus/memory, or add a larger entry to the site's machine_types catalog"),
        )
    return min(fits, key=lambda entry: (entry.cpus, entry.memory_gib, entry.type, entry.arch))


def image_family_for_arch(arch: Literal["x86_64", "arm64"]) -> str:
    """Return the public Debian 12 image family for one catalog architecture."""
    return IMAGE_FAMILIES[arch]
