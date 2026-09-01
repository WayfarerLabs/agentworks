"""Debian release selection at the VM-platform boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.debian import DebianRelease


def code_owned_release_value[T](
    values: Mapping[DebianRelease, T],
    release: DebianRelease,
    *,
    platform_name: str,
) -> T:
    """Resolve a platform-owned selector supplied by this build.

    A missing bundled selector means this Agentworks build is internally
    incomplete, so fail with platform-specific context before backend mutation.
    """
    try:
        return values[release]
    except KeyError:
        raise StateError(
            f"vm-platform/{platform_name} cannot create Debian {release.value}: "
            "its release map has no matching artifact",
            entity_kind="vm-platform",
            entity_name=platform_name,
            hint=(f"Update Agentworks to a build where vm-platform/{platform_name} supports Debian {release.value}."),
        ) from None


def operator_owned_release_value[T](
    values: Mapping[DebianRelease, T],
    release: DebianRelease,
    *,
    site_name: str,
    field: str,
) -> T:
    """Resolve a release value supplied by one operator-authored VM site."""
    try:
        return values[release]
    except KeyError:
        key = f"{field}.{release.value}"
        raise ConfigError(
            f"vm-site/{site_name} cannot create Debian {release.value}: {key} is not configured",
            entity_kind="vm-site",
            entity_name=site_name,
            hint=f"Set {key} in vm-site/{site_name}'s platform configuration.",
        ) from None
