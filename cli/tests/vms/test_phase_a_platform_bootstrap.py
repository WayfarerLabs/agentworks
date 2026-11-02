"""Phase A over ``agentworks.execution``: the sequence, the status it leaves, and what fails closed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import ProvisioningStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError, StateError
from agentworks.execution import reachability, trust
from agentworks.execution.errors import TrustMismatch
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.vms.manager import _guard_failed_vm
from tests.vm_access_support import Admin
from tests.vms.phase_a_support import (
    ADDRESS,
    BOOKWORM_OS_RELEASE,
    VM,
    PhaseAHarness,
    contract_outcomes,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.execution.reachability.time.sleep", lambda seconds: None)


@pytest.fixture
def phase(db: Database, vm_access_recorder: AccessRecorder) -> PhaseAHarness:
    return PhaseAHarness(db, vm_access_recorder)


def test_the_sequence_records_enrolls_checks_then_completes(phase: PhaseAHarness) -> None:
    address, release = phase.bootstrap(tailnet_address=f" {ADDRESS}\n")

    assert (address, release) == (ADDRESS, DebianRelease.TRIXIE)
    # The first contact enrolls; then the contract, release first, over the admin's VM-mode access.
    assert phase.recorder.operations == [("enroll", "run"), ("admin", "run"), ("admin", "run"), ("admin", "run")]
    first_contact, release_request, script, sudo = phase.recorder.requests("run")
    assert first_contact == reachability.PROBE
    assert isinstance(release_request, Command)
    assert release_request.argv == ("cat", "/etc/os-release")
    assert isinstance(script, Script)
    assert isinstance(sudo, Command)
    assert sudo.elevated
    # Both accesses were built from the row as it was after the address was recorded.
    assert [row.tailscale_host for row in phase.recorder.rows] == [ADDRESS, ADDRESS]
    assert phase.recorder.enrolled == [VM]
    assert phase.recorder.built == [(VM, Admin())]
    row = phase.row()
    assert row.tailscale_host == ADDRESS
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
    assert row.debian_release is DebianRelease.TRIXIE
    assert trust.has_record(VM)
    assert phase.events() == ["provisioning_started", "provisioning_complete"]
    assert phase.platform.secured == []


def test_the_requests_reach_the_operation_log(phase: PhaseAHarness) -> None:
    phase.bootstrap()

    assert all(log == phase.log.record for log in phase.recorder.logs)
    assert phase.log.steps[-1] == "Check the VM contract"


def test_an_address_core_cannot_parse_fails_before_any_connection_and_secures_the_vm(phase: PhaseAHarness) -> None:
    with pytest.raises(ProvisioningError):
        phase.bootstrap(tailnet_address="203.0.113.7.attacker")

    assert phase.recorder.calls == []
    row = phase.row()
    assert row.provisioning_status == ProvisioningStatus.FAILED.value
    assert row.tailscale_host is None
    assert phase.platform.secured == [VM]


def _first_contact_times_out(phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    phase.enrollment.run_outcomes = [Result(Unknown(UnknownReason.CONNECTION))] * reachability.ATTEMPTS


def _no_key_recorded(phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.execution.trust.has_record", lambda vm_name: False)


def _release_mismatch(phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    phase.admin.run_outcomes = contract_outcomes(os_release=BOOKWORM_OS_RELEASE)


def _base_failure(phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    phase.admin.run_outcomes = contract_outcomes(script=b"AGW-CHECK systemd x\nAGW-CHECK-END\n")


@pytest.mark.parametrize("arrange", [_first_contact_times_out, _no_key_recorded, _release_mismatch, _base_failure])
def test_a_failure_before_the_contract_passes_marks_failed_and_secures(
    phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch, arrange: Callable[[PhaseAHarness, pytest.MonkeyPatch], None]
) -> None:
    arrange(phase, monkeypatch)

    with pytest.raises((ProvisioningError, StateError)):
        phase.bootstrap()

    assert phase.row().provisioning_status == ProvisioningStatus.FAILED.value
    assert phase.events()[-1] == "provisioning_failed"
    assert phase.platform.secured == [VM]


def test_a_failed_release_check_records_no_release(phase: PhaseAHarness) -> None:
    phase.admin.run_outcomes = contract_outcomes(os_release=BOOKWORM_OS_RELEASE)

    with pytest.raises(StateError):
        phase.bootstrap(expected_release=DebianRelease.TRIXIE)

    assert phase.row().debian_release is None


@pytest.mark.parametrize("request_index", [0, 1, 2, 3], ids=["first-contact", "release", "script", "sudo"])
def test_an_interrupt_before_the_contract_passes_secures_and_leaves_the_vm_gated(
    phase: PhaseAHarness, request_index: int
) -> None:
    if request_index == 0:
        phase.enrollment.run_outcomes = [KeyboardInterrupt()]
    else:
        outcomes = contract_outcomes()
        outcomes[request_index - 1] = KeyboardInterrupt()
        phase.admin.run_outcomes = outcomes

    with pytest.raises(KeyboardInterrupt):
        phase.bootstrap()

    row = phase.row()
    assert row.provisioning_status != ProvisioningStatus.COMPLETE.value
    assert phase.platform.secured == [VM]
    with pytest.raises(StateError):
        _guard_failed_vm(row)


def test_an_interrupt_after_complete_leaves_the_vm_complete(phase: PhaseAHarness) -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        phase.bootstrap(on_tailscale_ready=interrupt)

    assert phase.row().provisioning_status == ProvisioningStatus.COMPLETE.value
    assert phase.platform.secured == []


def test_a_post_ready_wait_that_runs_out_warns_and_proceeds(phase: PhaseAHarness, captured_output: Any) -> None:
    phase.admin.run_outcomes = [
        *contract_outcomes(),
        *[Result(Unknown(UnknownReason.CONNECTION))] * reachability.ATTEMPTS,
    ]

    address, _release = phase.bootstrap(on_tailscale_ready=lambda: None)

    assert address == ADDRESS
    assert phase.row().provisioning_status == ProvisioningStatus.COMPLETE.value
    assert len(captured_output.warnings) == 1


def test_the_post_ready_wait_is_on_the_vm_mode_access_and_never_enrolls(phase: PhaseAHarness) -> None:
    phase.bootstrap(on_tailscale_ready=lambda: None)

    assert phase.recorder.operations[-1] == ("admin", "run")
    assert phase.recorder.requests("run")[-1] == reachability.PROBE
    assert phase.recorder.enrolled == [VM]


def test_a_post_ready_wait_that_raises_leaves_the_vm_complete_and_unsecured(phase: PhaseAHarness) -> None:
    """The VM was attested seconds earlier: the raise propagates, and create maps it to reinit advice."""
    phase.admin.run_outcomes = [*contract_outcomes(), Result(Unknown(UnknownReason.HOST_KEY))]

    with pytest.raises(TrustMismatch):
        phase.bootstrap(on_tailscale_ready=lambda: None)

    assert phase.row().provisioning_status == ProvisioningStatus.COMPLETE.value
    assert phase.platform.secured == []


def test_a_failing_write_in_the_failure_arm_neither_replaces_the_failure_nor_skips_securing(
    phase: PhaseAHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    phase.admin.run_outcomes = contract_outcomes(script=b"AGW-CHECK systemd x\nAGW-CHECK-END\n")
    write = phase.db.update_vm_provisioning_status

    def _refuse_failed(name: str, status: ProvisioningStatus) -> None:
        if status is ProvisioningStatus.FAILED:
            raise RuntimeError("database is locked")
        write(name, status)

    monkeypatch.setattr(phase.db, "update_vm_provisioning_status", _refuse_failed)

    with pytest.raises(StateError):
        phase.bootstrap()

    assert phase.platform.secured == [VM]


def test_a_secure_hook_failure_does_not_mask_the_phase_a_failure(phase: PhaseAHarness) -> None:
    """secure_failed_vm is best-effort: its failure warns and the original Phase A error still propagates."""
    phase.admin.run_outcomes = contract_outcomes(script=b"AGW-CHECK systemd x\nAGW-CHECK-END\n")
    phase.platform.secure_error = RuntimeError("hook exploded")

    with pytest.raises(StateError):
        phase.bootstrap()


def test_a_secure_hook_failure_does_not_mask_the_interrupt(phase: PhaseAHarness) -> None:
    phase.enrollment.run_outcomes = [KeyboardInterrupt()]
    phase.platform.secure_error = RuntimeError("hook exploded")

    with pytest.raises(KeyboardInterrupt):
        phase.bootstrap()


def test_the_post_ready_hook_fires_after_the_contract_and_never_secures(phase: PhaseAHarness) -> None:
    fired: list[str] = []

    phase.bootstrap(on_tailscale_ready=lambda: fired.append(phase.row().provisioning_status))

    assert fired == [ProvisioningStatus.COMPLETE.value]
    assert phase.platform.secured == []
