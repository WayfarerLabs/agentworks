"""Tests for ``ResourceReference``, ``SecretReference``, ``ReferenceEntry``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentworks.resources import (
    ReferenceEntry,
    ResourceReference,
    SecretReference,
)


def test_resource_reference_fields() -> None:
    ref = ResourceReference(
        name="tailscale-auth-key",
        kind="secret",
        usage="the Tailscale auth key",
        source=("vm_template", "default"),
    )
    assert ref.name == "tailscale-auth-key"
    assert ref.kind == "secret"
    assert ref.usage == "the Tailscale auth key"
    assert ref.source == ("vm_template", "default")


def test_resource_reference_is_immutable() -> None:
    ref = ResourceReference(name="x", kind="secret", usage="u", source=("k", "n"))
    with pytest.raises(FrozenInstanceError):
        ref.name = "y"  # type: ignore[misc]


def test_secret_reference_is_a_resource_reference() -> None:
    sec = SecretReference(
        name="api-key",
        kind="secret",
        usage="the API key",
        source=("admin_template", "default"),
    )
    assert isinstance(sec, ResourceReference)
    assert sec.kind == "secret"


def test_reference_entry_fields() -> None:
    entry = ReferenceEntry(source=("vm_template", "default"), usage="the auth key")
    assert entry.source == ("vm_template", "default")
    assert entry.usage == "the auth key"


def test_reference_entry_is_immutable() -> None:
    entry = ReferenceEntry(source=("k", "n"), usage="t")
    with pytest.raises(FrozenInstanceError):
        entry.usage = "new"  # type: ignore[misc]


def test_reference_entry_equality_and_hashability() -> None:
    a = ReferenceEntry(source=("k", "n"), usage="t")
    b = ReferenceEntry(source=("k", "n"), usage="t")
    c = ReferenceEntry(source=("k", "n"), usage="other")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
