"""Wait until a VM answers over SSH.

:func:`wait` polls one fixed, read-only probe through a :class:`VMAccess`, and
only while the failure is one that waiting can fix: the connection failed, was
cut short or timed out. Anything else is raised at once, so a changed host key
or a refused key is neither waited out nor repeated against the VM's sshd
(Debian 13's ``PerSourcePenalties`` penalizes repeated authentication
failures). The caller owns what an exhausted wait means: whether to warn and
proceed, or to fail.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command
from agentworks.execution.results import Unknown, UnknownReason

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess

# About 235 to 240 seconds in all when every attempt fails, the bound the
# legacy wait kept (incident row I15).
ATTEMPTS = 16
PROBE_DEADLINE = 10
PAUSE_SECONDS = 5
CONFIRM_PAUSE_SECONDS = 2

PROBE = Command(("true",), deadline=PROBE_DEADLINE)

POLLED = (Unknown(UnknownReason.CONNECTION), Unknown(UnknownReason.TRUNCATED), Unknown(UnknownReason.DEADLINE))
"""The outcomes that mean "not yet"; every other one is raised."""


class Wording(Enum):
    """What the operator is told while waiting and once the VM answers.

    ``RECONNECT`` is for a VM whose network just changed, where a wait of
    minutes is possible; ``VERIFY`` is for confirming a VM that was never
    known to be down.
    """

    RECONNECT = (
        "Waiting for Tailscale to reconnect (this may take several minutes)...",
        "Tailscale SSH reconnected",
    )
    VERIFY = (
        "Verifying Tailscale connectivity...",
        "Tailscale SSH reachable",
    )

    @property
    def waiting_message(self) -> str:
        return self.value[0]

    @property
    def success_message(self) -> str:
        return self.value[1]


def wait(access: VMAccess, *, wording: Wording) -> bool:
    """Whether the VM answered and held, polling up to :data:`ATTEMPTS` times.

    An answer on the first attempt returns at once. One after a failed attempt
    is confirmed by a second probe after :data:`CONFIRM_PAUSE_SECONDS`, since a
    path that just came up can flap; a failed confirmation is one more failed
    attempt. Failed attempts are :data:`PAUSE_SECONDS` apart. Returns ``False``
    when the attempts run out. Errors raised before dispatch (``TrustMissing``,
    ``ClientUnsupported``) propagate from the first attempt, and so does every
    outcome outside :data:`POLLED`, with its typed error.
    """
    output.detail(wording.waiting_message)
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(PAUSE_SECONDS)
        if not _answered(access):
            continue
        if attempt:
            time.sleep(CONFIRM_PAUSE_SECONDS)
            if not _answered(access):
                continue
        output.detail(wording.success_message)
        return True
    return False


def _answered(access: VMAccess) -> bool:
    """One probe: ``True`` when it completed with 0, ``False`` for a polled outcome; anything else raises."""
    try:
        access.run(PROBE, check=True)
    except ExecutionIncomplete as error:
        if error.result.completion in POLLED:
            return False
        raise
    return True
