"""Proxmox's root channel: the QEMU Guest Agent runs ``/bin/bash -s`` with the script as its input.

QGA always starts commands as root, so there is no elevation. The agent is
pinged every two seconds until it answers, since a VM that has just started
has none running yet; a ping carries no script, so asking again is safe. The
pinging stops at ``AGENT_WAIT_SECONDS`` or the script's deadline, whichever
comes first, so a VM with no agent at all fails in about two minutes. Then
one ``exec`` dispatches the script as ``input-data`` (limited to 65,536
characters), unless the pinging left none of the deadline, then
``exec-status`` is polled every two seconds until the guest reports an exit
or the deadline passes. Each request's wait is bounded by what remains of
the deadline, at most the API's ``REQUEST_TIMEOUT_SECONDS``. A poll carries only the PID, so
a failed one is asked again, since one missed answer says nothing of the
script; polls that keep failing for ``STATUS_RETRY_SECONDS`` give up (a
guest agent that restarted mid-script no longer knows the PID). QGA has no
cancellation endpoint: at the deadline the result is empty, so the outcome
is unknown, and nothing is dispatched again. No message holds the script,
because it can carry a secret: a failed dispatch, whose request carries the
script, says nothing of the provider's error, while a status failure, whose
request carries only the PID, quotes it, since it says why.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from agentworks.errors import AgentworksError, ConnectivityError, TokenRejectedError
from agentworks.execution import ssh_process
from agentworks.execution.root_script import ChannelUnreachable, RawRun
from agentworks.plugins.proxmox.api import REQUEST_TIMEOUT_SECONDS, ProxmoxAPIError, _TruncatedQGAOutput

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI

INPUT_DATA_LIMIT = 65_536
POLL_INTERVAL_SECONDS = 2.0
AGENT_WAIT_SECONDS = 120
STATUS_RETRY_SECONDS = 30
"""How long status polls may keep failing before the channel gives up on the script."""
_ROOT_SHELL = ["/bin/bash", "-s"]


def run_as_root(api: ProxmoxAPI, node: str, vmid: int, script: bytes, deadline: float) -> RawRun:
    """Run ``script`` through QGA once, with ``deadline`` seconds from now; see :func:`run_as_root_until`."""
    return run_as_root_until(api, node, vmid, script, time.monotonic() + deadline)


def run_as_root_until(api: ProxmoxAPI, node: str, vmid: int, script: bytes, expires: float) -> RawRun:
    """Run ``script`` through QGA once and return what the guest reported, or empty streams at ``expires``.

    ``expires`` is on ``time.monotonic()``'s clock, so a caller that has
    already spent part of its budget passes the same instant it started
    from, and no time passes uncounted between the two.

    A dispatch failure, or status polls failing for ``STATUS_RETRY_SECONDS``,
    raises ``ConnectivityError`` saying how far the request got, and never
    chains the original error, whose request could quote the script. A status
    failure from the API adds the API's own error text, since that request
    carries only the PID. The API refusing the site's token (401 or 403) is a
    ``TokenRejectedError`` instead, since no connection problem explains it.
    A guest agent that does not answer its ping in time to dispatch by the
    deadline raises ``ChannelUnreachable`` before anything is dispatched.
    """
    where = f"{vmid}@{node}"
    text = script.decode("utf-8")
    if len(text) > INPUT_DATA_LIMIT:
        raise ConnectivityError(f"a root script for Proxmox VM {where} exceeds QGA's {INPUT_DATA_LIMIT} characters")
    _await_agent(api, node, vmid, where, min(expires, time.monotonic() + AGENT_WAIT_SECONDS))
    remaining = expires - time.monotonic()
    if remaining <= 0:
        raise ChannelUnreachable(
            f"the QEMU guest agent of Proxmox VM {where} answered only as the deadline passed, so no script was"
            " dispatched",
            hint="Check that the Proxmox API is answering promptly, then run the command again.",
        )
    # Each failure is raised after its handler, so the original is not even the context.
    failure: AgentworksError | None = None
    try:
        pid = api.guest_agent_exec(
            node, vmid, command=list(_ROOT_SHELL), input_data=text, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining)
        )
    except Exception as error:
        failure = _token_rejected(
            error, f"Proxmox rejected the API token while dispatching a root script to VM {where}"
        ) or ConnectivityError(f"Proxmox QGA dispatch failed for VM {where}; the script may have started")
    if failure is not None:
        raise failure
    failing_since: float | None = None
    while True:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            return RawRun(b"", b"")
        status: dict[str, object] | None = None
        try:
            status = api.guest_agent_exec_status(node, vmid, pid=pid, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
        except _TruncatedQGAOutput:
            failure = ConnectivityError(
                f"Proxmox QGA reported that the script on VM {where} (PID {pid}) exited, with its output cut short"
            )
        except Exception as error:
            failing_since = time.monotonic() if failing_since is None else failing_since
            failure = _token_rejected(
                error,
                f"Proxmox rejected the API token while polling the root script on VM {where} (PID {pid});"
                " the script may still be running",
            )
            if failure is None and time.monotonic() - failing_since >= STATUS_RETRY_SECONDS:
                # The provider's text, shown as ssh's is: on a terminal, its last lines only.
                reason = f": {ssh_process.tail_lines(str(error))}" if isinstance(error, ProxmoxAPIError) else ""
                failure = ConnectivityError(
                    f"Proxmox QGA status failed for VM {where} (PID {pid}); the script may still be running{reason}"
                )
        if failure is not None:
            raise failure
        if status is not None:
            failing_since = None
            if cast("bool", status["exited"]):
                # The API's JSON can carry a lone surrogate, which UTF-8 cannot encode.
                return RawRun(
                    cast("str", status.get("out-data", "")).encode("utf-8", errors="replace"),
                    cast("str", status.get("err-data", "")).encode("utf-8", errors="replace"),
                )
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, expires - time.monotonic())))


def _await_agent(api: ProxmoxAPI, node: str, vmid: int, where: str, expires: float) -> None:
    """Ping the guest agent until it answers, or raise by ``expires`` with nothing dispatched.

    An API failure other than a token rejection means the agent is not
    answering yet; anything else the client raises is a bug and propagates.
    """
    while (remaining := expires - time.monotonic()) > 0:
        failure: AgentworksError | None = None
        try:
            api.guest_agent_ping(node, vmid, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
            return
        except ProxmoxAPIError as error:
            failure = _token_rejected(
                error, f"Proxmox rejected the API token while pinging the guest agent of VM {where}"
            )
        if failure is not None:
            raise failure
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, expires - time.monotonic())))
    raise ChannelUnreachable(
        f"the QEMU guest agent of Proxmox VM {where} did not answer, so no script was dispatched",
        hint=(
            "The VM needs qemu-guest-agent installed and running; check it from the VM's console in the Proxmox web UI."
        ),
    )


def _token_rejected(error: Exception, message: str) -> TokenRejectedError | None:
    """A ``TokenRejectedError`` saying ``message`` when ``error`` is the API's 401 or 403, else ``None``."""
    if isinstance(error, ProxmoxAPIError) and error.code in (401, 403):
        return TokenRejectedError(
            message,
            hint="Check token_id and the token secret's value and permissions on the Proxmox host.",
        )
    return None
