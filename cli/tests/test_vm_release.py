from __future__ import annotations

import contextlib
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import Database, InitStatus
from agentworks.debian import (
    DEBIAN_RELEASES,
    DebianRelease,
    DebianReleaseProfile,
    classify_release,
)
from agentworks.errors import StateError, UserAbort
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms.manager.reinit import _warn_newly_observed_legacy
from agentworks.vms.manager.release import confirm_vm_release, reconcile_release
from tests.conftest import CapturedOutput
from tests.vm_access_support import AccessRecorder

if TYPE_CHECKING:
    from collections.abc import Callable


def os_release(release: DebianRelease) -> Result:
    version = "12" if release is DebianRelease.BOOKWORM else "13"
    return Result(Completed(0), f"ID=debian\nVERSION_ID={version}\nVERSION_CODENAME={release.value}\n".encode())


def test_reconcile_release_fills_an_unknown_observation(db: Database) -> None:
    vm = db.insert_vm("box", site="lima-local", hostname="box")

    assert reconcile_release(db, vm, DebianRelease.TRIXIE) is DebianRelease.TRIXIE
    observed = db.get_vm("box")
    assert observed is not None
    assert observed.debian_release is DebianRelease.TRIXIE
    assert observed.debian_release_observed_at is not None


def test_reconcile_release_refuses_recorded_live_drift(db: Database) -> None:
    vm = db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release(vm.name, DebianRelease.BOOKWORM)
    recorded = db.get_vm(vm.name)
    assert recorded is not None

    with pytest.raises(StateError) as caught:
        reconcile_release(db, recorded, DebianRelease.TRIXIE)

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


@pytest.fixture
def answer_release(
    monkeypatch: pytest.MonkeyPatch, vm_access_recorder: AccessRecorder
) -> Callable[[DebianRelease], AccessRecorder]:
    """Open no real gate, and answer the release request with ``release``."""
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.gated_vm_boundary",
        lambda *_args, **_kwargs: contextlib.nullcontext((object(), object(), object())),
    )

    def answer(release: DebianRelease) -> AccessRecorder:
        vm_access_recorder.access(vm_name="box").run_outcomes = [os_release(release)]
        return vm_access_recorder

    return answer


def test_confirm_release_runs_only_the_release_request(
    db: Database,
    make_config,  # noqa: ANN001
    answer_release: Callable[[DebianRelease], AccessRecorder],
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    recorder = answer_release(DebianRelease.TRIXIE)

    confirm_vm_release(db, make_config(), "box", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    (request,) = recorder.requests("run")
    assert isinstance(request, Command)
    assert request.argv == ("cat", "/etc/os-release")


def test_confirm_release_adopts_live_change_and_requires_reinit(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    answer_release: Callable[[DebianRelease], AccessRecorder],
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release("box", DebianRelease.BOOKWORM)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    answer_release(DebianRelease.TRIXIE)

    observed = confirm_vm_release(
        db,
        make_config(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    vm = db.get_vm("box")
    assert observed is DebianRelease.TRIXIE
    assert vm is not None
    assert vm.debian_release is DebianRelease.TRIXIE
    assert vm.init_status == InitStatus.PENDING.value


@pytest.mark.parametrize(
    ("recorded", "observed"),
    [
        (None, DebianRelease.TRIXIE),
        (DebianRelease.TRIXIE, DebianRelease.BOOKWORM),
    ],
)
def test_confirm_release_adopts_unknown_or_backward_observation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    answer_release: Callable[[DebianRelease], AccessRecorder],
    recorded: DebianRelease | None,
    observed: DebianRelease,
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    if recorded is not None:
        db.update_vm_debian_release("box", recorded)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    answer_release(observed)

    confirm_vm_release(
        db,
        make_config(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    vm = db.get_vm("box")
    assert vm is not None
    assert vm.debian_release is observed
    assert vm.init_status == InitStatus.PENDING.value


def test_confirm_release_rolls_back_observation_when_pending_write_fails(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    answer_release: Callable[[DebianRelease], AccessRecorder],
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release("box", DebianRelease.BOOKWORM)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    answer_release(DebianRelease.TRIXIE)

    def fail_pending_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(db, "update_vm_init_status", fail_pending_write)

    with pytest.raises(RuntimeError, match="write failed"):
        confirm_vm_release(
            db,
            make_config(),
            "box",
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    vm = db.get_vm("box")
    assert vm is not None
    assert vm.debian_release is DebianRelease.BOOKWORM
    assert vm.init_status == InitStatus.COMPLETE.value


def test_confirm_release_refreshes_matching_observation_without_reinit(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    answer_release: Callable[[DebianRelease], AccessRecorder],
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release("box", DebianRelease.TRIXIE)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    answer_release(DebianRelease.TRIXIE)

    confirm_vm_release(
        db,
        make_config(),
        "box",
        yes=False,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    vm = db.get_vm("box")
    assert vm is not None
    assert vm.init_status == InitStatus.COMPLETE.value


def test_confirm_release_decline_preserves_recorded_state(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    answer_release: Callable[[DebianRelease], AccessRecorder],
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release("box", DebianRelease.BOOKWORM)
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    answer_release(DebianRelease.TRIXIE)
    monkeypatch.setattr("agentworks.vms.manager.release.output.confirm", lambda *_args, **_kwargs: False)

    with pytest.raises(UserAbort):
        confirm_vm_release(
            db,
            make_config(),
            "box",
            yes=False,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    vm = db.get_vm("box")
    assert vm is not None
    assert vm.debian_release is DebianRelease.BOOKWORM
    assert vm.init_status == InitStatus.COMPLETE.value


def test_an_unsupported_client_refuses_confirm_release_before_the_gate(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.execution.errors import ClientUnsupported

    db.insert_vm("box", site="lima-local", hostname="box")

    def _unsupported() -> object:
        raise ClientUnsupported("no supported OpenSSH client")

    monkeypatch.setattr("agentworks.execution.resolve_ssh", _unsupported)
    monkeypatch.setattr(
        "agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("the registry was loaded")
    )
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.gated_vm_boundary", lambda *a, **k: pytest.fail("the gate opened")
    )

    with pytest.raises(ClientUnsupported):
        confirm_vm_release(db, make_config(), "box", yes=True, interaction=TtyInteractionPolicy.REFUSE)
