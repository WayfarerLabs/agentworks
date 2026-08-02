"""The split invoked-config API on the four VM platforms:
``dependencies(owner, config)`` extracts the ConfigReference edges a
platform_config blob implies (total, non-throwing) and
``validate(owner, config)`` is the throwing shape check.
"""

from __future__ import annotations

import pytest

from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import ConfigError
from agentworks.plugins.aws.platform import DEFAULT_SECRET_ACCESS_KEY, EC2Platform
from agentworks.plugins.azure.platform import DEFAULT_CLIENT_SECRET, AzureVMPlatform
from agentworks.plugins.proxmox.platform import (
    DEFAULT_TOKEN_SECRET,
    ProxmoxPlatform,
)
from agentworks.resources.reference import ConfigReference

AZURE_CONFIG = {
    "subscription_id": "0000",
    "resource_group": "agw",
    "region": "eastus",
}
AZURE_SP = {
    "tenant_id": "tenant-0000",
    "client_id": "client-0000",
    "secret": "az-sp",
}
PROXMOX_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "pve1",
    "token_id": "agw@pam!agw",
    "template_vmid": 9000,
}
EC2_CONFIG = {"region": "us-east-1"}
EC2_CREDS = {"access_key_id": "AKIAEXAMPLE", "access_key_secret": "aws-secret"}


def test_registry_names_match_classes() -> None:
    assert {
        "lima": LimaPlatform,
        "wsl2": WSL2Platform,
        "azure-vm": AzureVMPlatform,
        "proxmox": ProxmoxPlatform,
        "aws-ec2": EC2Platform,
    } == VM_PLATFORM_REGISTRY
    for name, cls in VM_PLATFORM_REGISTRY.items():
        assert cls.name == name
        assert cls.description


# -- validate (the throwing shape check) -------------------------------------


def test_lima_accepts_empty_and_vm_host() -> None:
    assert LimaPlatform.validate("t", {}) is None
    assert LimaPlatform.validate("t", {"vm_host": "me@box"}) is None


def test_lima_rejects_bad_vm_host_and_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="vm_host"):
        LimaPlatform.validate("t", {"vm_host": ""})
    with pytest.raises(ConfigError, match="unknown lima"):
        LimaPlatform.validate("t", {"host": "x"})


def test_wsl2_accepts_no_configuration() -> None:
    assert WSL2Platform.validate("t", {}) is None
    with pytest.raises(ConfigError, match="accepts no configuration"):
        WSL2Platform.validate("t", {"anything": 1})


def test_azure_requires_the_three_keys() -> None:
    assert AzureVMPlatform.validate("t", AZURE_CONFIG) is None
    for missing in AZURE_CONFIG:
        broken = {k: v for k, v in AZURE_CONFIG.items() if k != missing}
        with pytest.raises(ConfigError, match=missing):
            AzureVMPlatform.validate("t", broken)
    with pytest.raises(ConfigError, match="unknown azure"):
        AzureVMPlatform.validate("t", {**AZURE_CONFIG, "extra": "x"})


def test_azure_service_principal_is_optional_and_shape_checked() -> None:
    """The optional ``service_principal`` table: absent is the ambient
    path (and stays valid), present must carry both identifiers, may
    name its secret, and rejects anything else."""
    assert AzureVMPlatform.validate("t", {**AZURE_CONFIG, "service_principal": AZURE_SP}) is None
    # ``secret`` is optional (the default name applies).
    assert (
        AzureVMPlatform.validate(
            "t",
            {**AZURE_CONFIG, "service_principal": {k: v for k, v in AZURE_SP.items() if k != "secret"}},
        )
        is None
    )
    for missing in ("tenant_id", "client_id"):
        broken = {k: v for k, v in AZURE_SP.items() if k != missing}
        with pytest.raises(ConfigError, match=f"service_principal.{missing}"):
            AzureVMPlatform.validate("t", {**AZURE_CONFIG, "service_principal": broken})


@pytest.mark.parametrize(
    ("sp", "match"),
    [
        pytest.param("not-a-table", "must be a table", id="not-a-table"),
        pytest.param({**AZURE_SP, "tenant_id": ""}, "tenant_id", id="empty-tenant"),
        pytest.param({**AZURE_SP, "client_id": 7}, "client_id", id="non-string-client"),
        pytest.param({**AZURE_SP, "secret": ""}, "bare secret name", id="empty-secret-name"),
        pytest.param({**AZURE_SP, "secret": 7}, "bare secret name", id="non-string-secret-name"),
        # The field is deliberately named `secret` (a NAME), so the
        # value-shaped spelling an operator might reach for is refused.
        pytest.param({**AZURE_SP, "client_secret": "hunter2"}, "unknown field", id="client-secret-value"),
        pytest.param({**AZURE_SP, "certificate": "x"}, "unknown field", id="future-variant-not-yet"),
    ],
)
def test_azure_rejects_malformed_service_principal(sp: object, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        AzureVMPlatform.validate("t", {**AZURE_CONFIG, "service_principal": sp})


def test_aws_ec2_requires_region() -> None:
    assert EC2Platform.validate("t", EC2_CONFIG) is None
    with pytest.raises(ConfigError, match="region is required"):
        EC2Platform.validate("t", {})
    with pytest.raises(ConfigError, match="unknown aws-ec2"):
        EC2Platform.validate("t", {**EC2_CONFIG, "extra": "x"})


def test_aws_ec2_optional_subnet_id_is_shape_checked() -> None:
    assert EC2Platform.validate("t", {**EC2_CONFIG, "subnet_id": "subnet-1"}) is None
    with pytest.raises(ConfigError, match="subnet_id"):
        EC2Platform.validate("t", {**EC2_CONFIG, "subnet_id": ""})


def test_aws_ec2_rejects_the_removed_ami_override() -> None:
    """There is no image knob: the fleet standardizes on Debian bookworm, so an
    ``ami`` key is an unknown field, not a pin."""
    with pytest.raises(ConfigError, match="unknown aws-ec2"):
        EC2Platform.validate("t", {**EC2_CONFIG, "ami": "ami-123"})


def test_aws_ec2_credentials_is_optional_and_shape_checked() -> None:
    """The optional ``credentials`` table: absent is the ambient path, present
    must carry access_key_id, may name its secret and a role, and rejects
    anything else."""
    assert EC2Platform.validate("t", {**EC2_CONFIG, "credentials": EC2_CREDS}) is None
    assert EC2Platform.validate("t", {**EC2_CONFIG, "credentials": {"access_key_id": "AKIA"}}) is None
    assert EC2Platform.validate("t", {**EC2_CONFIG, "credentials": {**EC2_CREDS, "assume_role_arn": "arn:x"}}) is None


@pytest.mark.parametrize(
    ("creds", "match"),
    [
        pytest.param("not-a-table", "must be a table", id="not-a-table"),
        pytest.param({}, "access_key_id", id="missing-access-key"),
        pytest.param({**EC2_CREDS, "access_key_id": ""}, "access_key_id", id="empty-access-key"),
        pytest.param({**EC2_CREDS, "access_key_secret": ""}, "bare secret name", id="empty-secret-name"),
        pytest.param({**EC2_CREDS, "access_key_secret": 7}, "bare secret name", id="non-string-secret-name"),
        pytest.param({**EC2_CREDS, "assume_role_arn": ""}, "assume_role_arn", id="empty-role"),
        # The field is `access_key_secret` (a NAME), so AWS's own value-shaped
        # term and the old `secret` spelling are both refused as unknown fields.
        pytest.param({**EC2_CREDS, "secret_access_key": "hunter2"}, "unknown field", id="secret-value-spelling"),
        pytest.param({**EC2_CREDS, "secret": "aws-secret"}, "unknown field", id="old-secret-spelling"),
    ],
)
def test_aws_ec2_rejects_malformed_credentials(creds: object, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        EC2Platform.validate("t", {**EC2_CONFIG, "credentials": creds})


def test_aws_ec2_rejects_bad_instance_type_arch() -> None:
    bad = {**EC2_CONFIG, "instance_types": [{"cpus": 2, "memory": 4, "type": "x", "arch": "amd64"}]}
    with pytest.raises(ConfigError, match="arch"):
        EC2Platform.validate("t", bad)


def test_proxmox_validation_errors() -> None:
    with pytest.raises(ConfigError, match="node is required"):
        ProxmoxPlatform.validate("t", {k: v for k, v in PROXMOX_CONFIG.items() if k != "node"})
    with pytest.raises(ConfigError, match="template_vmid must be an integer"):
        ProxmoxPlatform.validate("t", {**PROXMOX_CONFIG, "template_vmid": "not-a-number"})
    with pytest.raises(ConfigError, match="token_secret must be a bare secret"):
        ProxmoxPlatform.validate("t", {**PROXMOX_CONFIG, "token_secret": ""})
    with pytest.raises(ConfigError, match="unknown proxmox"):
        ProxmoxPlatform.validate("t", {**PROXMOX_CONFIG, "nodee": "x"})


# -- dependencies (total edge extraction) ------------------------------------


def test_config_free_platforms_imply_no_edges() -> None:
    assert LimaPlatform.dependencies("t", {"vm_host": "me@box"}) == ()
    assert WSL2Platform.dependencies("t", {}) == ()
    # No service_principal: azure authenticates ambiently and implies no
    # reference, exactly as every azure-vm site did before issue #199.
    assert AzureVMPlatform.dependencies("t", AZURE_CONFIG) == ()
    # No credentials table: ec2 authenticates ambiently and implies no edge.
    assert EC2Platform.dependencies("t", EC2_CONFIG) == ()


def test_aws_ec2_returns_the_secret_access_key_reference() -> None:
    (ref,) = EC2Platform.dependencies("t", {**EC2_CONFIG, "credentials": EC2_CREDS})
    assert (ref.kind, ref.name) == ("secret", "aws-secret")
    assert ref.usage == "the AWS secret access key"

    # Omitting ``access_key_secret`` falls back to the well-known default name.
    (ref,) = EC2Platform.dependencies("t", {**EC2_CONFIG, "credentials": {"access_key_id": "AKIA"}})
    assert ref.name == DEFAULT_SECRET_ACCESS_KEY


def test_aws_ec2_dependencies_is_total_on_malformed_config() -> None:
    """``dependencies`` never raises. The edge's identity is the secret NAME,
    so it emits even when the table's other fields are missing or malformed, and
    is omitted only when the table itself, or the field naming the edge, is
    unusable."""
    (ref,) = EC2Platform.dependencies("t", {"credentials": {"access_key_secret": "aws-secret"}})
    assert ref.name == "aws-secret"
    (ref,) = EC2Platform.dependencies("t", {**EC2_CONFIG, "credentials": {}})
    assert ref.name == DEFAULT_SECRET_ACCESS_KEY
    assert EC2Platform.dependencies("t", {**EC2_CONFIG, "credentials": {**EC2_CREDS, "access_key_secret": ""}}) == ()
    assert EC2Platform.dependencies("t", {**EC2_CONFIG, "credentials": "nope"}) == ()


def test_azure_returns_the_client_secret_reference() -> None:
    (ref,) = AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": AZURE_SP})
    assert (ref.kind, ref.name) == ("secret", "az-sp")
    assert "service-principal" in ref.usage

    # Omitting ``secret`` falls back to the well-known default name.
    no_name = {k: v for k, v in AZURE_SP.items() if k != "secret"}
    (ref,) = AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": no_name})
    assert ref.name == DEFAULT_CLIENT_SECRET


def test_azure_dependencies_is_total_on_malformed_config() -> None:
    """``dependencies`` never raises. The edge's identity is the secret
    NAME, so it emits even when the table's other fields are missing or
    malformed, and is omitted only when the table itself, or the field
    naming the edge, is unusable."""
    # Nothing but the secret name: the edge still emits.
    (ref,) = AzureVMPlatform.dependencies("t", {"service_principal": {"secret": "az-sp"}})
    assert ref.name == "az-sp"
    # An empty table is still a declared service principal, so the
    # default-named edge emits (validate is what rejects the shape).
    (ref,) = AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": {}})
    assert ref.name == DEFAULT_CLIENT_SECRET
    # A table whose OTHER fields are malformed still emits (their shape
    # does not change what the edge points at).
    assert AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": {**AZURE_SP, "tenant_id": 3}}) == (
        ConfigReference(kind="secret", name="az-sp", usage="the Azure service-principal client secret"),
    )
    # A malformed name, or a non-table, makes the edge underivable, so it
    # is omitted (never raised).
    assert AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": {**AZURE_SP, "secret": ""}}) == ()
    assert AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": {**AZURE_SP, "secret": 3}}) == ()
    assert AzureVMPlatform.dependencies("t", {**AZURE_CONFIG, "service_principal": "nope"}) == ()


def test_proxmox_returns_the_token_secret_reference() -> None:
    (ref,) = ProxmoxPlatform.dependencies("t", PROXMOX_CONFIG)
    assert (ref.kind, ref.name) == ("secret", DEFAULT_TOKEN_SECRET)
    assert "token" in ref.usage

    (ref,) = ProxmoxPlatform.dependencies("t", {**PROXMOX_CONFIG, "token_secret": "my-token"})
    assert ref.name == "my-token"


def test_proxmox_dependencies_is_total_on_malformed_config() -> None:
    """``dependencies`` never raises: it emits the token edge best-effort
    even when OTHER required fields are missing (their absence does not
    change the edge's identity), and omits the edge only when its own
    identity field (``token_secret``) is malformed."""
    # Every other required key missing: the token edge still emits.
    (ref,) = ProxmoxPlatform.dependencies("t", {"token_secret": "my-token"})
    assert ref.name == "my-token"
    # A malformed token_secret makes the edge's identity underivable, so
    # the edge is omitted (never raised).
    assert ProxmoxPlatform.dependencies("t", {**PROXMOX_CONFIG, "token_secret": ""}) == ()
    assert ProxmoxPlatform.dependencies("t", {**PROXMOX_CONFIG, "token_secret": 3}) == ()


def test_proxmox_dependencies_matches_valid_config_extraction() -> None:
    """For a valid blob, ``dependencies`` yields exactly the edge the old
    fused validate-and-extract method returned (the pre-refactor golden):
    one secret reference to the default token secret."""
    from agentworks.resources.reference import ConfigReference

    assert ProxmoxPlatform.dependencies("t", PROXMOX_CONFIG) == (
        ConfigReference(kind="secret", name=DEFAULT_TOKEN_SECRET, usage="the Proxmox API token"),
    )


def test_dependencies_is_pure() -> None:
    """The API runs at construct AND finalize; two calls must agree."""
    first = ProxmoxPlatform.dependencies("t", PROXMOX_CONFIG)
    second = ProxmoxPlatform.dependencies("t", PROXMOX_CONFIG)
    assert first == second


def test_legacy_platform_metadata_hooks() -> None:
    lima_row = {"name": "dev", "wsl_distro_name": None, "proxmox_vmid": None}
    assert LimaPlatform.legacy_platform_metadata(lima_row, {}) == {"instance_name": "dev"}
    wsl_row = {"name": "dev", "wsl_distro_name": "dev", "proxmox_vmid": None}
    assert WSL2Platform.legacy_platform_metadata(wsl_row, {}) == {"distro_name": "dev"}
    wsl_row_null = {"name": "dev", "wsl_distro_name": None}
    assert WSL2Platform.legacy_platform_metadata(wsl_row_null, {}) == {"distro_name": "dev"}
    az_row = {"name": "dev", "azure_resource_id": "/subscriptions/s/x"}
    assert AzureVMPlatform.legacy_platform_metadata(az_row, {}) == {"resource_id": "/subscriptions/s/x"}
    az_row_null = {"name": "dev", "azure_resource_id": None}
    assert AzureVMPlatform.legacy_platform_metadata(az_row_null, {}) == {}
    # ec2 ships after the DB migration, so it has no legacy rows to map: the
    # base no-op return of {} is correct regardless of the row's contents.
    assert EC2Platform.legacy_platform_metadata({"name": "dev", "azure_resource_id": "/x"}, {}) == {}
    px_row = {"name": "dev", "proxmox_vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {}) == {"vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {"proxmox": {"node": "pve1"}}) == {
        "vmid": "104",
        "node": "pve1",
    }
