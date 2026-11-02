"""``bootstrap_vm``'s platform-hook wiring, both directions, and what it hands Phase B.

Success: a completed Phase A ends in the ``on_tailscale_ready`` callback
firing (``create_vm`` wires that callback to the platform's
``post_tailscale_ready``, the seam where Azure removes its scoped bootstrap
SSH allow on the happy path). Failure: the kept-failed path calls the
platform's ``secure_failed_vm`` hook best-effort (fail closed: without it a
failed Azure VM would keep its bootstrap ingress indefinitely), and the
original error keeps propagating even when the hook itself fails. The same
holds for an operator interrupt, which also leaves an unattested VM failed.

Phase A runs for real over a database row and the recording ``VMAccess``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import ProvisioningStatus
from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.execution import trust
from agentworks.vms.initializer import driver
from tests.vms.phase_a_support import ADDRESS, VM, PhaseAHarness, contract_outcomes

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder

FAILING_SCRIPT = b"AGW-CHECK base-tools flock\nAGW-CHECK-END\n"


@pytest.fixture
def phase(db: Database, vm_access_recorder: AccessRecorder, monkeypatch: pytest.MonkeyPatch) -> PhaseAHarness:
    """Phase A's harness, with the reconnect pauses, the SSH-config sync and Phase B's transport stubbed."""
    monkeypatch.setattr("agentworks.execution.reachability.time.sleep", lambda seconds: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr(driver, "SSHTransport", lambda **kwargs: SimpleNamespace(**kwargs))
    return PhaseAHarness(db, vm_access_recorder)


def bootstrap(phase: PhaseAHarness, on_ready: Callable[[], None] | None = None, logger: Any = None) -> Any:
    return driver.bootstrap_vm(
        phase.db,
        phase.config,
        VM,
        phase.platform,  # type: ignore[arg-type]
        RunContext(),
        logger or phase.log,
        admin_username="agentworks",
        tailnet_address=ADDRESS,
        expected_release=DebianRelease.TRIXIE,
        on_tailscale_ready=on_ready,
    )


def test_phase_b_receives_a_vm_bound_target_at_the_recorded_address(phase: PhaseAHarness) -> None:
    ts_target, home, release = bootstrap(phase, lambda: None)

    assert ts_target.host == ADDRESS
    assert ts_target.trust == trust.TrustBinding(trust.TrustMode.VM, VM)
    assert ts_target.logger is phase.log
    assert (home, release) == ("/home/agentworks", DebianRelease.TRIXIE)


def test_bootstrap_uses_manager_owned_logger_without_closing(phase: PhaseAHarness) -> None:
    logger = MagicMock(display_path="~/hookvm-vm-create.log")

    bootstrap(phase, lambda: None, logger=logger)

    logger.close.assert_not_called()


def test_success_path_fires_on_tailscale_ready(phase: PhaseAHarness) -> None:
    """A successful Phase A ends in the on_tailscale_ready callback and never touches the fail-closed hook."""
    fired: list[bool] = []

    bootstrap(phase, lambda: fired.append(True))

    assert fired == [True]
    assert phase.platform.secured == []


def test_failed_path_secures_the_kept_vm(phase: PhaseAHarness) -> None:
    """The kept-failed path calls secure_failed_vm and never the success callback; the failure propagates."""
    phase.admin.run_outcomes = contract_outcomes(script=FAILING_SCRIPT)
    fired: list[bool] = []

    with pytest.raises(StateError):
        bootstrap(phase, lambda: fired.append(True))

    assert phase.platform.secured == [VM]
    assert fired == []


def test_failed_path_hook_failure_does_not_mask(phase: PhaseAHarness, captured_output: Any) -> None:
    """secure_failed_vm is best-effort: a hook failure warns and the original Phase A error still propagates."""
    phase.admin.run_outcomes = contract_outcomes(script=FAILING_SCRIPT)
    phase.platform.secure_error = RuntimeError("hook exploded")

    with pytest.raises(StateError):
        bootstrap(phase)

    assert len(captured_output.warnings) == 2  # the hook's failure, and the log pointer


def test_interrupt_during_first_contact_secures_marks_failed_and_reraises(phase: PhaseAHarness) -> None:
    """Ctrl-C escapes the Exception arm, but the kept VM is still secured, and, never attested, marked failed."""
    phase.enrollment.run_outcomes = [KeyboardInterrupt()]
    fired: list[bool] = []

    with pytest.raises(KeyboardInterrupt):
        bootstrap(phase, lambda: fired.append(True))

    assert phase.platform.secured == [VM]
    assert fired == []
    assert phase.row().provisioning_status == ProvisioningStatus.FAILED.value


def test_interrupt_hook_failure_does_not_mask_the_interrupt(phase: PhaseAHarness, captured_output: Any) -> None:
    phase.enrollment.run_outcomes = [KeyboardInterrupt()]
    phase.platform.secure_error = RuntimeError("hook exploded")

    with pytest.raises(KeyboardInterrupt):
        bootstrap(phase)

    assert len(captured_output.warnings) == 1
