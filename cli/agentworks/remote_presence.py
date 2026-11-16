"""Three-valued presence probes on a remote target.

A probe run with ``check=False`` exits nonzero both when it ran and answered
"absent" and when it never ran at all: ssh exits 255 for its own transport
failures, and a wrapper (``sudo``, a shell) has failure codes of its own. A
caller that reads ``.ok`` as "absent" therefore turns any of those into a
negative answer, and a guard that decides to create, delete, or overwrite on
that answer fails open. :func:`probe_presence` accepts exactly two answers,
present and the probe's own definite "absent" status, and raises on anything
else, so no caller acts on a probe that did not answer.
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


def probe_presence(
    target: Transport,
    command: str,
    *,
    absent_exit: int,
    vm_name: str,
    sudo: bool = False,
    timeout: int | None = None,
) -> SSHResult | None:
    """Run a read-only presence probe: its result when present, ``None`` when definitely absent.

    Exit 0 is present and ``absent_exit`` is the probe's own definite absent
    answer. Any other status raises: ssh's transport failure as
    :class:`SSHError`, every other unexpected status as :class:`ExternalError`.
    Neither is a negative answer, so callers must let it propagate rather than
    treat it as absence.
    """
    result = target.run(command, sudo=sudo, check=False, timeout=timeout)
    if result.returncode == 0:
        return result
    if result.returncode == absent_exit:
        return None
    if result.returncode == SSH_TRANSPORT_ERROR:
        raise SSHError(
            f"presence probe on VM '{vm_name}' did not run (ssh exit {SSH_TRANSPORT_ERROR}): {command}",
            hint=f"Check that VM '{vm_name}' is reachable, then retry.",
        )
    raise ExternalError(
        f"presence probe on VM '{vm_name}' did not answer (exit {result.returncode}): {command}",
        hint=f"Run the probe on VM '{vm_name}' by hand to see why it failed, then retry.",
    )
