"""Read-only probes in a target user's own login and interactive shell."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from agentworks.errors import ExternalError
from agentworks.execution.envelope import new_nonce
from agentworks.execution.requests import DiscardOutput, Script, Shell

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess

STATUS_PREFIX = "agentworks-command-status:"
"""What starts the line a probe reports its command's status on, followed by the request's nonce."""

_MISSING_STATUS = 20


def user_shell_probe(command: str, *, nonce: str, deadline: float | None = None) -> Script:
    """The request :func:`run_user_shell_command` sends for ``command``.

    The command runs in a subshell with both of its streams discarded on the
    guest, then its status is printed as ``agentworks-command-status:<nonce>:<status>``
    on a line of its own, after a newline in case startup output left one
    unterminated. The script is sourced by the account's login and
    interactive shell, so its startup files have run first, as they do for
    the user's own workloads. The nonce is fresh for each request, so
    startup output that does not know it, an ``EXIT`` trap or a line left
    unterminated included, cannot pass for the status. A startup file that
    redefines the probe's own commands (a ``printf`` function, say) can
    still forge it: a command check is a diagnostic, not a security
    boundary. The streams are not logged.
    """
    source = f"(\n{command}\n) >/dev/null 2>&1\nprintf '\\n{STATUS_PREFIX}{nonce}:%s\\n' \"$?\"\n"
    return Script(source, Shell.USER_DEFAULT, login=True, interactive=True, deadline=deadline, log_streams=False)


def presence_probe(command: str, *, nonce: str, deadline: float | None = None) -> Script:
    """The probe :func:`check_required_commands` sends for one command: it exits 0 when found, 20 when not."""
    return user_shell_probe(
        f"command -v {shlex.quote(command)} >/dev/null 2>&1 || exit {_MISSING_STATUS}", nonce=nonce, deadline=deadline
    )


def run_user_shell_command(command: str, access: VMAccess, *, deadline: float | None = None) -> int:
    """Run a read-only probe in the target user's login and interactive shell and return its exit status.

    The command's own output never leaves the guest, and the shell's stderr
    is discarded. Startup files may print to stdout, but only the line
    carrying this request's fresh nonce counts. A probe that reports no such
    line (a startup file that exits first, say) is indeterminate, an
    ``ExternalError`` that quotes nothing it read; an outcome other than a
    completion raises its typed execution error.
    """
    nonce = new_nonce()
    return _reported_status(access, user_shell_probe(command, nonce=nonce, deadline=deadline), nonce)


def check_required_commands(
    commands: tuple[str, ...],
    access: VMAccess,
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    """Return the commands absent from the target user's login and interactive shell.

    Command lookup uses the same startup environment as user workloads. A
    result other than present or absent is indeterminate and raises, as
    :func:`run_user_shell_command` does.
    """
    missing: list[str] = []
    for command in commands:
        nonce = new_nonce()
        status = _reported_status(access, presence_probe(command, nonce=nonce, deadline=deadline), nonce)
        if status == _MISSING_STATUS:
            missing.append(command)
        elif status != 0:
            raise ExternalError("could not check required commands in the target user's shell")
    return tuple(missing)


def _reported_status(access: VMAccess, probe: Script, nonce: str) -> int:
    result = access.run(probe, stderr=DiscardOutput())
    result.require_completed(access.vm_name)
    status_line = re.compile(re.escape(f"{STATUS_PREFIX}{nonce}:").encode() + rb"(\d{1,3})")
    statuses = [match for line in result.stdout.splitlines() if (match := status_line.fullmatch(line))]
    if len(statuses) != 1 or int(statuses[0][1]) > 255:
        raise ExternalError("could not read a command's status in the target user's shell")
    return int(statuses[0][1])
