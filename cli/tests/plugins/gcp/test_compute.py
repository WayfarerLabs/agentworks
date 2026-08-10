"""Read-only GCE location, machine, image, collision, and IP helpers."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.plugins.gcp.compute import (
    get_project,
    get_zone,
    live_external_ipv4,
    require_instance_name_available,
    resolve_debian_image,
    verify_live_machine_type,
)
from agentworks.plugins.gcp.config import GcpGCEConfig, MachineTypeSelection
from agentworks.plugins.gcp.errors import (
    GCEAuthenticationError,
    GCEConflictError,
    GCEError,
    GCEOperationError,
    GCEPermissionError,
    GCEQuotaError,
    call_google,
)

_CONFIG = GcpGCEConfig(
    name="gcp-gce",
    project_id="project-a",
    zone="us-central1-a",
)
_SENTINEL = "provider-reflected-secret-SENTINEL"


def _api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class _Cache:
    def __init__(self, **clients: object) -> None:
        self.clients = clients
        self.requested: list[str] = []

    def client(self, kind: str, _ctx: RunContext) -> Any:
        self.requested.append(kind)
        return self.clients[kind]


class _GetClient:
    def __init__(self, value: object = None, failure: Exception | None = None) -> None:
        self.value = value
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.value


def test_project_and_zone_use_explicit_target_project() -> None:
    projects = _GetClient(SimpleNamespace(name="project-a"))
    zones = _GetClient(SimpleNamespace(name="us-central1-a"))
    cache = _Cache(projects=projects, zones=zones)

    assert get_project(cache, RunContext(), "project-a").name == "project-a"  # type: ignore[arg-type]
    assert get_zone(cache, RunContext(), "project-a", "us-central1-a").name == "us-central1-a"  # type: ignore[arg-type]
    assert projects.calls == [{"project": "project-a"}]
    assert zones.calls == [{"project": "project-a", "zone": "us-central1-a"}]


@pytest.mark.parametrize(("kind", "helper"), [("project", "project"), ("zone", "zone")])
def test_missing_location_is_pre_mutation_config_error(kind: str, helper: str) -> None:
    missing = _GetClient(failure=_api_error(api_exceptions.NotFound, "safe provider text"))
    cache = _Cache(projects=missing, zones=missing)
    with pytest.raises(ConfigError, match=helper):
        if kind == "project":
            get_project(cache, RunContext(), "project-a")  # type: ignore[arg-type]
        else:
            get_zone(cache, RunContext(), "project-a", "us-central1-a")  # type: ignore[arg-type]


def test_live_machine_shape_and_architecture_are_verified() -> None:
    machine = compute_v1.MachineType(
        name="t2a-standard-4",
        guest_cpus=4,
        memory_mb=16384,
        architecture="ARM64",
    )
    client = _GetClient(machine)
    cache = _Cache(**{"machine-types": client})
    selected = MachineTypeSelection(4, 16, "t2a-standard-4", "arm64")

    assert verify_live_machine_type(cache, RunContext(), _CONFIG, selected) is machine  # type: ignore[arg-type]
    assert client.calls == [{"project": "project-a", "zone": "us-central1-a", "machine_type": "t2a-standard-4"}]


@pytest.mark.parametrize(
    "machine",
    [
        compute_v1.MachineType(guest_cpus=8, memory_mb=16384, architecture="ARM64"),
        compute_v1.MachineType(guest_cpus=4, memory_mb=8192, architecture="ARM64"),
        compute_v1.MachineType(guest_cpus=4, memory_mb=16384, architecture="X86_64"),
    ],
    ids=("cpus", "memory", "architecture"),
)
def test_live_machine_mismatch_fails_before_mutation(machine: compute_v1.MachineType) -> None:
    cache = _Cache(**{"machine-types": _GetClient(machine)})
    selected = MachineTypeSelection(4, 16, "t2a-standard-4", "arm64")
    with pytest.raises(ConfigError, match="does not match"):
        verify_live_machine_type(cache, RunContext(), _CONFIG, selected)  # type: ignore[arg-type]


def test_image_family_uses_public_project_and_matching_architecture() -> None:
    image = compute_v1.Image(
        name="debian-12-arm64-v1",
        architecture="ARM64",
        self_link="https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-arm64-v1",
    )

    class Images:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_from_family(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return image

    images = Images()
    cache = _Cache(images=images)
    assert resolve_debian_image(cache, RunContext(), "arm64") is image  # type: ignore[arg-type]
    assert images.calls == [{"project": "debian-cloud", "family": "debian-12-arm64"}]


def test_image_architecture_mismatch_is_rejected() -> None:
    image = compute_v1.Image(architecture="X86_64", self_link="projects/debian-cloud/images/wrong")

    class Images:
        def get_from_family(self, **_kwargs: object) -> object:
            return image

    with pytest.raises(ConfigError, match="reports architecture"):
        resolve_debian_image(_Cache(images=Images()), RunContext(), "arm64")  # type: ignore[arg-type]


def test_exact_instance_collision_and_not_found_paths() -> None:
    present = _GetClient(compute_v1.Instance(name="vm-a"))
    with pytest.raises(GCEConflictError, match="already exists"):
        require_instance_name_available(
            _Cache(instances=present),  # type: ignore[arg-type]
            RunContext(),
            project_id="project-a",
            zone="us-central1-a",
            instance_name="vm-a",
        )

    absent = _GetClient(failure=_api_error(api_exceptions.NotFound, "gone"))
    require_instance_name_available(
        _Cache(instances=absent),  # type: ignore[arg-type]
        RunContext(),
        project_id="project-a",
        zone="us-central1-a",
        instance_name="vm-a",
    )


def test_external_ipv4_is_read_live_from_named_nat_config() -> None:
    instance = compute_v1.Instance(
        network_interfaces=[
            compute_v1.NetworkInterface(
                access_configs=[
                    compute_v1.AccessConfig(name="other", type_="ONE_TO_ONE_NAT", nat_i_p="198.51.100.1"),
                    compute_v1.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT", nat_i_p="203.0.113.9"),
                ]
            )
        ]
    )
    assert live_external_ipv4(instance) == "203.0.113.9"
    instance.network_interfaces[0].access_configs[1].nat_i_p = "203.0.113.10"
    assert live_external_ipv4(instance) == "203.0.113.10"

    with pytest.raises(GCEError, match="no live"):
        live_external_ipv4(compute_v1.Instance())


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (_api_error(api_exceptions.Unauthenticated, _SENTINEL), GCEAuthenticationError),
        (_api_error(api_exceptions.PermissionDenied, _SENTINEL), GCEPermissionError),
        (_api_error(api_exceptions.AlreadyExists, _SENTINEL), GCEConflictError),
        (_api_error(api_exceptions.ResourceExhausted, _SENTINEL), GCEQuotaError),
        (_api_error(api_exceptions.DeadlineExceeded, _SENTINEL), GCEOperationError),
        (RuntimeError(_SENTINEL), GCEError),
    ],
)
def test_provider_error_mapping_is_typed_and_drops_full_exception_graph(
    provider: Exception,
    expected: type[GCEError],
) -> None:
    provider.__cause__ = ValueError(_SENTINEL)
    provider.extra = {"request": _SENTINEL}  # type: ignore[attr-defined]
    with pytest.raises(expected) as caught:
        call_google(lambda: (_ for _ in ()).throw(provider), operation="testing", resource="safe-resource")

    graph = [caught.value, *caught.value.args, *vars(caught.value).values()]
    assert _SENTINEL not in "\n".join(f"{value!s}\n{value!r}" for value in graph)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
