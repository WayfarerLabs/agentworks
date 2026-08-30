from __future__ import annotations

import subprocess
from dataclasses import dataclass

from agentworks.vms.upgrade.engine import ActionDisposition
from agentworks.vms.upgrade.execution import RemoteUpgradeExecution
from agentworks.vms.upgrade.journal import JournalState, UpgradeAction, UpgradePair


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


class _MultilineFailureTarget:
    def run(self, command: str, **kwargs: object) -> _Result:
        del kwargs
        if command.startswith("systemctl show"):
            return _Result(0, "failed\n100\n", "")
        if command.startswith("command -v fuser"):
            return _Result(1, "", "")
        if command.startswith("tail -n 30"):
            return _Result(0, "first failure\nsecond failure\n", "")
        return _Result(1, "", "")


def test_multiline_failed_unit_detail_is_safe_for_the_durable_journal() -> None:
    pair = UpgradePair("bookworm", "trixie")
    state = JournalState.prepared().claim(UpgradeAction.SOURCE_UPDATE)
    execution = RemoteUpgradeExecution(
        _MultilineFailureTarget(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        pair,
        target_version_id="13",
        target_suites=("trixie",),
    )

    result = execution.inspect(UpgradeAction.SOURCE_UPDATE, state)

    assert result.disposition is ActionDisposition.RETRYABLE
    assert result.detail is not None
    assert state.attempt_id is not None
    failed = state.fail_active(state.attempt_id, result.detail, repair_required=False)
    assert failed.failure == "first failure | second failure"
