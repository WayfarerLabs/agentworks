"""Full transport delegation with the owning setup operation's prepared env."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.transports import Transport

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from agentworks.ssh import SSHResult


class SetupRunner(Transport):
    """Deliver scope env while preserving core identity against local overrides."""

    def __init__(self, target: Transport, environment: Mapping[str, str]) -> None:
        self._target = target
        self._environment = dict(environment)
        self.logger = target.logger
        self.default_timeout = target.default_timeout

    def describe(self) -> str:
        return self._target.describe()

    def _env(self, local: dict[str, str] | None) -> dict[str, str]:
        merged = {**self._environment, **(local or {})}
        for key in tuple(merged):
            if key.startswith("AGENTWORKS_"):
                if key in self._environment:
                    merged[key] = self._environment[key]
                else:
                    del merged[key]
        return merged

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        tty: bool | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        input_data: str | None = None,
        discard_output: bool = False,
        retries: int | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> SSHResult:
        environment = self._env(env)
        if sudo:
            preserve = f" --preserve-env={shlex.quote(','.join(environment))}" if environment else ""
            command = f"sudo -n{preserve} bash -c {shlex.quote(command)}"
        return self._target.run(
            command,
            sudo=False,
            tty=tty,
            check=check,
            timeout=timeout,
            env=environment,
            input_text=input_text,
            input_data=input_data,
            discard_output=discard_output,
            retries=retries,
            on_retry=on_retry,
        )

    def _interactive(self, command: str, *, env: dict[str, str] | None = None) -> int:
        return self._target.interactive(command, env=self._env(env))

    def call_streaming(self, command: str, *, env: dict[str, str] | None = None) -> int:
        return self._target.call_streaming(command, env=self._env(env))

    def copy_to(self, local_path: str | Path, remote_path: str, *, timeout: int | None = None) -> None:
        self._target.copy_to(local_path, remote_path, timeout=timeout)

    def copy_from(self, remote_path: str, local_path: str | Path, *, timeout: int | None = None) -> None:
        self._target.copy_from(remote_path, local_path, timeout=timeout)
