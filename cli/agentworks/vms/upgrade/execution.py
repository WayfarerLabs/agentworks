"""Detached systemd execution and postcondition inspection on the guest."""

from __future__ import annotations

import shlex
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .engine import ActionDisposition, ActionResult
from .journal import JournalState, UpgradeAction, UpgradePair
from .remote import REMOTE_ROOT, RemoteJournal
from .scripts import render_upgrade_script

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.transports import Transport

_POLL_SECONDS = 3


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    disposition: ActionDisposition
    detail: str | None = None


class RemoteUpgradeExecution:
    """Run package actions detached from SSH and prove their outcomes."""

    def __init__(
        self,
        target: Transport,
        journal: RemoteJournal,
        pair: UpgradePair,
        *,
        target_version_id: str,
        target_suites: Sequence[str],
    ) -> None:
        self._target = target
        self._journal = journal
        self._pair = pair
        self._target_version_id = target_version_id
        self._target_suites = tuple(target_suites)
        self._directory = f"{REMOTE_ROOT}/{pair.dirname}"
        self._script = f"{self._directory}/upgrade.sh"
        self._log = f"{self._directory}/upgrade.log"

    def install_script(self) -> None:
        content = render_upgrade_script(self._pair, target_suites=self._target_suites)
        staging = f"/var/tmp/agentworks-upgrade-{uuid.uuid4().hex}.sh"
        try:
            self._target.write_file(staging, content, mode="0700")
            self._target.run(
                f"install -m 0700 {shlex.quote(staging)} {shlex.quote(self._script)}",
                sudo=True,
            )
        finally:
            self._target.run(f"rm -f {shlex.quote(staging)}", check=False)

    def start(self, action: UpgradeAction, attempt_id: str) -> ActionResult:
        if action is UpgradeAction.REBOOT:
            boot_id = self.current_boot_id()
            self._journal.dispatch_reboot(self._pair, boot_id)
            return ExecutionResult(ActionDisposition.RUNNING)

        unit = _unit_name(attempt_id)
        command = shlex.join([self._script, action.value])
        systemd_run = (
            f"systemd-run --unit={shlex.quote(unit)} --collect --quiet "
            "--property=Type=exec --property=TimeoutStopSec=30 "
            f"--property=StandardOutput=append:{shlex.quote(self._log)} "
            f"--property=StandardError=append:{shlex.quote(self._log)} "
            f"/bin/bash -lc {shlex.quote(command)}"
        )
        result = self._target.run(systemd_run, sudo=True, check=False)
        if not result.ok:
            detail = (result.stderr or result.stdout or "systemd-run failed").strip()
            return ExecutionResult(ActionDisposition.REPAIR_REQUIRED, detail[-2000:])
        return ExecutionResult(ActionDisposition.RUNNING)

    def wait(self, action: UpgradeAction, state: JournalState) -> ActionResult:
        """Poll until an action finishes; interruption leaves intent durable."""
        while True:
            result = self.inspect(action, state)
            if result.disposition is not ActionDisposition.RUNNING:
                return result
            time.sleep(_POLL_SECONDS)

    def inspect(self, action: UpgradeAction, state: JournalState) -> ActionResult:
        if action is UpgradeAction.REBOOT:
            current = self.current_boot_id()
            if state.boot_id_before is not None and current != state.boot_id_before:
                return ExecutionResult(ActionDisposition.SUCCEEDED)
            return ExecutionResult(ActionDisposition.RUNNING)

        if state.attempt_id is None:
            return ExecutionResult(ActionDisposition.REPAIR_REQUIRED, "active action has no attempt identity")
        unit = _unit_name(state.attempt_id)
        show = self._target.run(
            f"systemctl show {shlex.quote(unit)} --property=ActiveState --property=ExecMainStatus --value",
            sudo=True,
            check=False,
        )
        values = [line.strip() for line in (show.stdout or "").splitlines() if line.strip()]
        if values and values[0] in {"active", "activating", "deactivating"}:
            return ExecutionResult(ActionDisposition.RUNNING)

        postcondition = self._postcondition(action)
        if postcondition:
            return ExecutionResult(ActionDisposition.SUCCEEDED)
        locks = self._target.run(
            "command -v fuser >/dev/null 2>&1 && "
            "fuser /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend "
            "/var/cache/apt/archives/lock /var/lib/apt/lists/lock 2>/dev/null",
            sudo=True,
            check=False,
        )
        lock_owners = " ".join((locks.stdout or "").split())
        if locks.returncode not in {0, 1}:
            return ExecutionResult(
                ActionDisposition.REPAIR_REQUIRED,
                "cannot inspect native apt/dpkg lock ownership after an interrupted action",
            )
        if lock_owners:
            return ExecutionResult(
                ActionDisposition.REPAIR_REQUIRED,
                f"native apt/dpkg locks remain owned by process(es): {lock_owners}",
            )
        status = values[-1] if values else "unknown"
        tail = self._target.run(f"tail -n 30 {shlex.quote(self._log)}", sudo=True, check=False)
        detail = (tail.stdout or tail.stderr or f"systemd action exited with status {status}").strip()
        return ExecutionResult(ActionDisposition.RETRYABLE, detail[-2000:])

    def current_boot_id(self) -> str:
        return self._target.run("cat /proc/sys/kernel/random/boot_id").stdout.strip()

    def _postcondition(self, action: UpgradeAction) -> bool:
        if action is UpgradeAction.SOURCE_UPDATE:
            command = (
                'audit="$(dpkg --audit)" && test -z "$audit" && '
                'simulation="$(LC_ALL=C apt-get -s full-upgrade)" && '
                "! printf '%s\\n' \"$simulation\" | grep -q '^Inst '"
            )
        elif action is UpgradeAction.SWITCH_SOURCES:
            suites = "|".join(self._target_suites)
            command = (
                f"test -f {shlex.quote(self._directory)}/sources-before/.archive-complete && "
                f"test -f {shlex.quote(self._directory)}/.switch-sources-complete && "
                f"grep -Eq '^Suites:.*({suites})' /etc/apt/sources.list.d/debian.sources && "
                f"! grep -R -w {shlex.quote(self._pair.source)} "
                "/etc/apt/sources.list /etc/apt/sources.list.d/*.list "
                "/etc/apt/sources.list.d/*.sources 2>/dev/null"
            )
        elif action in {UpgradeAction.MINIMAL_UPGRADE, UpgradeAction.FULL_UPGRADE}:
            command = (
                f"dpkg --compare-versions \"$(dpkg-query -W -f='${{Version}}' base-files)\" ge "
                f'{shlex.quote(self._target_version_id)} && audit="$(dpkg --audit)" && test -z "$audit"'
            )
            if action is UpgradeAction.FULL_UPGRADE:
                command += (
                    ' && simulation="$(LC_ALL=C apt-get -s full-upgrade)"'
                    " && ! printf '%s\\n' \"$simulation\" | grep -q '^Inst '"
                )
        else:
            return False
        return self._target.run(command, sudo=True, check=False).ok


def _unit_name(attempt_id: str) -> str:
    safe = attempt_id.replace("-", "")
    return f"agentworks-debian-upgrade-{safe}"
