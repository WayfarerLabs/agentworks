"""Debian release facts shared by VM creation, observation, and upgrade.

The ordered profile tuple is the sole definition of Agentworks' current
Debian release.  Platform selectors and apt resources keep their own mapped
values, but use these typed release keys rather than inventing local notions
of "current".
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.transports import Transport


class DebianRelease(StrEnum):
    """Debian codenames recognized by this Agentworks build."""

    BOOKWORM = "bookworm"
    TRIXIE = "trixie"


class DebianSupport(StrEnum):
    """A recognized release's position relative to the current profile."""

    CURRENT = "current"
    PREVIOUS = "previous"
    LEGACY = "legacy"


@dataclass(frozen=True)
class DebianUpgradePolicy:
    """The transition facts owned by one target release profile."""

    source: DebianRelease
    target: DebianRelease
    source_suites: tuple[str, ...]
    target_suites: tuple[str, ...]
    minimum_openssh_version: str
    documentation_urls: tuple[str, ...]
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class DebianReleaseProfile:
    """One recognized release and its adjacent upgrade policy, if any."""

    release: DebianRelease
    version_id: str
    upgrade_from_previous: DebianUpgradePolicy | None = None


BOOKWORM_TO_TRIXIE = DebianUpgradePolicy(
    source=DebianRelease.BOOKWORM,
    target=DebianRelease.TRIXIE,
    source_suites=("bookworm", "bookworm-updates", "bookworm-security"),
    target_suites=("trixie", "trixie-updates", "trixie-security"),
    minimum_openssh_version="1:9.2p1-2+deb12u7",
    documentation_urls=(
        "https://www.debian.org/releases/trixie/amd64/release-notes/ch-upgrading.en.html",
        "https://www.debian.org/releases/trixie/amd64/release-notes/ch-information.en.html",
    ),
    blockers=("mariadb-unsafe-shutdown", "rabbitmq-server-installed"),
)


def validate_release_profiles(
    profiles: Sequence[DebianReleaseProfile],
) -> tuple[DebianReleaseProfile, ...]:
    """Validate and freeze an ordered Debian release registry."""

    frozen = tuple(profiles)
    if not frozen:
        raise ValueError("the Debian release registry must not be empty")

    releases = [profile.release for profile in frozen]
    versions = [profile.version_id for profile in frozen]
    if len(set(releases)) != len(releases):
        raise ValueError("the Debian release registry contains a duplicate codename")
    if len(set(versions)) != len(versions):
        raise ValueError("the Debian release registry contains a duplicate VERSION_ID")

    if frozen[0].upgrade_from_previous is not None:
        raise ValueError("the first Debian release profile cannot have an upgrade policy")
    for previous, profile in zip(frozen, frozen[1:], strict=False):
        policy = profile.upgrade_from_previous
        if policy is None:
            raise ValueError(f"Debian release {profile.release} requires an adjacent upgrade policy")
        if policy.source is not previous.release or policy.target is not profile.release:
            raise ValueError(
                f"Debian release {profile.release} upgrade policy must describe {previous.release} to {profile.release}"
            )
    return frozen


DEBIAN_RELEASES = validate_release_profiles(
    (
        DebianReleaseProfile(
            release=DebianRelease.BOOKWORM,
            version_id="12",
        ),
        DebianReleaseProfile(
            release=DebianRelease.TRIXIE,
            version_id="13",
            upgrade_from_previous=BOOKWORM_TO_TRIXIE,
        ),
    )
)

CURRENT_DEBIAN_RELEASE = DEBIAN_RELEASES[-1].release


def profile_for_release(
    release: DebianRelease,
    profiles: Sequence[DebianReleaseProfile] = DEBIAN_RELEASES,
) -> DebianReleaseProfile:
    """Return the registered profile for ``release``."""

    for profile in profiles:
        if profile.release is release:
            return profile
    raise StateError(
        f"Debian release '{release}' is not supported by this Agentworks build",
        entity_kind="debian-release",
        entity_name=str(release),
        hint="Upgrade Agentworks to a build that supports this Debian release.",
    )


def classify_release(
    release: DebianRelease,
    profiles: Sequence[DebianReleaseProfile] = DEBIAN_RELEASES,
) -> DebianSupport:
    """Classify ``release`` from its position in the ordered registry."""

    registered = tuple(profiles)
    profile_for_release(release, registered)
    position = next(index for index, profile in enumerate(registered) if profile.release is release)
    distance = len(registered) - position - 1
    if distance == 0:
        return DebianSupport.CURRENT
    if distance == 1:
        return DebianSupport.PREVIOUS
    return DebianSupport.LEGACY


def parse_os_release(
    text: str,
    profiles: Sequence[DebianReleaseProfile] = DEBIAN_RELEASES,
) -> DebianRelease:
    """Parse a recognized Debian codename/version pair from os-release."""

    fields: dict[str, str] = {}
    wanted = {"ID", "VERSION_ID", "VERSION_CODENAME"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key not in wanted:
            continue
        try:
            values = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise _observation_error(fields | {key: raw_value}, "contains invalid quoting") from exc
        fields[key] = values[0] if len(values) == 1 else raw_value

    missing = sorted(wanted - fields.keys())
    if missing:
        raise _observation_error(fields, f"is missing {', '.join(missing)}")
    if fields["ID"] != "debian":
        raise _observation_error(fields, "does not identify Debian")

    for profile in profiles:
        if fields["VERSION_CODENAME"] == profile.release.value and fields["VERSION_ID"] == profile.version_id:
            return profile.release
    raise _observation_error(fields, "is not recognized by this Agentworks build")


# Kept as a discoverable spelling for platform implementations while the
# os-release parser remains the canonical public name.
parse_debian_release = parse_os_release


def probe_debian_release(
    transport: Transport,
    *,
    expected: DebianRelease | None = None,
) -> DebianRelease:
    """Read and validate a VM's live Debian release through its transport."""

    observed = parse_os_release(transport.run("cat /etc/os-release").stdout)
    if expected is not None and observed is not expected:
        raise StateError(
            f"VM reports Debian {observed}, expected {expected}",
            entity_kind="debian-release",
            entity_name=str(observed),
            hint="Use the platform image or template mapped to the requested Debian release.",
        )
    return observed


def _observation_error(fields: dict[str, str], reason: str) -> StateError:
    observed = ", ".join(f"{key}={fields.get(key, '<missing>')}" for key in ("ID", "VERSION_ID", "VERSION_CODENAME"))
    return StateError(
        f"guest os-release {reason}: {observed}",
        entity_kind="debian-release",
        hint="Upgrade Agentworks if the guest runs a newer Debian release.",
    )
