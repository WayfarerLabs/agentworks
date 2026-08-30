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
            return _Result(0, "ExecMainStatus=100\nActiveState=failed\n", "")
        if command.startswith("command -v fuser"):
            return _Result(1, "", "")
        if command == "dpkg --audit":
            return _Result(0, "", "")
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


class _OrderedPropertyTarget:
    def __init__(self, *, active_state: str, audit: str = "") -> None:
        self.active_state = active_state
        self.audit = audit
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> _Result:
        del kwargs
        self.commands.append(command)
        if command.startswith("systemctl show"):
            return _Result(0, f"ExecMainStatus=0\nActiveState={self.active_state}\n", "")
        if "fuser /var/lib/dpkg/lock" in command:
            return _Result(1, "", "")
        if command == "dpkg --audit":
            return _Result(0, self.audit, "")
        return _Result(1, "", "")


def _execution_for(target: _OrderedPropertyTarget) -> tuple[RemoteUpgradeExecution, JournalState]:
    pair = UpgradePair("bookworm", "trixie")
    source_update = JournalState.prepared().claim(UpgradeAction.SOURCE_UPDATE)
    assert source_update.attempt_id is not None
    source_current = source_update.complete_active(source_update.attempt_id)
    return (
        RemoteUpgradeExecution(
            target,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            pair,
            target_version_id="13",
            target_suites=("trixie",),
        ),
        source_current.claim(UpgradeAction.SWITCH_SOURCES),
    )


def test_named_systemd_properties_keep_active_action_running_regardless_of_order() -> None:
    target = _OrderedPropertyTarget(active_state="active")
    execution, state = _execution_for(target)

    result = execution.inspect(UpgradeAction.SWITCH_SOURCES, state)

    assert result.disposition is ActionDisposition.RUNNING
    assert len(target.commands) == 1


def test_interrupted_dpkg_state_requires_manual_repair_instead_of_apt_replay() -> None:
    target = _OrderedPropertyTarget(active_state="failed", audit="package is only half configured\n")
    execution, state = _execution_for(target)

    result = execution.inspect(UpgradeAction.SWITCH_SOURCES, state)

    assert result.disposition is ActionDisposition.REPAIR_REQUIRED
