"""Tests for ``ResourceRequirement``, ``SecretRequirement``, ``UsageEntry``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentworks.resources import (
    ResourceRequirement,
    SecretRequirement,
    UsageEntry,
)


def test_resource_requirement_fields() -> None:
    req = ResourceRequirement(
        name="tailscale-auth-key",
        kind="secret",
        usage="the Tailscale auth key",
        source=("vm_template", "default"),
    )
    assert req.name == "tailscale-auth-key"
    assert req.kind == "secret"
    assert req.usage == "the Tailscale auth key"
    assert req.source == ("vm_template", "default")


def test_resource_requirement_is_immutable() -> None:
    req = ResourceRequirement(name="x", kind="secret", usage="u", source=("k", "n"))
    with pytest.raises(FrozenInstanceError):
        req.name = "y"  # type: ignore[misc]


def test_secret_requirement_is_a_resource_requirement() -> None:
    sec = SecretRequirement(
        name="api-key",
        kind="secret",
        usage="the API key",
        source=("admin_template", "default"),
    )
    assert isinstance(sec, ResourceRequirement)
    assert sec.kind == "secret"


def test_usage_entry_fields() -> None:
    entry = UsageEntry(source=("vm_template", "default"), text="the auth key")
    assert entry.source == ("vm_template", "default")
    assert entry.text == "the auth key"


def test_usage_entry_is_immutable() -> None:
    entry = UsageEntry(source=("k", "n"), text="t")
    with pytest.raises(FrozenInstanceError):
        entry.text = "new"  # type: ignore[misc]


def test_usage_entry_equality_and_hashability() -> None:
    a = UsageEntry(source=("k", "n"), text="t")
    b = UsageEntry(source=("k", "n"), text="t")
    c = UsageEntry(source=("k", "n"), text="other")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
