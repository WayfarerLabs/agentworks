from __future__ import annotations

import subprocess
from dataclasses import dataclass

from agentworks.vms.upgrade.execution import RemoteUpgradeExecution
from agentworks.vms.upgrade.journal import UpgradeAction, UpgradePair


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _FailingSimulationTarget:
    def run(self, command: str, **kwargs: object) -> _Result:
        del kwargs
        prelude = "dpkg() { return 0; }; apt-get() { return 100; }; export -f dpkg apt-get; "
        completed = subprocess.run(
            ["bash", "-c", prelude + command],
            capture_output=True,
            text=True,
            check=False,
        )
        return _Result(completed.returncode, completed.stdout, completed.stderr)


def test_failed_apt_simulation_cannot_satisfy_source_current_postcondition() -> None:
    execution = RemoteUpgradeExecution(
        _FailingSimulationTarget(),
        object(),
        UpgradePair("bookworm", "trixie"),
        target_version_id="13",
        target_suites=("trixie",),
    )

    assert execution._postcondition(UpgradeAction.SOURCE_UPDATE) is False
