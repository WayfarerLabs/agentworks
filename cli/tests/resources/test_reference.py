"""Tests for ``ResourceReference``, ``SecretReference``, ``ReferenceEntry``.

What these types are is a frozen dataclass, so what is pinned here is what
the dataclass decorator does NOT give us for free: the subclass relation,
and the freezing. Field round-trips and the generated ``__eq__`` /
``__hash__`` are the decorator's own behavior; that the field NAMES are the
post-rename ones is pinned in ``test_phase3_naming_consistency.py``, and
every finalize test reads them for real.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentworks.resources import (
    ReferenceEntry,
    ResourceReference,
    SecretReference,
)


def test_resource_reference_is_immutable() -> None:
    ref = ResourceReference(name="x", kind="secret", usage="u", source=("k", "n"))
    with pytest.raises(FrozenInstanceError):
        ref.name = "y"  # type: ignore[misc]


def test_secret_reference_is_a_resource_reference() -> None:
    sec = SecretReference(
        name="api-key",
        kind="secret",
        usage="the API key",
        source=("admin-template", "default"),
    )
    assert isinstance(sec, ResourceReference)
    assert sec.kind == "secret"


def test_reference_entry_is_immutable() -> None:
    entry = ReferenceEntry(source=("k", "n"), usage="t")
    with pytest.raises(FrozenInstanceError):
        entry.usage = "new"  # type: ignore[misc]
