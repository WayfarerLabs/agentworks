"""What ``driver.bootstrap_vm`` adds to Phase A: Phase B's access, and a log it never closes.

Phase A runs for real over a database row and the recording ``VMAccess``; its
own sequence, failure arms and hooks are ``test_phase_a_platform_bootstrap.py``'s.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.debian import DebianRelease
from agentworks.execution.access import Admin
from agentworks.vms.initializer import driver
from tests.vms.phase_a_support import ADDRESS, VM, PhaseAHarness

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder


@pytest.fixture
def phase(db: Database, vm_access_recorder: AccessRecorder, monkeypatch: pytest.MonkeyPatch) -> PhaseAHarness:
    """Phase A's harness, with the reconnect pauses and the SSH-config sync stubbed."""
    monkeypatch.setattr("agentworks.execution.reachability.time.sleep", lambda seconds: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    return PhaseAHarness(db, vm_access_recorder)


def bootstrap(phase: PhaseAHarness, logger: Any = None) -> Any:
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
    )


def test_phase_b_receives_the_admins_access_built_from_the_recorded_address(phase: PhaseAHarness) -> None:
    access, home, release = bootstrap(phase)

    assert access is phase.admin
    assert phase.recorder.built[-1] == (VM, Admin())
    assert phase.recorder.rows[-1].tailscale_host == ADDRESS
    assert phase.recorder.logs[-1] == phase.log.record
    assert (home, release) == ("/home/agentworks", DebianRelease.TRIXIE)


def test_bootstrap_uses_manager_owned_logger_without_closing(phase: PhaseAHarness) -> None:
    logger = MagicMock(display_path="~/hookvm-vm-create.log")

    bootstrap(phase, logger=logger)

    logger.close.assert_not_called()
