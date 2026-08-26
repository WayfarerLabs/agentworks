"""Provider identifiers reject blanks without normalizing literal values."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentworks.plugins.aws.config import AwsAccessKeyAuth, AwsEC2Config, AwsInstanceType
from agentworks.plugins.azure.config import AzureServicePrincipalAuth, AzureVMConfig, AzureVMSize
from agentworks.plugins.gcp.config import GcpGCEConfig, GcpMachineType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.schema import AgwModel

_AZURE_CONFIG: dict[str, object] = {
    "name": "azure-vm",
    "subscription_id": "sub-123",
    "resource_group": "agentworks",
    "region": "eastus",
}

_AZURE_AUTH: dict[str, object] = {
    "mode": "service-principal",
    "tenant_id": "tenant-123",
    "client_id": "client-123",
    "secret": "azure-client-secret",
}

_AZURE_SIZE: dict[str, object] = {
    "cpus": 2,
    "memory": 8,
    "size": "Standard_B2ms",
}

_AWS_CONFIG: dict[str, object] = {
    "name": "aws-ec2",
    "region": "us-east-1",
    "subnet_id": "subnet-00000000000000000",
}

_AWS_AUTH: dict[str, object] = {
    "mode": "access-key",
    "access_key_id": "AKIAEXAMPLE",
    "access_key_secret": "aws-secret-access-key",
    "assume_role_arn": "arn:aws:iam::123456789012:role/agentworks",
}

_AWS_INSTANCE_TYPE: dict[str, object] = {
    "cpus": 2,
    "memory": 8,
    "type": "t4g.large",
    "arch": "arm64",
}

_GCP_CONFIG: dict[str, object] = {
    "name": "gcp-gce",
    "project_id": "agentworks-dev",
    "zone": "us-central1-a",
    "subnet": "app-subnet",
}

_GCP_MACHINE_TYPE: dict[str, object] = {
    "cpus": 2,
    "memory": 8,
    "type": "e2-standard-2",
    "arch": "x86_64",
}

_PROVIDER_IDENTIFIERS = (
    pytest.param(AzureVMConfig, _AZURE_CONFIG, "subscription_id", id="azure-subscription"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, "resource_group", id="azure-resource-group"),
    pytest.param(AzureVMConfig, _AZURE_CONFIG, "region", id="azure-region"),
    pytest.param(AzureServicePrincipalAuth, _AZURE_AUTH, "tenant_id", id="azure-tenant"),
    pytest.param(AzureServicePrincipalAuth, _AZURE_AUTH, "client_id", id="azure-client"),
    pytest.param(AzureVMSize, _AZURE_SIZE, "size", id="azure-vm-size"),
    pytest.param(AwsEC2Config, _AWS_CONFIG, "region", id="aws-region"),
    pytest.param(AwsEC2Config, _AWS_CONFIG, "subnet_id", id="aws-subnet"),
    pytest.param(AwsAccessKeyAuth, _AWS_AUTH, "access_key_id", id="aws-access-key"),
    pytest.param(AwsAccessKeyAuth, _AWS_AUTH, "assume_role_arn", id="aws-assume-role"),
    pytest.param(AwsInstanceType, _AWS_INSTANCE_TYPE, "type", id="aws-instance-type"),
    pytest.param(GcpGCEConfig, _GCP_CONFIG, "project_id", id="gcp-project"),
    pytest.param(GcpGCEConfig, _GCP_CONFIG, "zone", id="gcp-zone"),
    pytest.param(GcpGCEConfig, _GCP_CONFIG, "subnet", id="gcp-subnet"),
    pytest.param(GcpMachineType, _GCP_MACHINE_TYPE, "type", id="gcp-machine-type"),
)

_BLANK_VALUES = (
    pytest.param("", id="empty"),
    pytest.param(" \t\n", id="whitespace-only"),
    pytest.param("\x1c", id="u001c"),
    pytest.param("\x1d", id="u001d"),
    pytest.param("\x1e", id="u001e"),
    pytest.param("\x1f", id="u001f"),
    pytest.param("".join(chr(codepoint) for codepoint in range(0x110000) if chr(codepoint).isspace()), id="all"),
)

_LITERAL_VALUES = (
    pytest.param("  literal-id  ", id="surrounded"),
    pytest.param("\ufeff", id="u-feff"),
)


def _with_value(config: Mapping[str, object], field: str, value: str) -> dict[str, object]:
    return {**config, field: value}


@pytest.mark.parametrize(("model", "config", "field"), _PROVIDER_IDENTIFIERS)
@pytest.mark.parametrize("invalid", _BLANK_VALUES)
def test_provider_identifiers_reject_blanks(
    model: type[AgwModel],
    config: Mapping[str, object],
    field: str,
    invalid: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(_with_value(config, field, invalid))


@pytest.mark.parametrize(("model", "config", "field"), _PROVIDER_IDENTIFIERS)
@pytest.mark.parametrize("invalid", _BLANK_VALUES)
def test_provider_identifier_schemas_reject_blanks(
    model: type[AgwModel],
    config: Mapping[str, object],
    field: str,
    invalid: str,
) -> None:
    validator = Draft202012Validator(model.model_json_schema())

    assert list(validator.iter_errors(_with_value(config, field, invalid)))


@pytest.mark.parametrize(("model", "config", "field"), _PROVIDER_IDENTIFIERS)
@pytest.mark.parametrize("literal", _LITERAL_VALUES)
def test_provider_identifiers_preserve_literals(
    model: type[AgwModel],
    config: Mapping[str, object],
    field: str,
    literal: str,
) -> None:
    config_with_literal = _with_value(config, field, literal)
    parsed = model.model_validate(config_with_literal)

    assert getattr(parsed, field) == literal
    assert list(Draft202012Validator(model.model_json_schema()).iter_errors(config_with_literal)) == []
