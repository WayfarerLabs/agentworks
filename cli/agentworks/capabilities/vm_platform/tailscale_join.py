"""Joining a new VM to the tailnet and reading its address, through the platform's root channel.

Every platform runs these over its own ``run_as_root`` once its bootstrap
has installed Tailscale. The auth key travels only inside the join script,
as ``root_script``'s unexported ``AGW_SECRET``, and :data:`JOIN_FRAGMENT`
hands it to Tailscale on stdin, so it is in no argument on the workstation
or the guest. The address read and the logout carry no key, so retrying a
read never sends the key twice.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.base import parse_tailnet_address
from agentworks.errors import ConnectivityError, ProvisioningError
from agentworks.execution import root_script

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.root_script import RawRun

JOIN_FRAGMENT = """printf '%s\\n' "$AGW_SECRET" | tailscale up --auth-key=file:/dev/stdin"""
"""The one join command: the shell's ``printf`` builtin pipes the key to Tailscale, which reads the file."""

JOIN_DEADLINE_SECONDS = 30
ADDRESS_READS = 6
ADDRESS_PAUSE_SECONDS = 2
ADDRESS_READ_DEADLINE_SECONDS = 15
LOGOUT_DEADLINE_SECONDS = 30
READINESS_PROBES = 30
READINESS_PAUSE_SECONDS = 10
READINESS_PROBE_DEADLINE_SECONDS = 10
READINESS_COMMAND_DEADLINE_SECONDS = 600

CREATE_RERUN_HINT = "The create can be run again."
"""The settle's guidance after a create's node was logged out: the backend was rolled back too."""

DEFAULT_READINESS_COMMAND = "cloud-init status --wait"
DEFAULT_READINESS_LABEL = "cloud-init"


def join_tailscale(run_as_root: Callable[[bytes, float], RawRun], auth_key: str, *, node: str) -> None:
    """Join the VM to the tailnet as ``node`` with ``auth_key``, exactly once.

    A non-zero status raises ``ProvisioningError``. An outcome that cannot
    be established, a channel that raised ``ConnectivityError`` included,
    raises naming ``node``, since the node may have joined. Neither retries.
    """
    try:
        result = root_script.run(run_as_root, JOIN_FRAGMENT, deadline=JOIN_DEADLINE_SECONDS, secret=auth_key)
    except ConnectivityError as error:
        raise _join_unknown(node) from error
    if result.status is None:
        raise _join_unknown(node)
    if result.status != 0:
        raise ProvisioningError(f"'tailscale up' exited with status {result.status} on node '{node}'")


def _join_unknown(node: str) -> ProvisioningError:
    return ProvisioningError(
        f"the outcome of joining node '{node}' to the tailnet is unknown; "
        f"if '{node}' is listed in the Tailscale admin console, remove it there"
    )


def read_tailnet_address(run_as_root: Callable[[bytes, float], RawRun], *, node: str) -> str:
    """The VM's tailnet address from ``tailscale ip -4``, retrying a read that fails.

    A read fails on a non-zero status, an unknown outcome, a value
    :func:`parse_tailnet_address` refuses, or a ``ConnectivityError`` from
    the channel. Any other error propagates at once, since reading again
    cannot change it. After :data:`ADDRESS_READS` failed reads this raises
    ``ConnectivityError`` naming ``node``. It never logs the node out.
    """
    reason = ""
    cause: ConnectivityError | None = None
    for attempt in range(ADDRESS_READS):
        if attempt:
            time.sleep(ADDRESS_PAUSE_SECONDS)
        cause = None
        try:
            result = root_script.run(run_as_root, "tailscale ip -4", deadline=ADDRESS_READ_DEADLINE_SECONDS)
        except ConnectivityError as error:
            cause, reason = error, "the platform channel failed"
            continue
        if result.status is None:
            reason = "the outcome of the read is unknown"
            continue
        if result.status != 0:
            reason = f"'tailscale ip -4' exited with status {result.status}"
            continue
        try:
            address = parse_tailnet_address(result.stdout.decode("utf-8", errors="replace"))
        except ValueError:
            reason = "the VM reported a value that is not an IP address"
            continue
        output.detail(f"Tailscale IP: {address}")
        return address
    raise ConnectivityError(
        f"node '{node}' joined the tailnet but did not report its address in {ADDRESS_READS} reads ({reason})"
    ) from cause


def settle_tailnet_address(run_as_root: Callable[[bytes, float], RawRun], *, node: str, rerun_hint: str) -> str:
    """The joined node's address, or the node logged out and ``ConnectivityError``.

    A node whose address cannot be read is unusable, and core never enrolled
    its host key, so nothing could log it out later: when the reads are
    exhausted, ``tailscale logout`` runs once over the same channel. The
    error then gives ``rerun_hint`` if the logout succeeded, and otherwise
    names ``node`` for removal in the Tailscale admin console. The guidance
    is in the message, since a create's caller wraps this error.
    """
    try:
        return read_tailnet_address(run_as_root, node=node)
    except ConnectivityError as error:
        failure = error
    if _log_out(run_as_root):
        message = f"{failure}; the node was logged out of the tailnet. {rerun_hint}"
    else:
        message = f"{failure}, and it could not be logged out; remove '{node}' in the Tailscale admin console"
    raise ConnectivityError(message) from failure


def _log_out(run_as_root: Callable[[bytes, float], RawRun]) -> bool:
    """Whether ``tailscale logout`` completed with status 0; a raise counts as no."""
    try:
        result = root_script.run(run_as_root, "tailscale logout", deadline=LOGOUT_DEADLINE_SECONDS)
    except ConnectivityError:
        return False
    return result.status == 0


def complete_cloud_bootstrap(
    run_as_root: Callable[[bytes, float], RawRun],
    auth_key: str,
    *,
    node: str,
    readiness_command: str = DEFAULT_READINESS_COMMAND,
    readiness_label: str = DEFAULT_READINESS_LABEL,
) -> str:
    """Finish a cloud VM's key-free bootstrap: wait for it, join as ``node`` once, and return the settled address.

    A readiness failure raises before the key is sent, so the platform's
    create rollback removes the partial VM with the key never used.
    """
    _wait_for_readiness(run_as_root, readiness_command, readiness_label)
    join_tailscale(run_as_root, auth_key, node=node)
    return settle_tailnet_address(run_as_root, node=node, rerun_hint=CREATE_RERUN_HINT)


def _wait_for_readiness(run_as_root: Callable[[bytes, float], RawRun], command: str, label: str) -> None:
    output.detail(f"Waiting for {label} bootstrap to complete (this may take several minutes)...")
    failure: ConnectivityError | None = None
    for attempt in range(READINESS_PROBES):
        if attempt:
            time.sleep(READINESS_PAUSE_SECONDS)
        failure = None
        try:
            probe = root_script.run(run_as_root, "true", deadline=READINESS_PROBE_DEADLINE_SECONDS)
        except ConnectivityError as error:
            # A refused key included: a cloud's guest agent may install the key after sshd starts.
            failure = error
            continue
        if probe.status == 0:
            break
    else:
        raise ProvisioningError("the VM did not answer over SSH for create-time bootstrap") from failure

    incomplete = ProvisioningError(f"{label} did not complete successfully during create-time bootstrap")
    try:
        result = root_script.run(run_as_root, command, deadline=READINESS_COMMAND_DEADLINE_SECONDS)
    except ConnectivityError as error:
        raise incomplete from error
    if result.status != 0:
        raise incomplete
