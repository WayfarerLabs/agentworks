"""WSL2's bootstrap through the distro's root channel."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform.wsl2_bootstrap import BOOTSTRAP_DEADLINE_SECONDS, run_wsl2_bootstrap
from agentworks.errors import ProvisioningError
from tests._root_channel_fakes import Answer, FakeGuest

STEPS = b"##STEP## Hostname\n##SUCCESS## hostname set\n##STEP## Mask SVE\n##WARN## update-grub failed\n"


def bootstrap(guest: FakeGuest, progress: MagicMock | None = None) -> MagicMock:
    progress = progress or MagicMock()
    run_wsl2_bootstrap(
        guest.run_as_root,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        hostname="lab-vm1",
        swap_gib=0,
        progress=progress,
    )
    return progress


def test_the_bootstrap_runs_once_with_no_secret_and_reports_its_steps() -> None:
    guest = FakeGuest(lambda payload, seen: Answer(stdout=STEPS))

    progress = bootstrap(guest)

    (script,) = guest.scripts
    assert b"AGW_SECRET" not in script
    assert guest.deadlines == [BOOTSTRAP_DEADLINE_SECONDS]
    assert [call.args for call in progress.step.call_args_list] == [("Hostname",), ("Mask SVE",)]
    progress.warning.assert_called_once_with("update-grub failed")


@pytest.mark.parametrize("answer", [Answer(status=1, stdout=STEPS), Answer(status=None)], ids=["failed", "unknown"])
def test_a_bootstrap_that_does_not_exit_0_raises_without_retrying(answer: Answer) -> None:
    guest = FakeGuest(lambda payload, seen: answer)

    with pytest.raises(ProvisioningError):
        bootstrap(guest)

    assert len(guest.scripts) == 1
