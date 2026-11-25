"""Three-valued presence probes on a VM.

A probe's ``Completed`` status is the payload's own: an outcome that is not a
completion (a request that never started, ssh's own failure, a ``sudo -n``
refusal in front of the launcher) already raises from
:meth:`~agentworks.execution.results.Result.require_completed`. The payload can
still exit with a status that is no answer at all, such as ``getent``'s 1 for a
usage or database error or ``test``'s 2 for its own error. A caller that reads
every nonzero status as "absent" turns those into a negative answer, and a
guard that decides to create, delete, or overwrite on that answer fails open.
:func:`probe_presence` accepts exactly two answers, present and the probe's own
definite "absent" status, and raises on anything else, so no caller acts on a
probe that did not answer.

The tmux runtime probes in :mod:`agentworks.sessions.tmux` make the same
three-way split but return :class:`~agentworks.sessions.tmux.ProbeStatus`
``UNKNOWN`` instead of raising, because their callers each decide what an
unknown runtime state means. Use this module where an unknown answer must stop
the operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution.errors import CommandFailed

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result

# ``test`` exits 1 when its expression is false (2 or more is its own error).
TEST_FALSE_STATUS = 1
# ``getent`` exits 2 when the key is not found in the database (1 is a usage
# or unknown-database error, 3 an unsupported enumeration).
GETENT_NOT_FOUND_STATUS = 2


def probe_presence(access: VMAccess, request: RunRequest, *, absent_status: int) -> Result | None:
    """Run a read-only presence probe: its result when present, ``None`` when definitely absent.

    Status 0 is present and ``absent_status`` is the probe's own definite
    absent answer. Any other status is :class:`CommandFailed`, and an outcome
    that is not a completion raises its typed error. Neither is a negative
    answer, so callers must let it propagate rather than treat it as absence.
    """
    result = access.run(request)
    status = result.require_completed(access.vm_name)
    if status == 0:
        return result
    if status == absent_status:
        return None
    raise CommandFailed(access.vm_name, status, result, streams_logged=request.log_streams)
