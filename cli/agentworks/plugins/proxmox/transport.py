"""QEMU Guest Agent execution transport for Proxmox VMs.

QGA always starts commands as root, so admin commands use ``runuser`` while
``sudo=True`` retains the root identity. Its ``input-data`` field is limited
to 65,536 characters. QGA has no cancellation endpoint, so this adapter stops
polling without claiming that the guest process stopped and never redispatches
after an ambiguous failure.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from agentworks.plugins.proxmox.api import ProxmoxAPIError, _InvalidQGAExecStatus, _validate_qga_exec_status
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import ExecTransport

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI
    from agentworks.ssh import SSHLogger


_INPUT_DATA_LIMIT = 65_536
_POLL_INTERVAL_SECONDS = 2.0


class ProxmoxExecTransport(ExecTransport):
    """Bounded command execution through Proxmox QGA exec and status."""

    def __init__(
        self,
        api: ProxmoxAPI,
        *,
        node: str,
        vmid: int,
        admin_username: str,
        logger: SSHLogger | None = None,
        default_timeout: int | None = None,
    ) -> None:
        self._api = api
        self.node = node
        self.vmid = vmid
        self.admin_username = admin_username
        self.logger = logger
        self.default_timeout = default_timeout

    def describe(self) -> str:
        return f"proxmox:{self.vmid}@{self.node}"

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        check: bool = True,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> SSHResult:
        """Run ``command`` once through QGA and return its captured result."""
        if input_text is not None and len(input_text) > _INPUT_DATA_LIMIT:
            raise SSHError(f"Proxmox QGA input_text exceeds {_INPUT_DATA_LIMIT} characters") from None

        argv = _command_argv(command, admin_username=self.admin_username, sudo=sudo)
        resolved_timeout = self._resolve_timeout(timeout)
        deadline = None if resolved_timeout is None else time.monotonic() + resolved_timeout
        dispatch_timeout = self._remaining(deadline, pid=None)

        dispatch_failure = (
            f"Proxmox QGA command dispatch failed for {self.describe()}; the guest command may have started"
        )
        dispatch_failed = False
        try:
            pid = self._api.guest_agent_exec(
                self.node,
                self.vmid,
                command=argv,
                input_data=input_text,
                timeout=dispatch_timeout,
            )
        except ProxmoxAPIError as error:
            if input_text is None:
                raise SSHError(f"{dispatch_failure}: {error}") from error
            dispatch_failed = True
        except Exception:
            if input_text is None:
                raise
            dispatch_failed = True
        if dispatch_failed:
            raise SSHError(dispatch_failure) from None

        while True:
            status_timeout = self._remaining(deadline, pid=pid)
            status_failure = (
                f"Proxmox QGA status failed for {self.describe()} (PID {pid}); the guest command may still be running"
            )
            status_failed = False
            try:
                status = self._api.guest_agent_exec_status(
                    self.node,
                    self.vmid,
                    pid=pid,
                    timeout=status_timeout,
                )
            except ProxmoxAPIError as error:
                if input_text is None:
                    raise SSHError(f"{status_failure}: {error}") from error
                status_failed = True
            except Exception:
                if input_text is None:
                    raise
                status_failed = True
            if status_failed:
                raise SSHError(status_failure) from None

            validation_failure: str | None = None
            try:
                exited, result = _parse_status(status, sensitive=input_text is not None)
            except SSHError as error:
                validation_failure = str(error)
            if validation_failure is not None:
                raise SSHError(f"{validation_failure} for {self.describe()} (PID {pid})") from None
            if exited:
                assert result is not None
                self._log(command, result)
                if check and not result.ok:
                    detail = "" if input_text is not None else f"\nstderr: {result.stderr.strip()}"
                    raise SSHError(
                        f"Proxmox QGA command failed (exit {result.returncode}): {command}{detail}"
                    ) from None
                return result

            remaining = self._remaining(deadline, pid=pid)
            time.sleep(_POLL_INTERVAL_SECONDS if remaining is None else min(_POLL_INTERVAL_SECONDS, remaining))

    def _remaining(self, deadline: float | None, *, pid: int | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            return remaining
        if pid is None:
            message = f"Proxmox QGA command timed out before dispatch for {self.describe()}"
        else:
            message = (
                f"Proxmox QGA command timed out for {self.describe()} (PID {pid}); "
                "the guest command may still be running"
            )
        if self.logger is not None:
            self.logger.log_error(message)
        raise SSHError(message) from None

    def _log(self, command: str, result: SSHResult) -> None:
        if self.logger is None:
            return
        self.logger.log_command(command, result)


def _command_argv(command: str, *, admin_username: str, sudo: bool) -> list[str]:
    """Render QGA argv without accepting or interpolating stdin."""
    if sudo:
        return ["/bin/bash", "-lc", command]
    return [
        "/usr/sbin/runuser",
        "-u",
        admin_username,
        "--",
        "/bin/bash",
        "-lc",
        command,
    ]


def _parse_status(status: dict[str, object], *, sensitive: bool) -> tuple[bool, SSHResult | None]:
    """Validate one external exec-status response and map complete output."""
    try:
        normalized = _validate_qga_exec_status(status)
    except _InvalidQGAExecStatus as error:
        raise SSHError(str(error)) from None

    if not cast("bool", normalized["exited"]):
        return False, None

    returncode = cast("int", normalized["exitcode"]) if "exitcode" in normalized else -cast("int", normalized["signal"])
    stdout = cast("str", normalized.get("out-data", ""))
    stderr = cast("str", normalized.get("err-data", ""))
    if sensitive:
        stdout = ""
        stderr = ""
    return True, SSHResult(returncode=returncode, stdout=stdout, stderr=stderr)
