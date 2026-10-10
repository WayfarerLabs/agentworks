"""Literal provider identity boundaries, including retained v1 recovery input."""

from __future__ import annotations

import pytest

from agentworks.errors import ValidationError
from agentworks.plugins.azure._activation import (
    ActivationPayload,
    decode_activation_payload,
    encode_activation_payload,
    start_url,
)
from agentworks.plugins.azure._identity import MAX_RESOURCE_ID_BYTES, canonical_vm_id, parse_resource_id

RESOURCE = "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
UUID = "00112233-4455-6677-8899-aabbccddeeff"


@pytest.mark.parametrize("group", ["group", "gr:ou:p%2F?#é"])
def test_literal_path_keeps_component_bytes(group):
    path = RESOURCE.replace("/group/", f"/{group}/")
    assert parse_resource_id(path) == ("sub", group, "vm")


@pytest.mark.parametrize("size,accepted", [(2048, True), (2049, False)])
def test_byte_bound(size, accepted):
    # Multibyte literal text makes this a byte boundary, not a character bound.
    prefix = RESOURCE.removesuffix("vm") + "é"
    path = prefix + "x" * (size - len(prefix.encode("utf-8")))
    if accepted:
        assert len(path.encode("utf-8")) == MAX_RESOURCE_ID_BYTES
        assert parse_resource_id(path)[2] == path.rsplit("/", 1)[1]
    else:
        with pytest.raises(ValidationError):
            parse_resource_id(path)


@pytest.mark.parametrize(
    "path",
    [
        None,
        RESOURCE.replace("/group/", "/../"),
        RESOURCE + "\x1f",
        RESOURCE.lower(),
        RESOURCE + "/extra",
        RESOURCE + "\ud800",
    ],
)
def test_invalid_path(path):
    with pytest.raises(ValidationError):
        parse_resource_id(path)


@pytest.mark.parametrize("value", [UUID.upper(), "00000000-0000-0000-0000-000000000000"])
def test_uuid_canonical_without_version_requirement(value):
    assert canonical_vm_id(value) == value.lower()


def test_non_hyphenated_uuid_refuses():
    for value in [UUID.replace("-", ""), "{" + UUID + "}", "urn:uuid:" + UUID, UUID + " ", None, 128, UUID[:-1] + "g"]:
        with pytest.raises(ValidationError):
            canonical_vm_id(value)


def test_legacy_v1_payload_and_path_quoting():
    path = RESOURCE.replace("/group/", "/gr%2F:ou?#é/")
    payload = ActivationPayload(path)
    assert decode_activation_payload(encode_activation_payload(payload)) == payload
    url = start_url(path)
    assert "/gr%252F%3Aou%3F%23%C3%A9/" in url
    assert url.count("?") == 1 and "#" not in url
