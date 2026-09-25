"""Private managed VM target identity codec and composition invariants."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.db import VMRow
from agentworks.errors import StateError, ValidationError
from agentworks.execution._managed_runs import ManagedTargetKind
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.vms.target_identity import (
    compose_managed_vm_target_identity,
    vm_guest_boot_id,
    vm_incarnation_fingerprint,
)

_MARKER = "0123456789abcdef0123456789abcdef"
_OTHER_MARKER = "fedcba9876543210fedcba9876543210"
_BOOT_ID = "00000000-0000-4000-8000-000000000001"
_INIT_START_TICKS = 1234


def _vm(*, marker: str | None = _MARKER) -> VMRow:
    return VMRow(
        name="box",
        site="local",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="ready",
        init_status="ready",
        tailscale_host=None,
        cpus=None,
        memory_gib=None,
        disk_gib=None,
        swap_gib=None,
        admin_username="admin",
        hostname="box",
        created_at="2026-09-21T00:00:00Z",
        last_seen_at=None,
        instance_marker=marker,
    )


def _guest(
    marker: str = _MARKER,
    boot_id: str = _BOOT_ID,
    init_start_ticks: int = _INIT_START_TICKS,
) -> VMGuestIdentity:
    return VMGuestIdentity(instance_marker=marker, boot_id=boot_id, init_start_ticks=init_start_ticks)


def test_fingerprint_uses_stable_version_one_utf8_length_framing() -> None:
    assert vm_incarnation_fingerprint(ProviderLocator("provider://é"), _MARKER) == (
        "v1:aef86b5efcc818c44eb0b9ea90c5234dc6c56e37ff0f5452ccea6a522a9d49cc"
    )


def test_fingerprint_is_sensitive_to_locator_and_marker() -> None:
    base = vm_incarnation_fingerprint(ProviderLocator("provider://box"), _MARKER)

    assert vm_incarnation_fingerprint(ProviderLocator("provider://other"), _MARKER) != base
    assert vm_incarnation_fingerprint(ProviderLocator("provider://box"), _OTHER_MARKER) != base


@pytest.mark.parametrize("marker", (None, "", "A" * 32, "g" * 32, 3))
def test_fingerprint_rejects_malformed_marker(marker: object) -> None:
    with pytest.raises(ValidationError):
        vm_incarnation_fingerprint(ProviderLocator("opaque"), marker)  # type: ignore[arg-type]


def test_composition_rejects_unavailable_locator_without_name_fallback() -> None:
    with pytest.raises(StateError) as raised:
        compose_managed_vm_target_identity(_vm(), ProviderLocatorUnavailable(), _guest())

    assert raised.value.entity_kind == "vm"
    assert raised.value.entity_name == "box"


def test_composition_rejects_legacy_null_marker_without_mutation() -> None:
    vm = _vm(marker=None)

    with pytest.raises(StateError) as raised:
        compose_managed_vm_target_identity(vm, ProviderLocator("opaque"), _guest())

    assert raised.value.entity_kind == "vm"
    assert raised.value.entity_name == vm.name
    assert vm.instance_marker is None


def test_composition_rejects_mismatched_guest_marker() -> None:
    vm = _vm()
    before = replace(vm)

    with pytest.raises(StateError) as raised:
        compose_managed_vm_target_identity(vm, ProviderLocator("opaque"), _guest(_OTHER_MARKER))

    assert raised.value.entity_kind == "vm"
    assert raised.value.entity_name == vm.name
    assert vm == before


def test_composition_returns_managed_identity_and_keeps_boot_outside_fingerprint() -> None:
    identity = compose_managed_vm_target_identity(_vm(), ProviderLocator("opaque"), _guest())
    other_boot = compose_managed_vm_target_identity(
        _vm(),
        ProviderLocator("opaque"),
        _guest(boot_id="00000000-0000-4000-8000-000000000002"),
    )
    restarted_distribution = compose_managed_vm_target_identity(
        _vm(), ProviderLocator("opaque"), _guest(init_start_ticks=_INIT_START_TICKS + 1)
    )

    assert identity.kind is ManagedTargetKind.VM
    assert identity.name == "box"
    assert identity.incarnation == vm_incarnation_fingerprint(ProviderLocator("opaque"), _MARKER)
    assert identity.boot_id == vm_guest_boot_id(_guest())
    assert identity.boot_id != _BOOT_ID
    assert other_boot.incarnation == identity.incarnation
    assert other_boot.boot_id != identity.boot_id
    assert restarted_distribution.incarnation == identity.incarnation
    assert restarted_distribution.boot_id != identity.boot_id


def test_composition_rejects_wrong_guest_shape() -> None:
    with pytest.raises(ValidationError):
        compose_managed_vm_target_identity(_vm(), ProviderLocator("opaque"), object())  # type: ignore[arg-type]


def test_guest_protocol_rejects_noncanonical_boot_id() -> None:
    with pytest.raises(ValueError):
        VMGuestIdentity(instance_marker=_MARKER, boot_id="not-a-uuid", init_start_ticks=_INIT_START_TICKS)


def test_composition_does_not_change_other_vm_fields() -> None:
    vm = _vm()
    before = replace(vm)
    compose_managed_vm_target_identity(vm, ProviderLocator("opaque"), _guest())
    assert vm == before
