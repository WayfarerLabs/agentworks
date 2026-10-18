"""Full native observations admit only the selected instance and bounded route facts."""

import json
from dataclasses import replace
from typing import Any

import pytest

from agentworks.db import VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._native_access import GCEInstanceObservation
from agentworks.plugins.gcp._native_identity import NativeIdentity
from tests.plugins.gcp.test_native_identity import native_selection
from tests.plugins.gcp.test_owned_read import instance


def native_instance() -> dict[str, Any]:
    return instance(
        networkInterfaces=[
            {
                "network": "https://www.googleapis.com/compute/v1/projects/project/global/networks/default",
                "subnetwork": "projects/project/regions/us-central1/subnetworks/default",
                "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT", "natIP": "192.0.2.17"}],
            }
        ],
        tags={"items": ["selected-tag", "other-tag"]},
        serviceAccounts=[{"email": "selected@project.iam.gserviceaccount.com"}],
    )


def project_native(value: Any) -> GCEInstanceObservation:
    reader, vm = native_selection()
    return reader._project_instance(json.dumps(value).encode(), NativeIdentity.from_row(vm))


def test_full_observation_carries_all_route_target_facts():
    result = project_native(native_instance())
    assert result.locator.token == "gcp-gce:project:us-central1-a:123"
    assert result.power is VMStatus.RUNNING and result.raw_power == "RUNNING"
    assert result.endpoint == "192.0.2.17"
    assert result.service_accounts == ("selected@project.iam.gserviceaccount.com",)
    assert result.tags == ("selected-tag", "other-tag")


@pytest.mark.parametrize("status", ["TERMINATED", "STOPPED", "PROVISIONING", "STAGING", "RUNNING", None])
def test_no_invented_endpoint_and_raw_transition_preserved(status):
    value = native_instance()
    value["status"] = status
    value["networkInterfaces"][0]["accessConfigs"][0].pop("natIP")
    del value["serviceAccounts"]
    result = project_native(value)
    assert result.raw_power == status
    assert result.endpoint is None and result.service_accounts == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "124"),
        ("name", "other"),
        ("networkInterfaces", []),
        ("networkInterfaces", [{}]),
        ("tags", {}),
        ("tags", {"items": ["other"]}),
        ("tags", {"items": ["selected-tag"] * 65}),
        ("tags", {"items": ["selected-tag", "selected-tag"]}),
        ("tags", {"items": ["selected-tag", "bad-"]}),
        ("serviceAccounts", [{"email": "bad\n@example.com"}]),
        ("serviceAccounts", [{}]),
        ("serviceAccounts", [None]),
        ("serviceAccounts", [{"email": "a@b"}] * 17),
        ("status", []),
        ("status", "X" * 65),
    ],
)
def test_refuses_missing_foreign_or_unbounded_route_facts(field, value):
    body = native_instance()
    body[field] = value
    with pytest.raises(ValidationError):
        project_native(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("network", "projects/foreign/global/networks/default"),
        ("subnetwork", "projects/project/regions/us-central1/subnetworks/other"),
        ("subnetwork", None),
        ("accessConfigs", []),
        ("accessConfigs", [{"name": "Other", "type": "ONE_TO_ONE_NAT"}]),
        ("accessConfigs", [{"name": "External NAT", "type": "DIRECT_IPV6"}]),
    ],
)
def test_refuses_foreign_nic_or_access_config(field, value):
    body = native_instance()
    body["networkInterfaces"][0][field] = value
    with pytest.raises(ValidationError):
        project_native(body)


@pytest.mark.parametrize("address", ["192.000.2.17", "192.0.2.17/32", "2001:db8::1", "", 3221226001, True, None])
def test_supplied_endpoint_requires_canonical_ipv4(address):
    body = native_instance()
    body["networkInterfaces"][0]["accessConfigs"][0]["natIP"] = address
    with pytest.raises(ValidationError):
        project_native(body)


def test_refuses_extra_nics_or_access_configs():
    for field in ("nic", "config"):
        body = native_instance()
        target = body["networkInterfaces"] if field == "nic" else body["networkInterfaces"][0]["accessConfigs"]
        target.append(target[0])
        with pytest.raises(ValidationError):
            project_native(body)


def test_subnet_omission_matches_subnet_free_selection():
    reader, vm = native_selection()
    del vm.platform_metadata["subnet_url"]
    body = native_instance()
    del body["networkInterfaces"][0]["subnetwork"]
    assert reader._project_instance(json.dumps(body).encode(), NativeIdentity.from_row(vm)).endpoint == "192.0.2.17"


@pytest.mark.parametrize(
    "subnet",
    [
        "projects/project/regions/us-central1/subnetworks/default",
        "https://compute.googleapis.com/compute/v1/projects/project/regions/us-central1/subnetworks/auto-selected",
        "https://www.googleapis.com/compute/v1/projects/project/regions/us-central1/subnetworks/auto-selected",
    ],
)
def test_unrecorded_subnet_accepts_valid_live_provider_selection(subnet):
    reader, vm = native_selection()
    del vm.platform_metadata["subnet_url"]
    body = native_instance()
    body["networkInterfaces"][0]["subnetwork"] = subnet
    assert reader._project_instance(json.dumps(body).encode(), NativeIdentity.from_row(vm)).endpoint == "192.0.2.17"


@pytest.mark.parametrize(
    "subnet",
    [
        "projects/foreign/regions/us-central1/subnetworks/default",
        "https://foreign.invalid/compute/v1/projects/project/regions/us-central1/subnetworks/default",
        "projects/project/regions/us-central1/subnetworks/default?x=1",
        "projects/project/regions/us-central1/subnetworks/%64efault",
        "projects/project/regions/us-central1/subnetworks/../default",
        "",
        None,
        42,
    ],
)
def test_unrecorded_subnet_still_refuses_foreign_or_malformed_live_selection(subnet):
    reader, vm = native_selection()
    del vm.platform_metadata["subnet_url"]
    body = native_instance()
    body["networkInterfaces"][0]["subnetwork"] = subnet
    with pytest.raises(ValidationError):
        reader._project_instance(json.dumps(body).encode(), NativeIdentity.from_row(vm))


def test_recorded_subnet_requires_live_subnet_presence():
    body = native_instance()
    del body["networkInterfaces"][0]["subnetwork"]
    with pytest.raises(ValidationError):
        project_native(body)


def test_provider_collection_upper_bounds_and_optional_status():
    body = native_instance()
    body.pop("status")
    body["tags"]["items"] = ["selected-tag", *(f"tag-{i}" for i in range(63))]
    body["serviceAccounts"] = [{"email": f"account-{i}@project.iam.gserviceaccount.com"} for i in range(16)]
    result = project_native(body)
    assert result.raw_power is None and result.power is VMStatus.UNKNOWN
    assert len(result.tags) == 64 and len(result.service_accounts) == 16


def test_declared_identity_must_match_reader_before_credential_admission(monkeypatch):
    reader, vm = native_selection()
    monkeypatch.setattr(reader._auth, "begin", lambda _: pytest.fail("foreign identity reached credentials"))
    with pytest.raises(StateError):
        reader.observe_instance(Deadline.after(5), identity=replace(NativeIdentity.from_row(vm), instance_id="124"))
    assert reader.close(Deadline.after(5))
