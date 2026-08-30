from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.debian import (
    BOOKWORM_TO_TRIXIE,
    CURRENT_DEBIAN_RELEASE,
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianSupport,
    DebianUpgradePolicy,
    classify_release,
    parse_os_release,
    probe_debian_release,
    validate_release_profiles,
)
from agentworks.errors import StateError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n', DebianRelease.BOOKWORM),
        ('VERSION_CODENAME="trixie"\nVERSION_ID=13\nID="debian"\n', DebianRelease.TRIXIE),
    ],
)
def test_parse_os_release_recognizes_registered_pairs(text: str, expected: DebianRelease) -> None:
    assert parse_os_release(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble\n",
        "ID=debian\nVERSION_ID=13\nVERSION_CODENAME=bookworm\n",
        "ID=debian\nVERSION_ID=14\nVERSION_CODENAME=forky\n",
        "ID=debian\nVERSION_ID=13\n",
    ],
)
def test_parse_os_release_rejects_unrecognized_observations(text: str) -> None:
    with pytest.raises(StateError) as caught:
        parse_os_release(text)

    assert caught.value.entity_kind == "debian-release"


def test_current_and_support_derive_from_registry_order() -> None:
    assert CURRENT_DEBIAN_RELEASE is DEBIAN_RELEASES[-1].release
    assert classify_release(DebianRelease.TRIXIE) is DebianSupport.CURRENT
    assert classify_release(DebianRelease.BOOKWORM) is DebianSupport.PREVIOUS


def test_appending_a_candidate_profile_reclassifies_older_releases() -> None:
    class CandidateRelease(StrEnum):
        FORKY = "forky"

    forky = cast("DebianRelease", CandidateRelease.FORKY)
    candidate_policy = DebianUpgradePolicy(
        source_suites=("trixie",),
        target_suites=("forky",),
        minimum_openssh_version="candidate",
        documentation_urls=("https://www.debian.org/releases/forky/",),
    )
    candidate_profiles = validate_release_profiles(
        (
            *DEBIAN_RELEASES,
            DebianReleaseProfile(forky, "14", upgrade_from_previous=candidate_policy),
        )
    )

    assert classify_release(forky, candidate_profiles) is DebianSupport.CURRENT
    assert classify_release(DebianRelease.TRIXIE, candidate_profiles) is DebianSupport.PREVIOUS
    assert classify_release(DebianRelease.BOOKWORM, candidate_profiles) is DebianSupport.LEGACY


def test_release_registry_requires_the_adjacent_policy() -> None:
    with pytest.raises(ValueError):
        validate_release_profiles(
            (
                DebianReleaseProfile(DebianRelease.BOOKWORM, "12"),
                DebianReleaseProfile(DebianRelease.TRIXIE, "13"),
            )
        )

    assert BOOKWORM_TO_TRIXIE.source_suites[0] == "bookworm"
    assert BOOKWORM_TO_TRIXIE.target_suites[0] == "trixie"


def test_probe_validates_the_expected_release() -> None:
    class FakeTransport:
        def run(self, _command: str) -> SimpleNamespace:
            return SimpleNamespace(stdout="ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")

    assert probe_debian_release(FakeTransport(), expected=DebianRelease.TRIXIE) is DebianRelease.TRIXIE  # type: ignore[arg-type]

    with pytest.raises(StateError) as caught:
        probe_debian_release(FakeTransport(), expected=DebianRelease.BOOKWORM)  # type: ignore[arg-type]
    assert caught.value.entity_kind == "debian-release"
