"""QEMU Guest Agent execution transport for Proxmox VMs."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import ExecTransport

if TYPE_CHECKING:
    from collections.abc import Callable

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
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self.node = node
        self.vmid = vmid
        self.admin_username = admin_username
        self.logger = logger
        self.default_timeout = default_timeout
        self._monotonic = monotonic
        self._sleep = sleep

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
            raise ValueError(f"Proxmox QGA input_text exceeds {_INPUT_DATA_LIMIT} characters")

        argv = _command_argv(command, admin_username=self.admin_username, sudo=sudo)
        resolved_timeout = self._resolve_timeout(timeout)
        deadline = None if resolved_timeout is None else self._monotonic() + resolved_timeout
        dispatch_timeout = self._remaining(deadline, pid=None)

        dispatch_failed = False
        try:
            pid = self._api.guest_agent_exec(
                self.node,
                self.vmid,
                command=argv,
                input_data=input_text,
                timeout=dispatch_timeout,
            )
        except ProxmoxAPIError:
            dispatch_failed = True
        except Exception:
            if input_text is None:
                raise
            dispatch_failed = True
        if dispatch_failed:
            raise SSHError(
                f"Proxmox QGA command dispatch failed for {self.describe()}; the guest command may have started"
            ) from None

        while True:
            status_timeout = self._remaining(deadline, pid=pid)
            status_failed = False
            try:
                status = self._api.guest_agent_exec_status(
                    self.node,
                    self.vmid,
                    pid=pid,
                    timeout=status_timeout,
                )
            except ProxmoxAPIError:
                status_failed = True
            except Exception:
                if input_text is None:
                    raise
                status_failed = True
            if status_failed:
                raise SSHError(
                    f"Proxmox QGA status failed for {self.describe()} (PID {pid}); "
                    "the guest command may still be running"
                ) from None

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
            self._sleep(_POLL_INTERVAL_SECONDS if remaining is None else min(_POLL_INTERVAL_SECONDS, remaining))

    def _remaining(self, deadline: float | None, *, pid: int | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - self._monotonic()
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
    exited = status.get("exited")
    if type(exited) is not bool:
        raise SSHError("Proxmox QGA exec-status response has an invalid exited field") from None
    if not exited:
        return False, None

    exitcode = status.get("exitcode")
    signal = status.get("signal")
    has_exitcode = type(exitcode) is int
    has_signal = type(signal) is int
    if has_exitcode == has_signal:
        raise SSHError("Proxmox QGA exec-status response has an invalid exit status") from None

    stdout = status.get("out-data", "")
    stderr = status.get("err-data", "")
    out_truncated = status.get("out-truncated", False)
    err_truncated = status.get("err-truncated", False)
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise SSHError("Proxmox QGA exec-status response has invalid output fields") from None
    if type(out_truncated) is not bool or type(err_truncated) is not bool:
        raise SSHError("Proxmox QGA exec-status response has invalid truncation fields") from None
    if out_truncated or err_truncated:
        raise SSHError("Proxmox QGA command output was truncated") from None

    if has_exitcode:
        assert isinstance(exitcode, int)
        returncode = exitcode
    else:
        assert isinstance(signal, int)
        returncode = -signal
    if sensitive:
        stdout = ""
        stderr = ""
    return True, SSHResult(returncode=returncode, stdout=stdout, stderr=stderr)
