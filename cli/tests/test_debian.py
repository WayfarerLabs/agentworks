from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.debian import (
    BOOKWORM_TO_TRIXIE,
    CURRENT_DEBIAN_RELEASE,
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianSupport,
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


def test_release_registry_requires_the_adjacent_policy() -> None:
    with pytest.raises(ValueError):
        validate_release_profiles(
            (
                DebianReleaseProfile(DebianRelease.BOOKWORM, "12"),
                DebianReleaseProfile(DebianRelease.TRIXIE, "13"),
            )
        )

    assert BOOKWORM_TO_TRIXIE.source is DebianRelease.BOOKWORM
    assert BOOKWORM_TO_TRIXIE.target is DebianRelease.TRIXIE


def test_probe_validates_the_expected_release() -> None:
    class FakeTransport:
        def run(self, _command: str) -> SimpleNamespace:
            return SimpleNamespace(stdout="ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")

    assert probe_debian_release(FakeTransport(), expected=DebianRelease.TRIXIE) is DebianRelease.TRIXIE  # type: ignore[arg-type]

    with pytest.raises(StateError) as caught:
        probe_debian_release(FakeTransport(), expected=DebianRelease.BOOKWORM)  # type: ignore[arg-type]
    assert caught.value.entity_kind == "debian-release"
