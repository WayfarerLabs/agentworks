"""The shipped invoked-validation API on the four VM platforms:
``validate_config(owner, config)`` validates the platform_config blob
and returns the ConfigReference tuple it implies.
"""

from __future__ import annotations

import pytest

from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
from agentworks.capabilities.vm_platform.azure_vm import AzureVMPlatform
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.proxmox import (
    DEFAULT_TOKEN_SECRET,
    ProxmoxPlatform,
)
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import ConfigError

AZURE_CONFIG = {
    "subscription_id": "0000",
    "resource_group": "agw",
    "region": "eastus",
}
PROXMOX_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "pve1",
    "token_id": "agw@pam!agw",
    "template_vmid": 9000,
}


def test_registry_names_match_classes() -> None:
    assert {
        "lima": LimaPlatform,
        "wsl2": WSL2Platform,
        "azure-vm": AzureVMPlatform,
        "proxmox": ProxmoxPlatform,
    } == VM_PLATFORM_REGISTRY
    for name, cls in VM_PLATFORM_REGISTRY.items():
        assert cls.name == name
        assert cls.description


def test_lima_accepts_empty_and_vm_host() -> None:
    assert LimaPlatform.validate_config("t", {}) == ()
    assert LimaPlatform.validate_config("t", {"vm_host": "me@box"}) == ()


def test_lima_rejects_bad_vm_host_and_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="vm_host"):
        LimaPlatform.validate_config("t", {"vm_host": ""})
    with pytest.raises(ConfigError, match="unknown lima"):
        LimaPlatform.validate_config("t", {"host": "x"})


def test_wsl2_accepts_no_configuration() -> None:
    assert WSL2Platform.validate_config("t", {}) == ()
    with pytest.raises(ConfigError, match="accepts no configuration"):
        WSL2Platform.validate_config("t", {"anything": 1})


def test_azure_requires_the_three_keys() -> None:
    assert AzureVMPlatform.validate_config("t", AZURE_CONFIG) == ()
    for missing in AZURE_CONFIG:
        broken = {k: v for k, v in AZURE_CONFIG.items() if k != missing}
        with pytest.raises(ConfigError, match=missing):
            AzureVMPlatform.validate_config("t", broken)
    with pytest.raises(ConfigError, match="unknown azure"):
        AzureVMPlatform.validate_config("t", {**AZURE_CONFIG, "extra": "x"})


def test_proxmox_returns_the_token_secret_reference() -> None:
    (ref,) = ProxmoxPlatform.validate_config("t", PROXMOX_CONFIG)
    assert (ref.kind, ref.name) == ("secret", DEFAULT_TOKEN_SECRET)
    assert "token" in ref.usage

    (ref,) = ProxmoxPlatform.validate_config("t", {**PROXMOX_CONFIG, "token_secret": "my-token"})
    assert ref.name == "my-token"


def test_proxmox_validation_errors() -> None:
    with pytest.raises(ConfigError, match="node is required"):
        ProxmoxPlatform.validate_config("t", {k: v for k, v in PROXMOX_CONFIG.items() if k != "node"})
    with pytest.raises(ConfigError, match="template_vmid must be an integer"):
        ProxmoxPlatform.validate_config("t", {**PROXMOX_CONFIG, "template_vmid": "not-a-number"})
    with pytest.raises(ConfigError, match="token_secret must be a bare secret"):
        ProxmoxPlatform.validate_config("t", {**PROXMOX_CONFIG, "token_secret": ""})
    with pytest.raises(ConfigError, match="unknown proxmox"):
        ProxmoxPlatform.validate_config("t", {**PROXMOX_CONFIG, "nodee": "x"})


def test_validate_config_is_pure() -> None:
    """The API runs at decode AND finalize; two calls must agree."""
    first = ProxmoxPlatform.validate_config("t", PROXMOX_CONFIG)
    second = ProxmoxPlatform.validate_config("t", PROXMOX_CONFIG)
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
    px_row = {"name": "dev", "proxmox_vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {}) == {"vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {"proxmox": {"node": "pve1"}}) == {
        "vmid": "104",
        "node": "pve1",
    }
