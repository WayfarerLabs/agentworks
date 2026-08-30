"""Debian release selection at the VM-platform boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.debian import DebianRelease, probe_debian_release
from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.transports import Transport


def code_owned_release_value[T](
    values: Mapping[DebianRelease, T],
    release: DebianRelease,
    *,
    platform_name: str,
) -> T:
    """Resolve a platform-owned selector supplied by this build.

    ``ProvisionRequest`` is a capability boundary that plugins can call, so
    the requested release is validated even though core supplies a typed value.
    """
    try:
        return values[release]
    except KeyError:
        raise StateError(
            f"vm-platform/{platform_name} cannot create Debian {release.value}: "
            "its release map has no matching artifact",
            entity_kind="vm-platform",
            entity_name=platform_name,
            hint=f"Update Agentworks or the plugin that provides vm-platform/{platform_name}.",
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


def verify_provisioned_release(transport: Transport, expected: DebianRelease) -> DebianRelease:
    """Verify a newly provisioned guest before its rollback window closes."""
    return probe_debian_release(transport, expected=expected)
