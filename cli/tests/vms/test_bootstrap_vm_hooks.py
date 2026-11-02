"""What ``driver.bootstrap_vm`` adds to Phase A: Phase B's legacy target, and a log it never closes.

Phase A runs for real over a database row and the recording ``VMAccess``; its
own sequence, failure arms and hooks are ``test_phase_a_platform_bootstrap.py``'s.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.debian import DebianRelease
from agentworks.execution import trust
from agentworks.vms.initializer import driver
from tests.vms.phase_a_support import ADDRESS, VM, PhaseAHarness

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder


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
