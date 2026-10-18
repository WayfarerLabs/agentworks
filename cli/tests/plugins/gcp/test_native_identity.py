"""Persisted native selection is bounded and exact without rewriting the row."""

from copy import deepcopy
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.gcp._native_access import GCEOwnedRead
from agentworks.plugins.gcp._native_identity import NativeIdentity, canonical_path
from tests.plugins.gcp.test_owned_read import make_reader


def native_selection() -> tuple[GCEOwnedRead, Any]:
    reader, vm = make_reader()
    vm.platform_metadata.update(
        network_url="https://compute.googleapis.com/compute/v1/projects/project/global/networks/default",
        subnet_url="projects/project/regions/us-central1/subnetworks/default",
        network_tag="selected-tag",
        deny_rule="selected-deny",
        deny_rule_id="456",
        access_config_name="External NAT",
    )
    return reader, vm


def test_native_selection_is_minimal_and_does_not_rewrite_persisted_urls():
    _, vm = native_selection()
    original = deepcopy(vm.platform_metadata)
    identity = NativeIdentity.from_row(vm)
    assert identity.network_url == "projects/project/global/networks/default"
    assert identity.subnet_url == "projects/project/regions/us-central1/subnetworks/default"
    assert identity.locator.token == "gcp-gce:project:us-central1-a:123"
    assert vm.platform_metadata == original


@pytest.mark.parametrize(
    "prefix", ["", "https://compute.googleapis.com/compute/v1/", "https://www.googleapis.com/compute/v1/"]
)
@pytest.mark.parametrize(
    "kind,path",
    [
        ("network", "projects/project/global/networks/default"),
        ("subnet", "projects/project/regions/us-central1/subnetworks/default"),
    ],
)
def test_exact_supported_resource_spellings(prefix, kind, path):
    assert canonical_path(prefix + path, project_id="project", kind=kind) == path


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.invalid/compute/v1/projects/project/global/networks/default",
        "https://compute.googleapis.com:443/compute/v1/projects/project/global/networks/default",
        "https://user@compute.googleapis.com/compute/v1/projects/project/global/networks/default",
        "https://compute.googleapis.com/compute/v1/projects/project/global/networks/default?x=1",
        "https://compute.googleapis.com/compute/v1/projects/project/global/networks/default#x",
        "projects/foreign/global/networks/default",
        "projects/project/global/networks/default/extra",
        "projects/project/global/networks/../default",
        "projects/project/global/networks/%64efault",
        "projects/project/global/networks/",
        "/projects/project/global/networks/default",
        "projects/project/global/networks/" + "x" * 64,
        None,
    ],
)
def test_refuses_ambiguous_or_foreign_network_paths(path):
    with pytest.raises(ValidationError):
        canonical_path(path, project_id="project", kind="network")


@pytest.mark.parametrize(
    "key,value",
    [
        ("project_id", "Project"),
        ("zone", "zone/x"),
        ("instance_name", "x" * 64),
        ("instance_id", "0"),
        ("instance_id", "01"),
        ("instance_id", "18446744073709551616"),
        ("deny_rule_id", 123),
        ("deny_rule_id", "-1"),
        ("network_tag", "1bad"),
        ("network_tag", "bad-"),
        ("deny_rule", "Upper"),
        ("access_config_name", "External\nNAT"),
        ("access_config_name", "x" * 64),
        ("subnet_url", ""),
    ],
)
def test_rejects_malformed_persisted_selection(key, value):
    _, vm = native_selection()
    vm.platform_metadata[key] = value
    with pytest.raises(StateError):
        NativeIdentity.from_row(vm)


@pytest.mark.parametrize(
    "key",
    [
        "project_id",
        "zone",
        "instance_name",
        "instance_id",
        "network_url",
        "network_tag",
        "deny_rule",
        "deny_rule_id",
        "access_config_name",
    ],
)
def test_requires_only_fields_consumed_by_native_access(key):
    _, vm = native_selection()
    del vm.platform_metadata[key]
    with pytest.raises(StateError):
        NativeIdentity.from_row(vm)


def test_subnet_optional_and_uint64_maximum():
    _, vm = native_selection()
    del vm.platform_metadata["subnet_url"]
    vm.platform_metadata["instance_id"] = vm.platform_metadata["deny_rule_id"] = "18446744073709551615"
    assert NativeIdentity.from_row(vm).subnet_url is None
