"""Shared vm-platform v2 provider-locator boundary invariants."""

from __future__ import annotations

import pytest

from agentworks.capabilities.vm_platform.base import (
    MAX_PROVIDER_LOCATOR_BYTES,
    ProviderLocator,
    ProviderLocatorUnavailable,
    provider_locator_remaining,
)
from agentworks.errors import LimitExceededError, ValidationError
from agentworks.execution.carrier import Deadline


def test_provider_locator_preserves_opaque_token_exactly() -> None:
    token = "provider://scope/instance?opaque=%20"

    assert ProviderLocator(token).token == token


@pytest.mark.parametrize(
    "token",
    ("", "contains\0nul", b"bytes", 1, None),
)
def test_provider_locator_rejects_invalid_boundary_tokens(token: object) -> None:
    with pytest.raises(ValidationError):
        ProviderLocator(token)  # type: ignore[arg-type]


def test_provider_locator_rejects_string_subclasses() -> None:
    class PluginString(str):
        pass

    with pytest.raises(ValidationError):
        ProviderLocator(PluginString("opaque"))


def test_provider_locator_rejects_utf8_unencodable_token() -> None:
    with pytest.raises(ValidationError):
        ProviderLocator("\ud800")


def test_provider_locator_bounds_utf8_bytes_not_characters() -> None:
    assert ProviderLocator("a" * MAX_PROVIDER_LOCATOR_BYTES).token == "a" * MAX_PROVIDER_LOCATOR_BYTES
    with pytest.raises(ValidationError):
        ProviderLocator("a" * (MAX_PROVIDER_LOCATOR_BYTES + 1))
    with pytest.raises(ValidationError):
        ProviderLocator("é" * ((MAX_PROVIDER_LOCATOR_BYTES // 2) + 1))


def test_provider_locator_unavailable_is_fieldless() -> None:
    assert ProviderLocatorUnavailable().__dict__ == {}


def test_provider_locator_remaining_returns_positive_finite_budget() -> None:
    assert provider_locator_remaining(Deadline.after(10), vm_name="test-vm") > 0


@pytest.mark.parametrize("deadline", (Deadline.after(None), Deadline.after(0), object()))
def test_provider_locator_remaining_rejects_unbounded_expired_or_wrong_deadline(deadline: object) -> None:
    expected = LimitExceededError if type(deadline) is Deadline and deadline.expired else ValidationError
    with pytest.raises(expected):
        provider_locator_remaining(deadline, vm_name="test-vm")  # type: ignore[arg-type]
