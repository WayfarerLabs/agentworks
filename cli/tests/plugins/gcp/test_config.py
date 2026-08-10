"""Reviewed GCP site schema, defaults, catalog, and image mapping."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from agentworks.errors import ConfigError
from agentworks.plugins.gcp.config import (
    DEFAULT_MACHINE_TYPES,
    IMAGE_PROJECT,
    GcpAmbientAuth,
    GcpGCEConfig,
    GcpServiceAccountAuth,
    MachineTypeSelection,
    image_family_for_arch,
    machine_catalog,
    select_machine_type,
)
from agentworks.schema import RefOwner, extract_references, filled_defaults

_OWNER = RefOwner("vm-site", "gcp-dev")
_BASE: dict[str, object] = {
    "name": "gcp-gce",
    "project_id": "agentworks-dev",
    "zone": "us-central1-a",
}


def _validate(blob: dict[str, object]) -> GcpGCEConfig:
    filled = filled_defaults(GcpGCEConfig, blob, _OWNER)
    return GcpGCEConfig.model_validate(filled)


def test_outer_auth_omission_selects_ambient_but_explicit_null_is_invalid() -> None:
    omitted = _validate(dict(_BASE))
    assert omitted.auth == GcpAmbientAuth(mode="ambient")

    with pytest.raises(ValidationError):
        _validate({**_BASE, "auth": None})


@pytest.mark.parametrize("secret", [pytest.param(None, id="explicit-null"), pytest.param("omitted", id="omitted")])
def test_service_account_secret_null_and_omission_select_the_well_known_default(secret: str | None) -> None:
    auth: dict[str, object] = {"mode": "service-account"}
    if secret is None:
        auth["secret"] = None
    model = _validate({**_BASE, "auth": auth})

    assert model.auth == GcpServiceAccountAuth(mode="service-account", secret="gcp-service-account-key")
    refs = extract_references(
        GcpGCEConfig,
        filled_defaults(GcpGCEConfig, {**_BASE, "auth": auth}, _OWNER),
    )
    assert [(ref.kind, ref.name) for ref in refs] == [("secret", "gcp-service-account-key")]


@pytest.mark.parametrize(
    "blob",
    [
        {**_BASE, "project_id": ""},
        {**_BASE, "zone": ""},
        {**_BASE, "subnet": ""},
        {**_BASE, "machine_types": []},
        {**_BASE, "auth": {"mode": "service-account", "secret": ""}},
        {**_BASE, "auth": {"mode": "service-account", "ambient": True}},
        {**_BASE, "auth": {"mode": "unknown"}},
    ],
    ids=("project", "zone", "subnet", "empty-catalog", "blank-secret", "mixed-arm", "unknown-arm"),
)
def test_closed_schema_rejects_invalid_or_mixed_shapes(blob: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _validate(blob)


@pytest.mark.parametrize("field", ["cpus", "memory"])
@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", None, True])
def test_machine_counts_are_strict_positive_integers(field: str, bad: object) -> None:
    entry: dict[str, object] = {"cpus": 2, "memory": 8, "type": "e2-standard-2", "arch": "x86_64"}
    entry[field] = bad
    with pytest.raises(ValidationError):
        _validate({**_BASE, "machine_types": [entry]})


@pytest.mark.parametrize(
    "change",
    [
        {"type": ""},
        {"arch": "sparc"},
        {"extra": "nope"},
    ],
)
def test_machine_entry_is_closed_and_exact(change: dict[str, object]) -> None:
    entry: dict[str, object] = {"cpus": 2, "memory": 8, "type": "e2-standard-2", "arch": "x86_64"}
    entry.update(change)
    with pytest.raises(ValidationError):
        _validate({**_BASE, "machine_types": [entry]})


def test_exact_default_catalog_and_override_projection() -> None:
    default = _validate(dict(_BASE))
    assert machine_catalog(default) is DEFAULT_MACHINE_TYPES
    assert (
        MachineTypeSelection(2, 8, "e2-standard-2", "x86_64"),
        MachineTypeSelection(4, 16, "e2-standard-4", "x86_64"),
        MachineTypeSelection(8, 32, "e2-standard-8", "x86_64"),
        MachineTypeSelection(16, 64, "e2-standard-16", "x86_64"),
        MachineTypeSelection(32, 128, "e2-standard-32", "x86_64"),
    ) == DEFAULT_MACHINE_TYPES

    override = _validate(
        {
            **_BASE,
            "machine_types": [
                {"cpus": 4, "memory": 16, "type": "t2a-standard-4", "arch": "arm64"},
            ],
        }
    )
    assert machine_catalog(override) == (MachineTypeSelection(4, 16, "t2a-standard-4", "arm64"),)


def test_selection_is_order_independent_and_satisfies_both_axes() -> None:
    entries = (
        MachineTypeSelection(8, 16, "wide-cpu", "x86_64"),
        MachineTypeSelection(4, 32, "wide-memory", "arm64"),
        MachineTypeSelection(4, 16, "small-z", "x86_64"),
        MachineTypeSelection(4, 16, "small-a", "arm64"),
    )
    expected = MachineTypeSelection(4, 16, "small-a", "arm64")
    assert select_machine_type(entries, cpus=4, memory_gib=16) == expected
    assert select_machine_type(tuple(reversed(entries)), cpus=4, memory_gib=16) == expected
    assert select_machine_type(entries, cpus=5, memory_gib=16).type == "wide-cpu"
    assert select_machine_type(entries, cpus=4, memory_gib=17).type == "wide-memory"


def test_selection_failure_is_typed_and_names_the_largest_entry() -> None:
    with pytest.raises(ConfigError, match="e2-standard-32") as caught:
        select_machine_type(DEFAULT_MACHINE_TYPES, cpus=64, memory_gib=256)
    assert caught.value.hint is not None and "machine_types" in caught.value.hint


def test_image_family_mapping_is_exact() -> None:
    assert IMAGE_PROJECT == "debian-cloud"
    assert image_family_for_arch("x86_64") == "debian-12"
    assert image_family_for_arch("arm64") == "debian-12-arm64"


def test_secret_marker_schema_is_nullable_optional_and_input_is_not_mutated() -> None:
    raw = {**_BASE, "auth": {"mode": "service-account", "secret": None}}
    original = deepcopy(raw)
    filled = filled_defaults(GcpGCEConfig, raw, _OWNER)
    assert raw == original
    assert filled != raw

    schema = GcpGCEConfig.model_json_schema()
    service = schema["$defs"]["GcpServiceAccountAuth"]
    assert "secret" not in service.get("required", [])
    secret_schema = service["properties"]["secret"]
    assert secret_schema["x-agw-ref"]["kind"] == "secret"
    assert {arm.get("type") for arm in secret_schema["anyOf"]} == {"string", "null"}
