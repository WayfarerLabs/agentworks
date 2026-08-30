"""Contract-v3 Debian release selection at the VM-platform boundary."""

from __future__ import annotations

from typing import cast

import pytest

from agentworks.capabilities.vm_platform import debian_release as release_boundary
from agentworks.capabilities.vm_platform.debian_release import (
    code_owned_release_value,
    operator_owned_release_value,
    verify_provisioned_release,
)
from agentworks.capabilities.vm_platform.lima import _LIMA_IMAGE_BLOCKS, LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import _DEBIAN_OCI_TAGS, WSL2Platform
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.aws.platform import _DEBIAN_SSM_RELEASES, EC2Platform
from agentworks.plugins.azure.config import AZURE_IMAGES
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp.config import IMAGE_FAMILIES
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.plugins.proxmox.platform import ProxmoxConfig, ProxmoxPlatform
from agentworks.transports import Transport


def test_all_platforms_declare_contract_version_three() -> None:
    assert {
        LimaPlatform.contract_version,
        WSL2Platform.contract_version,
        EC2Platform.contract_version,
        AzureVMPlatform.contract_version,
        GCEPlatform.contract_version,
        ProxmoxPlatform.contract_version,
    } == {3}


def test_every_code_owned_platform_map_has_the_trixie_selector() -> None:
    assert "debian-13-generic-amd64.qcow2" in _LIMA_IMAGE_BLOCKS[DebianRelease.TRIXIE]
    assert "debian-13-generic-arm64.qcow2" in _LIMA_IMAGE_BLOCKS[DebianRelease.TRIXIE]
    assert _DEBIAN_OCI_TAGS[DebianRelease.TRIXIE] == "trixie"
    assert _DEBIAN_SSM_RELEASES[DebianRelease.TRIXIE] == "13"
    assert AZURE_IMAGES[DebianRelease.TRIXIE].urn == "Debian:debian-13:13-gen2:latest"
    assert IMAGE_FAMILIES[DebianRelease.TRIXIE] == {
        "x86_64": "debian-13",
        "arm64": "debian-13-arm64",
    }


def test_code_owned_map_miss_names_the_outdated_platform() -> None:
    with pytest.raises(StateError) as caught:
        code_owned_release_value({}, DebianRelease.TRIXIE, platform_name="example-cloud")

    assert caught.value.entity_kind == "vm-platform"
    assert caught.value.entity_name == "example-cloud"
    assert caught.value.hint is not None and "plugin" in caught.value.hint


def test_operator_owned_map_miss_names_the_exact_site_key() -> None:
    with pytest.raises(ConfigError) as caught:
        operator_owned_release_value(
            {},
            DebianRelease.TRIXIE,
            site_name="lab",
            field="template_vmids",
        )

    assert caught.value.entity_kind == "vm-site"
    assert caught.value.entity_name == "lab"
    assert caught.value.hint is not None and "template_vmids.trixie" in caught.value.hint


def test_legacy_proxmox_scalar_populates_only_bookworm() -> None:
    config = ProxmoxConfig.model_validate(
        {
            "name": "proxmox",
            "api_url": "https://pve.example.test:8006",
            "node": "pve1",
            "token_id": "agentworks@pam!agw",
            "token_secret": "proxmox-token",
            "template_vmid": 9000,
        }
    )

    assert config.template_vmids == {DebianRelease.BOOKWORM: 9000}
    with pytest.raises(ConfigError):
        operator_owned_release_value(
            config.template_vmids,
            DebianRelease.TRIXIE,
            site_name="lab",
            field="template_vmids",
        )


def test_shared_verifier_passes_the_expected_release_to_the_core_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, DebianRelease | None]] = []
    transport = cast("Transport", object())

    def _probe(candidate: object, expected: DebianRelease | None = None) -> DebianRelease:
        seen.append((candidate, expected))
        return DebianRelease.TRIXIE

    monkeypatch.setattr(release_boundary, "probe_debian_release", _probe)

    assert verify_provisioned_release(transport, DebianRelease.TRIXIE) is DebianRelease.TRIXIE
    assert seen == [(transport, DebianRelease.TRIXIE)]
