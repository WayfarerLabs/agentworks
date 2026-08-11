"""Typed GCE instance requests and provider-incarnation reconciliation."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    AuthorizationError,
    ConnectivityError,
    NotFoundError,
    TokenRejectedError,
)
from agentworks.plugins.gcp.cleanup import InstanceOwnership
from agentworks.plugins.gcp.compute import (
    EXTERNAL_ACCESS_CONFIG_NAME,
    canonical_resource_url,
    provider_resource_id,
    verify_zonal_operation,
)
from agentworks.plugins.gcp.errors import (
    GCECapacityError,
    GCEError,
    GCEIndeterminateOperationError,
    GCEOperationError,
    GCEQuotaError,
    call_google,
    call_google_optional,
    wait_for_extended_operation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.plugins.gcp.network import NetworkSelection


@dataclass
class InstanceInsertAttempt:
    """One request identity plus ownership learned before an interruptible wait."""

    instance_name: str
    request_id: str
    ownership: InstanceOwnership | None = None
    submitted: bool = False

    @classmethod
    def create(cls, instance_name: str) -> InstanceInsertAttempt:
        return cls(instance_name=instance_name, request_id=str(uuid.uuid4()))


def build_instance_resource(
    *,
    instance_name: str,
    machine_type_url: str,
    image_url: str,
    disk_type_url: str,
    disk_gib: int,
    network: NetworkSelection,
    network_tag: str,
    admin_username: str,
    ssh_public_key: str,
    startup_script: str,
) -> Any:
    """Build the complete retained instance body without any credential."""
    from google.cloud import compute_v1

    interface_kwargs: dict[str, Any] = {
        "stack_type": "IPV4_ONLY",
        "access_configs": [
            compute_v1.AccessConfig(
                name=EXTERNAL_ACCESS_CONFIG_NAME,
                type_="ONE_TO_ONE_NAT",
                network_tier="PREMIUM",
            )
        ],
    }
    if network.subnet_url is None:
        interface_kwargs["network"] = network.network_url
    else:
        interface_kwargs["subnetwork"] = network.subnet_url

    return compute_v1.Instance(
        name=instance_name,
        machine_type=machine_type_url,
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                type_="PERSISTENT",
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=image_url,
                    disk_size_gb=disk_gib,
                    disk_type=disk_type_url,
                ),
            )
        ],
        network_interfaces=[compute_v1.NetworkInterface(**interface_kwargs)],
        tags=compute_v1.Tags(items=[network_tag]),
        metadata=compute_v1.Metadata(
            items=[
                compute_v1.Items(key="ssh-keys", value=f"{admin_username}:{ssh_public_key}"),
                compute_v1.Items(key="block-project-ssh-keys", value="TRUE"),
                compute_v1.Items(key="enable-oslogin", value="FALSE"),
                compute_v1.Items(key="startup-script", value=startup_script),
            ]
        ),
        # Do not let the guest inherit the project's default service account
        # or any OAuth scope. The control-plane credential remains host-side.
        service_accounts=[],
    )


def insert_instance_reconciled(
    client: Any,
    *,
    project_id: str,
    zone: str,
    instance: Any,
    selected_machine_type: str,
    attempt: InstanceInsertAttempt,
    timeout: float,
) -> tuple[Any, InstanceOwnership]:
    """Insert once and prove ownership through request, operation, and live IDs."""
    from google.cloud import compute_v1

    if attempt.instance_name != str(instance.name) or attempt.submitted:
        raise ValueError("instance insert attempt does not match this fresh mutation")
    request = compute_v1.InsertInstanceRequest(
        project=project_id,
        zone=zone,
        instance_resource=instance,
        request_id=attempt.request_id,
    )
    attempt.submitted = True

    operation: Any | None = None
    initial_failure: AgentworksError | None = None
    try:
        operation = call_google(
            lambda: client.insert(request=request, retry=None),
            operation="inserting an owned instance",
            resource=f"instance {project_id}/{zone}/{instance.name}",
        )
    except (AlreadyExistsError, AuthorizationError, NotFoundError, TokenRejectedError, GCEQuotaError):
        raise
    except (ConnectivityError, GCEError) as exc:
        initial_failure = exc

    if operation is None:
        retry_failed = False
        try:
            operation = call_google(
                lambda: client.insert(request=request, retry=None),
                operation="reconciling an indeterminate instance insert",
                resource=f"instance {project_id}/{zone}/{instance.name}",
            )
        except (AlreadyExistsError, AuthorizationError, NotFoundError, TokenRejectedError, GCEQuotaError):
            raise
        except (ConnectivityError, GCEError):
            retry_failed = True
        if retry_failed:
            if initial_failure is None:  # pragma: no cover - guarded by operation being absent
                raise AssertionError("missing initial instance insert failure")
            raise initial_failure

    target_id = verify_zonal_operation(
        operation,
        request_id=attempt.request_id,
        operation_type="insert",
        project_id=project_id,
        zone=zone,
        instance_name=attempt.instance_name,
        expected_resource_id=None,
    )
    attempt.ownership = InstanceOwnership(resource_id=target_id)

    wait_failure: AgentworksError | None = None
    definitive_failure: GCEOperationError | None = None
    try:
        wait_for_extended_operation(operation, label=f"instance {instance.name}", zone=zone, timeout=timeout)
    except GCECapacityError:
        raise
    except GCEIndeterminateOperationError as exc:
        wait_failure = exc
    except GCEOperationError:
        definitive_failure = GCEOperationError(
            f"Google Cloud rejected instance '{instance.name}' while inserting selected machine type "
            f"'{selected_machine_type}'",
            entity_kind="gcp-instance",
            entity_name=str(instance.name),
            hint=(
                f"verify that machine type '{selected_machine_type}' supports a CPU-only Debian 12 VM with a "
                "'pd-balanced' boot disk, or choose a compatible machine_types entry"
            ),
        )

    if definitive_failure is not None:
        raise definitive_failure

    realized = call_google_optional(
        lambda: client.get(project=project_id, zone=zone, instance=instance.name),
        operation="reconciling an inserted instance",
        resource=f"instance {project_id}/{zone}/{instance.name}",
    )
    if realized is not None:
        observed_id = provider_resource_id(realized.id)
        if observed_id == target_id:
            # A matching realized incarnation reconciles an indeterminate wait.
            return realized, attempt.ownership
        raise AlreadyExistsError(
            f"GCE instance '{instance.name}' has a different provider identity after insert",
            entity_kind="gcp-instance",
            entity_name=str(instance.name),
            hint="retain the colliding instance because this insert attempt does not own it",
        )
    if wait_failure is not None:
        raise wait_failure
    raise GCEOperationError(
        f"GCE instance '{instance.name}' was absent after its insert operation completed",
        entity_kind="gcp-instance",
        entity_name=str(instance.name),
    )


def read_owned_instance(
    client: Any,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    resource_id: str,
) -> Any | None:
    """Read an instance only when the persisted provider incarnation matches."""
    instance = call_google_optional(
        lambda: client.get(project=project_id, zone=zone, instance=instance_name),
        operation="reading an owned instance",
        resource=f"instance {project_id}/{zone}/{instance_name}",
    )
    if instance is None:
        return None
    if provider_resource_id(instance.id) != resource_id:
        raise AlreadyExistsError(
            f"GCE instance '{instance_name}' has a different provider identity",
            entity_kind="gcp-instance",
            entity_name=instance_name,
            hint="do not mutate or delete the same-name instance; escalate ownership",
        )
    return instance


def wait_for_instance_status(
    client: Any,
    *,
    project_id: str,
    zone: str,
    instance_name: str,
    resource_id: str,
    accepted: Sequence[str],
    timeout: float,
    poll_interval: float = 2.0,
) -> Any:
    """Poll one owned incarnation to a bounded provider state."""
    deadline = time.monotonic() + timeout
    wanted = {state.upper() for state in accepted}
    while True:
        instance = read_owned_instance(
            client,
            project_id=project_id,
            zone=zone,
            instance_name=instance_name,
            resource_id=resource_id,
        )
        if instance is None:
            raise NotFoundError(
                f"GCE instance '{instance_name}' disappeared while waiting for {', '.join(sorted(wanted))}",
                entity_kind="gcp-instance",
                entity_name=instance_name,
            )
        if str(instance.status).upper() in wanted:
            return instance
        if time.monotonic() >= deadline:
            raise GCEOperationError(
                f"GCE instance '{instance_name}' did not reach {', '.join(sorted(wanted))} before timeout",
                entity_kind="gcp-instance",
                entity_name=instance_name,
            )
        time.sleep(poll_interval)


def verify_instance_network(instance: Any, *, network_url: str, subnet_url: str | None) -> None:
    """Require the live instance to remain attached to its persisted network."""
    interfaces = list(instance.network_interfaces)
    if len(interfaces) != 1:
        raise GCEError("GCE instance has an unexpected network-interface layout")
    interface = interfaces[0]
    if canonical_resource_url(str(interface.network)) != canonical_resource_url(network_url):
        raise GCEError("GCE instance is no longer attached to its persisted network identity")
    if subnet_url is not None and canonical_resource_url(str(interface.subnetwork)) != canonical_resource_url(
        subnet_url
    ):
        raise GCEError("GCE instance is no longer attached to its persisted subnetwork identity")
