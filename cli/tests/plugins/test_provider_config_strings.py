"""Azure identifiers and the AWS access-key ID reject blanks without trimming."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from agentworks.plugins.aws.config import AwsEC2Config
from agentworks.plugins.azure.config import AzureVMConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.schema import AgwModel

_AZURE_CONFIG: dict[str, object] = {
    "name": "azure-vm",
    "subscription_id": "sub-123",
    "resource_group": "agentworks",
    "region": "eastus",
    "auth": {
        "mode": "service-principal",
        "tenant_id": "tenant-123",
        "client_id": "client-123",
        "secret": "azure-client-secret",
    },
}

_AWS_CONFIG: dict[str, object] = {
    "name": "aws-ec2",
    "region": "us-east-1",
    "auth": {
        "mode": "access-key",
        "access_key_id": "AKIAEXAMPLE",
        "access_key_secret": "aws-secret-access-key",
    },
}

_IDENTIFIERS = (
    pytest.param(AzureVMConfig, _AZURE_CONFIG, ("subscription_id",), id="azure-subscription"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, ("resource_group",), id="azure-resource-group"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, ("region",), id="azure-region"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, ("auth", "tenant_id"), id="azure-tenant"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, ("auth", "client_id"), id="azure-client"),
    pytest.param(AwsEC2Config, _AWS_CONFIG, ("auth", "access_key_id"), id="aws-access-key"),
)


def _with_value(config: Mapping[str, object], path: tuple[str, ...], value: str) -> dict[str, object]:
    changed = deepcopy(dict(config))
    target = changed
    for part in path[:-1]:
        target = cast("dict[str, object]", target[part])
    target[path[-1]] = value
    return changed


@pytest.mark.parametrize(("model", "config", "path"), _IDENTIFIERS)
@pytest.mark.parametrize("invalid", ["", " \t\n"], ids=["empty", "whitespace-only"])
def test_selected_azure_fields_and_aws_access_key_id_reject_blanks(
    model: type[AgwModel],
    config: Mapping[str, object],
    path: tuple[str, ...],
    invalid: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(_with_value(config, path, invalid))


@pytest.mark.parametrize(("model", "config", "path"), _IDENTIFIERS)
def test_selected_azure_fields_and_aws_access_key_id_preserve_literals(
    model: type[AgwModel],
    config: Mapping[str, object],
    path: tuple[str, ...],
) -> None:
    literal = "  literal-id  "
    parsed = model.model_validate(_with_value(config, path, literal))
    parsed_value: object = parsed
    for part in path:
        parsed_value = getattr(parsed_value, part)
    assert parsed_value == literal
