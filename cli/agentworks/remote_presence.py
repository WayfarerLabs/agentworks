"""Three-valued presence probes on a remote target.

A probe run with ``check=False`` exits nonzero both when it ran and answered
"absent" and when it never ran at all: ssh exits 255 for its own transport
failures, and a wrapper (``sudo``, a shell) has failure codes of its own. A
caller that reads ``.ok`` as "absent" therefore turns any of those into a
negative answer, and a guard that decides to create, delete, or overwrite on
that answer fails open. :func:`probe_presence` accepts exactly two answers,
present and the probe's own definite "absent" status, and raises on anything
else, so no caller acts on a probe that did not answer.

The tmux runtime probes in :mod:`agentworks.sessions.tmux` make the same
three-way split but return :class:`~agentworks.sessions.tmux.ProbeStatus`
``UNKNOWN`` instead of raising, because their callers each decide what an
unknown runtime state means. Use this module where an unknown answer must stop
the operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ExternalError
from agentworks.ssh import SSH_TRANSPORT_ERROR, SSHError

if TYPE_CHECKING:
    from agentworks.ssh import SSHResult
    from agentworks.transports import Transport

# ``test`` exits 1 when its expression is false (2 or more is its own error).
TEST_FALSE_EXIT = 1
# ``getent`` exits 2 when the key is not found in the database (1 is a usage
# or unknown-database error, 3 an unsupported enumeration).
GETENT_NOT_FOUND_EXIT = 2
# The absent status of :func:`sudo_safe_test`: one that neither ``sudo`` (1),
# ``bash -c`` (2, 126, 127), nor ssh (255) produces on its own.
SUDO_SAFE_ABSENT_EXIT = 3

# ``sudo -n`` exits 1 when it refuses to run the command.
_SUDO_REFUSAL_EXIT = 1
_STDERR_TAIL_LINES = 5


def sudo_safe_test(condition: str) -> str:
    """A probe command that exits 0 when ``condition`` holds and :data:`SUDO_SAFE_ABSENT_EXIT` when it does not.

    Use it for a ``test`` probe run under ``sudo=True``, whose own false status
    (1) a ``sudo -n`` refusal would also produce.
    """
    return f"if {condition}; then exit 0; else exit {SUDO_SAFE_ABSENT_EXIT}; fi"


def probe_presence(
    target: Transport,
    command: str,
    *,
    absent_exit: int,
    sudo: bool = False,
    timeout: int | None = None,
    vm_name: str | None = None,
    entity_kind: str | None = None,
    entity_name: str | None = None,
) -> SSHResult | None:
    """Run a read-only presence probe: its result when present, ``None`` when definitely absent.

    Exit 0 is present and ``absent_exit`` is the probe's own definite absent
    answer. Any other status raises: ssh's transport failure as
    :class:`SSHError`, every other unexpected status as :class:`ExternalError`,
    each with a short stderr tail and the caller's entity. Neither is a negative
    answer, so callers must let it propagate rather than treat it as absence.

    ``sudo=True`` with ``absent_exit=1`` is refused: ``sudo -n`` exits 1 when
    it refuses to run the command, so that absent answer would be a guess. Wrap
    a ``test`` probe in :func:`sudo_safe_test` instead.

    ``vm_name`` names the target in the error; without it the transport's own
    label does.
    """
    if sudo and absent_exit == _SUDO_REFUSAL_EXIT:
        raise ValueError("a sudo probe cannot use exit 1 as absent: a sudo -n refusal exits 1 too")
    result = target.run(command, sudo=sudo, check=False, timeout=timeout)
    if result.returncode == 0:
        return result
    if result.returncode == absent_exit:
        return None
    where = f"VM '{vm_name}'" if vm_name is not None else target.describe()
    stderr_tail = "\n".join(result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:])
    stderr_part = f"\nstderr: {stderr_tail}" if stderr_tail else ""
    if result.returncode == SSH_TRANSPORT_ERROR:
        raise SSHError(
            f"presence probe on {where} did not answer (ssh exit {SSH_TRANSPORT_ERROR}): {command}{stderr_part}",
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint=f"Check that {where} is reachable, then retry.",
        )
    raise ExternalError(
        f"presence probe on {where} did not answer (exit {result.returncode}): {command}{stderr_part}",
        entity_kind=entity_kind,
        entity_name=entity_name,
        hint=f"Run the probe on {where} by hand to see why it failed, then retry.",
    )
