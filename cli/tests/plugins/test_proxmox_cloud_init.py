"""Cloud-init readiness for the Proxmox platform, through the guest agent's root channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ConnectivityError, ProvisioningError
from agentworks.plugins.proxmox.platform import CLOUD_INIT_DEADLINE_SECONDS, _wait_for_cloud_init
from tests._root_channel_fakes import Answer, FakeGuest

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def _guest(answer: Answer) -> FakeGuest:
    return FakeGuest(respond=lambda payload, seen: answer)


def test_cloud_init_success_returns_without_warning(captured_output: CapturedOutput) -> None:
    guest = _guest(Answer(status=0))

    _wait_for_cloud_init(guest.run_as_root, 101)

    assert guest.payloads == ["cloud-init status --wait"]
    assert captured_output.warnings == []


def test_cloud_init_degraded_completion_returns_with_warning(captured_output: CapturedOutput) -> None:
    guest = _guest(Answer(status=2))

    _wait_for_cloud_init(guest.run_as_root, 101)

    assert len(captured_output.warnings) == 1


@pytest.mark.parametrize("status", [1, None], ids=["failed", "unknown"])
def test_cloud_init_that_does_not_complete_raises_after_one_run(status: int | None) -> None:
    guest = _guest(Answer(status=status))

    with pytest.raises(ProvisioningError):
        _wait_for_cloud_init(guest.run_as_root, 101)

    assert len(guest.scripts) == 1


def test_a_channel_failure_is_asked_again_until_cloud_init_answers(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """cloud-init can restart the guest agent mid-run; asking ``cloud-init status --wait`` again is safe."""
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.sleep", lambda seconds: None)
    restarted = ConnectivityError("Proxmox QGA status failed for VM 101; the script may still be running")
    guest = FakeGuest(respond=lambda payload, seen: Answer(raises=restarted) if seen == 0 else Answer(status=0))

    _wait_for_cloud_init(guest.run_as_root, 101)

    assert guest.payloads == ["cloud-init status --wait"] * 2
    assert captured_output.warnings == []


def test_a_channel_that_keeps_failing_raises_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("agentworks.plugins.proxmox.platform.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "agentworks.plugins.proxmox.platform.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    guest = _guest(Answer(raises=ConnectivityError("the guest agent is gone")))

    with pytest.raises(ConnectivityError):
        _wait_for_cloud_init(guest.run_as_root, 101)

    assert now[0] >= CLOUD_INIT_DEADLINE_SECONDS
