from __future__ import annotations

from enum import StrEnum
from typing import cast

import pytest

from agentworks.debian import (
    CURRENT_DEBIAN_RELEASE,
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianSupport,
    classify_release,
    parse_os_release,
    validate_release_profiles,
    verify_os_release,
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
    candidate_profiles = validate_release_profiles(
        (
            *DEBIAN_RELEASES,
            DebianReleaseProfile(forky, "14"),
        )
    )

    assert classify_release(forky, candidate_profiles) is DebianSupport.CURRENT
    assert classify_release(DebianRelease.TRIXIE, candidate_profiles) is DebianSupport.PREVIOUS
    assert classify_release(DebianRelease.BOOKWORM, candidate_profiles) is DebianSupport.LEGACY


def test_verify_os_release_validates_the_expected_release() -> None:
    text = "ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n"

    assert verify_os_release(text, expected=DebianRelease.TRIXIE) is DebianRelease.TRIXIE

    with pytest.raises(StateError) as caught:
        verify_os_release(text, expected=DebianRelease.BOOKWORM)
    assert caught.value.entity_kind == "debian-release"


def test_a_guest_value_reaches_the_error_only_as_a_plain_token() -> None:
    hostile = "\x1b]0;owned\x07 $(id) " + "x" * 100
    text = f'ID=debian\nVERSION_ID="{hostile}"\nVERSION_CODENAME=trixie\n'

    with pytest.raises(StateError) as caught:
        parse_os_release(text)

    assert hostile not in str(caught.value)
    assert "\x1b" not in str(caught.value)
    assert "ID=debian" in str(caught.value)
