from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.db import Database
from agentworks.debian import (
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    DebianUpgradePolicy,
    classify_release,
)
from agentworks.errors import StateError
from agentworks.vms.manager.lifecycle import _warn_newly_observed_legacy
from agentworks.vms.manager.release import verified_vm_release
from tests.conftest import CapturedOutput


class ReleaseTransport:
    def __init__(self, release: DebianRelease) -> None:
        version = "12" if release is DebianRelease.BOOKWORM else "13"
        self.result = SimpleNamespace(stdout=f"ID=debian\nVERSION_ID={version}\nVERSION_CODENAME={release.value}\n")

    def run(self, _command: str) -> SimpleNamespace:
        return self.result


def test_verified_vm_release_fills_an_unknown_observation(db: Database) -> None:
    vm = db.insert_vm("box", site="lima-local", hostname="box")

    assert (
        verified_vm_release(db, vm, ReleaseTransport(DebianRelease.TRIXIE))  # type: ignore[arg-type]
        is DebianRelease.TRIXIE
    )
    observed = db.get_vm("box")
    assert observed is not None
    assert observed.debian_release is DebianRelease.TRIXIE
    assert observed.debian_release_observed_at is not None


def test_verified_vm_release_refuses_recorded_live_drift(db: Database) -> None:
    vm = db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release(vm.name, DebianRelease.BOOKWORM)
    recorded = db.get_vm(vm.name)
    assert recorded is not None

    with pytest.raises(StateError) as caught:
        verified_vm_release(db, recorded, ReleaseTransport(DebianRelease.TRIXIE))  # type: ignore[arg-type]

    assert caught.value.entity_kind == "vm"
    unchanged = db.get_vm(vm.name)
    assert unchanged is not None
    assert unchanged.debian_release is DebianRelease.BOOKWORM


def test_newly_observed_future_legacy_release_warns_once(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    class CandidateRelease(StrEnum):
        FORKY = "forky"

    forky = cast("DebianRelease", CandidateRelease.FORKY)
    profiles = (
        *DEBIAN_RELEASES,
        DebianReleaseProfile(
            forky,
            "14",
            DebianUpgradePolicy(
                source_suites=("trixie",),
                target_suites=("forky",),
                minimum_openssh_version="candidate",
                documentation_urls=(),
            ),
        ),
    )
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.classify_release",
        lambda release: classify_release(release, profiles),
    )
    vm = db.insert_vm("box", site="lima-local", hostname="box")

    _warn_newly_observed_legacy(vm, DebianRelease.BOOKWORM)
    _warn_newly_observed_legacy(
        replace(vm, debian_release=DebianRelease.BOOKWORM),
        DebianRelease.BOOKWORM,
    )

    assert len(captured_output.warnings) == 1
