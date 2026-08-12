"""Read-only Compute Engine project, zone, machine, image, and instance helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.errors import AlreadyExistsError, ConfigError
from agentworks.plugins.gcp.config import IMAGE_PROJECT, MachineTypeSelection, image_family_for_arch
from agentworks.plugins.gcp.errors import GCEError, GCEOperationError, call_google_optional

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.plugins.gcp.auth import GcpClientCache
    from agentworks.plugins.gcp.config import GcpGCEConfig

EXTERNAL_ACCESS_CONFIG_NAME = "External NAT"


def provider_resource_id(value: object) -> str | None:
    """Normalize one positive uint64 provider ID without accepting zero."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        normalized = int(value)
    except ValueError:
        return None
    return str(normalized) if normalized > 0 else None


def canonical_resource_url(value: str) -> str:
    """Normalize absolute and project-relative Compute resource URLs."""
    marker = "/projects/"
    if marker in value:
        return value[value.index(marker) + 1 :].rstrip("/")
    return value.lstrip("/").rstrip("/")


def verify_zonal_operation(
    operation: Any,
    *,
    request_id: str,
    operation_type: str,
    project_id: str,
    zone: str,
    instance_name: str,
    expected_resource_id: str | None,
) -> str:
    """Verify request, operation, target, and provider incarnation identity."""
    expected_link = f"projects/{project_id}/zones/{zone}/instances/{instance_name}"
    target_id = provider_resource_id(getattr(operation, "target_id", None))
    if (
        str(getattr(operation, "client_operation_id", "")) != request_id
        or str(getattr(operation, "operation_type", "")).lower() != operation_type
        or canonical_resource_url(str(getattr(operation, "target_link", ""))) != expected_link
        or target_id is None
        or (expected_resource_id is not None and target_id != expected_resource_id)
    ):
        raise GCEOperationError(
            f"Google Cloud returned incomplete ownership identity for instance {project_id}/{zone}/{instance_name}",
            entity_kind="gcp-instance",
            entity_name=instance_name,
            hint="retain the named instance until its provider identity can be established",
        )
    return target_id


def get_project(clients: GcpClientCache, ctx: RunContext, project_id: str) -> Any:
    """Return an addressable target project or fail before mutation."""
    client = clients.client("projects", ctx)
    project = call_google_optional(
        lambda: client.get(project=project_id),
        operation="reading the target project",
        resource=f"project {project_id}",
    )
    if project is None:
        raise ConfigError(
            f"Google Cloud project '{project_id}' does not exist or is not addressable",
            hint="check project_id and the selected identity's Compute Engine access",
        )
    return project


def get_zone(clients: GcpClientCache, ctx: RunContext, project_id: str, zone: str) -> Any:
    """Return an addressable Compute Engine zone or fail before mutation."""
    client = clients.client("zones", ctx)
    result = call_google_optional(
        lambda: client.get(project=project_id, zone=zone),
        operation="reading the target zone",
        resource=f"zone {project_id}/{zone}",
    )
    if result is None:
        raise ConfigError(
            f"Compute Engine zone '{zone}' is not available in project '{project_id}'",
            hint="check the vm-site zone and that the Compute Engine API is enabled",
        )
    return result


def verify_live_machine_type(
    clients: GcpClientCache,
    ctx: RunContext,
    config: GcpGCEConfig,
    selected: MachineTypeSelection,
) -> Any:
    """Verify provider shape and known CPU-only Persistent Disk compatibility."""
    client = clients.client("machine-types", ctx)
    machine = call_google_optional(
        lambda: client.get(
            project=config.project_id,
            zone=config.zone,
            machine_type=selected.type,
        ),
        operation="reading the selected machine type",
        resource=f"machine type {config.project_id}/{config.zone}/{selected.type}",
    )
    if machine is None:
        raise ConfigError(
            f"GCE machine type '{selected.type}' is not available in zone '{config.zone}'",
            hint="select a machine type that exists in the vm-site zone",
        )

    live_cpus = _required_positive_machine_field(
        machine,
        field_name="guest_cpus",
        field_label="guest CPU count",
        machine_type=selected.type,
    )
    live_memory_mib = _required_positive_machine_field(
        machine,
        field_name="memory_mb",
        field_label="memory size",
        machine_type=selected.type,
    )
    live_arch = str(machine.architecture).lower()
    architecture_mismatch = bool(live_arch) and live_arch != selected.arch
    if live_cpus != selected.cpus or live_memory_mib != selected.memory_gib * 1024 or architecture_mismatch:
        raise ConfigError(
            f"GCE machine type '{selected.type}' does not match its machine_types declaration: "
            f"provider reports {live_cpus} vCPU / {live_memory_mib} MiB / {live_arch or 'unknown architecture'}, "
            f"site declares {selected.cpus} vCPU / {selected.memory_gib * 1024} MiB / {selected.arch}",
            hint="correct the machine_types entry rather than provisioning a different live shape",
        )
    maximum_disks_is_present = _proto_field_is_present(machine, "maximum_persistent_disks")
    if (maximum_disks_is_present and int(machine.maximum_persistent_disks) == 0) or bool(machine.accelerators):
        raise ConfigError(
            f"GCE machine type '{selected.type}' is incompatible with this platform's storage or accelerator contract",
            hint=(
                f"select a machine_types entry where '{selected.type}' supports Persistent Disk and requires no "
                "guest accelerator; gcp-gce currently provisions a CPU-only Debian 12 VM with a "
                "'pd-balanced' boot disk"
            ),
        )
    return machine


def _proto_field_is_present(message: Any, field_name: str) -> bool:
    """Check scalar presence, treating an unrecognized SDK shape as absent.

    Proto-plus normally exposes its underlying protobuf through ``pb``. Known
    representation errors from SDK shape drift fall back to ``False`` instead
    of consuming a protobuf scalar default. Required fields then fail as an
    unknown provider shape, while optional output fields remain unknown.
    Unexpected exceptions propagate instead of being mistaken for absence.
    """
    try:
        proto_plus_converter = getattr(type(message), "pb", None)
        protobuf = proto_plus_converter(message) if callable(proto_plus_converter) else message
    except (AttributeError, TypeError, ValueError):
        return False
    has_field = getattr(protobuf, "HasField", None)
    if not callable(has_field):
        return False
    try:
        return bool(has_field(field_name))
    except (TypeError, ValueError):
        return False


def _required_positive_machine_field(
    machine: Any,
    *,
    field_name: str,
    field_label: str,
    machine_type: str,
) -> int:
    """Read one presence-tracked required positive machine-shape scalar."""
    if not _proto_field_is_present(machine, field_name):
        raise ConfigError(
            f"GCE machine type '{machine_type}' has an unknown provider shape: "
            f"required {field_label} field '{field_name}' is absent",
            hint=(
                "select a supported machine type or retry after checking the Google Compute SDK; "
                "gcp-gce requires present positive guest CPU and memory fields before provisioning"
            ),
        )
    try:
        value = int(getattr(machine, field_name))
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise ConfigError(
            f"GCE machine type '{machine_type}' has an invalid provider shape: "
            f"required {field_label} field '{field_name}' is not positive",
            hint="select a machine type whose live guest CPU and memory fields are both positive",
        )
    return value


def resolve_debian_image(
    clients: GcpClientCache,
    ctx: RunContext,
    arch: str,
) -> Any:
    """Resolve and architecture-check the public Debian 12 image family."""
    if arch not in {"x86_64", "arm64"}:
        raise ConfigError(f"unsupported GCE image architecture '{arch}'")
    family = image_family_for_arch(arch)  # type: ignore[arg-type]
    client = clients.client("images", ctx)
    image = call_google_optional(
        lambda: client.get_from_family(project=IMAGE_PROJECT, family=family),
        operation="resolving the Debian image family",
        resource=f"image family {IMAGE_PROJECT}/{family}",
    )
    if image is None:
        raise ConfigError(
            f"Debian image family '{IMAGE_PROJECT}/{family}' was not found",
            hint="retry after verifying the public debian-cloud image project is reachable",
        )
    live_arch = str(image.architecture).lower()
    if live_arch != arch:
        raise ConfigError(
            f"Debian image family '{IMAGE_PROJECT}/{family}' reports architecture '{live_arch}', expected '{arch}'"
        )
    if not str(image.self_link):
        raise GCEError(f"Debian image family '{IMAGE_PROJECT}/{family}' returned no self link")
    return image


def resolve_balanced_disk_type(
    clients: GcpClientCache,
    ctx: RunContext,
    config: GcpGCEConfig,
) -> Any:
    """Resolve the zonal balanced persistent-disk type before mutation."""
    client = clients.client("disk-types", ctx)
    disk_type = call_google_optional(
        lambda: client.get(
            project=config.project_id,
            zone=config.zone,
            disk_type="pd-balanced",
        ),
        operation="resolving the balanced persistent-disk type",
        resource=f"disk type {config.project_id}/{config.zone}/pd-balanced",
    )
    if disk_type is None:
        raise ConfigError(
            f"GCE disk type 'pd-balanced' is not available in zone '{config.zone}'",
            hint="select a zone that supports balanced persistent disks",
        )
    if not str(disk_type.self_link):
        raise GCEError(f"GCE disk type '{config.project_id}/{config.zone}/pd-balanced' returned no self link")
    return disk_type


def require_instance_name_available(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
) -> None:
    """Fail before mutation when the exact normalized instance exists."""
    client = clients.client("instances", ctx)
    existing = call_google_optional(
        lambda: client.get(project=project_id, zone=zone, instance=instance_name),
        operation="checking the instance name",
        resource=f"instance {project_id}/{zone}/{instance_name}",
    )
    if existing is not None:
        raise AlreadyExistsError(
            f"GCE instance '{instance_name}' already exists in project '{project_id}', zone '{zone}'",
            entity_kind="gcp-instance",
            entity_name=instance_name,
            hint=(
                "choose another Agentworks VM name; otherwise inspect the existing provider identity and "
                "escalate ownership. Do not delete the instance by name without independent ownership proof"
            ),
        )


def live_external_ipv4(instance: Any, *, access_config_name: str = EXTERNAL_ACCESS_CONFIG_NAME) -> str:
    """Read the named ephemeral external IPv4 from a live instance object."""
    for interface in instance.network_interfaces:
        for access in interface.access_configs:
            if access.name == access_config_name and access.type_ == "ONE_TO_ONE_NAT":
                address = str(access.nat_i_p)
                if address:
                    return address
    raise GCEError(
        f"GCE instance has no live '{access_config_name}' external IPv4 access config",
        hint="start the instance and inspect its network interface before retrying",
    )
