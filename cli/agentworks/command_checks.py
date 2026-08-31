"""Target-user command availability checks."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import SSHResult
    from agentworks.transports import Transport


def run_user_shell_command(
    command: str,
    transport: Transport,
    *,
    timeout: int | None = None,
) -> SSHResult:
    """Run a read-only probe in the target user's login/interactive shell.

    The fixed descriptor channel distinguishes an inner-command status from a
    shell-startup failure while the outer shell suppresses both startup and
    command output.
    """
    status_probe = (
        f"(\n{command}\n)\ncommand_status=$?\nprintf 'agentworks-command-status:%s\\n' \"$command_status\" >&3\nexit 0"
    )
    wrapper = (
        f'status_line=$("$SHELL" -lic {shlex.quote(status_probe)} 3>&1 1>/dev/null 2>/dev/null) || exit 22\n'
        'case "$status_line" in\n'
        "  agentworks-command-status:[0-9]|"
        "agentworks-command-status:[0-9][0-9]|"
        "agentworks-command-status:[0-9][0-9][0-9]) ;;\n"
        "  *) exit 22 ;;\n"
        "esac\n"
        "command_status=${status_line#agentworks-command-status:}\n"
        '[ "$command_status" -le 255 ] || exit 22\n'
        'exit "$command_status"'
    )
    return transport.run(
        f"{{ {wrapper}; }} >/dev/null 2>&1",
        check=False,
        # Command redirection cannot suppress output from an outer login shell
        # that a transport starts before evaluating this wrapper.
        discard_output=True,
        timeout=timeout,
    )


def check_required_commands(
    commands: tuple[str, ...],
    transport: Transport,
    *,
    timeout: int | None = None,
) -> tuple[str, ...]:
    """Return commands absent from the target user's login/interactive shell.

    Command lookup uses the same login and interactive startup environment as
    user workloads. A result other than present or absent is indeterminate and
    fails with a value-safe transport error.
    """
    missing: list[str] = []
    for command in commands:
        result = run_user_shell_command(
            f"command -v {shlex.quote(command)} >/dev/null 2>&1 || exit 20",
            transport,
            timeout=timeout,
        )
        if result.returncode == 0:
            continue
        if result.returncode == 20:
            missing.append(command)
            continue
        raise SSHError("could not check required commands in the target user's shell")
    return tuple(missing)
