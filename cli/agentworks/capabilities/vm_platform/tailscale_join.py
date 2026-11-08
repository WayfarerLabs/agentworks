"""Joining a new VM to the tailnet and reading its address, through the platform's root channel.

Every platform runs these over its own ``run_as_root`` once its bootstrap
has installed Tailscale. The auth key travels only inside the join script,
as ``root_script``'s unexported ``AGW_SECRET``, and :data:`JOIN_FRAGMENT`
hands it to Tailscale on stdin, so it is in no argument on the workstation
or the guest. The address read and the logout carry no key, so retrying a
read never sends the key twice. :func:`log_out` is the one logout.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.base import parse_tailnet_address
from agentworks.capabilities.vm_platform.public_ssh import SudoRefused
from agentworks.errors import ConnectivityError, ProvisioningError
from agentworks.execution import root_script, ssh_process
from agentworks.execution.root_script import ChannelUnreachable

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform.public_ssh import PublicSSH
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result
    from agentworks.execution.root_script import RawRun, RootScriptResult

JOIN_FRAGMENT = """printf '%s\\n' "$AGW_SECRET" | tailscale up --auth-key=file:/dev/stdin"""
"""The one join command: the shell's ``printf`` builtin pipes the key to Tailscale, which reads the file."""

JOIN_DEADLINE_SECONDS = 30
JOIN_REASON_LINES = 5
"""How many of a failed join's last stderr lines its error quotes: Tailscale's own reason is one or two."""
ADDRESS_READS = 6
ADDRESS_PAUSE_SECONDS = 2
ADDRESS_READ_DEADLINE_SECONDS = 15
LOGOUT_DEADLINE_SECONDS = 30
READINESS_WAIT_SECONDS = 600
"""How long a cloud VM being created gets to answer over SSH: it boots, and its guest agent may install the key."""
READINESS_COMMAND_DEADLINE_SECONDS = 600

LOGOUT_SCRIPT = "\n".join(
    (
        "trap '' HUP PIPE",
        "s=0",
        "out=$(tailscale logout 2>&1) || s=$?",
        """printf '%s\\n' "$out" || true""",
        'exit "$s"',
    )
)
"""The one logout: a logout can take the network away from its own channel (Azure's did), so it ignores ``SIGHUP``
and ``SIGPIPE`` and writes nothing until ``tailscale logout`` is done, then reports its output and status."""

CREATE_RERUN_HINT = "The join spent the auth key: replace a single-use key before running the create again."
"""The settle's guidance after a create's node was logged out.

The platform's rollback reports for itself whether the backend was removed.
"""

DEFAULT_READINESS_COMMAND = "cloud-init status --wait"
DEFAULT_READINESS_LABEL = "cloud-init"
# ``cloud-init status --wait`` exits 2 when cloud-init finished with recoverable
# errors (a deprecated key, a module that warned): the VM came up, so a wait
# for it warns and carries on.
CLOUD_INIT_RECOVERABLE_STATUS = 2


def join_tailscale(run_as_root: Callable[[bytes, float], RawRun], auth_key: str, *, node: str) -> None:
    """Join the VM to the tailnet as ``node`` with ``auth_key``, exactly once.

    A non-zero status raises ``ProvisioningError`` quoting the last lines of
    ``tailscale up``'s stderr, its reason (an invalid auth key, say), with
    the key redacted as from every stream; the error reaches the create's
    operation log as its outcome. A channel that raised
    :class:`ChannelUnreachable` dispatched nothing, so the node did not join,
    and that raises with the channel's own hint. Any other outcome that
    cannot be established, another ``ConnectivityError`` included, raises
    naming ``node``, since the node may have joined. None retries.
    """
    try:
        result = root_script.run(run_as_root, JOIN_FRAGMENT, deadline=JOIN_DEADLINE_SECONDS, secret=auth_key)
    except ChannelUnreachable as error:
        raise ProvisioningError(f"node '{node}' did not join the tailnet: {error}", hint=error.hint) from error
    except ConnectivityError as error:
        raise _join_unknown(node) from error
    if result.status is None:
        raise _join_unknown(node)
    if result.status != 0:
        reason = ssh_process.tail_lines(result.stderr.decode("utf-8", errors="replace"), JOIN_REASON_LINES)
        raise ProvisioningError(
            f"'tailscale up' exited with status {result.status} on node '{node}'" + (f": {reason}" if reason else "")
        )


def _join_unknown(node: str) -> ProvisioningError:
    return ProvisioningError(
        f"the outcome of joining node '{node}' to the tailnet is unknown",
        hint=f"If '{node}' is listed in the Tailscale admin console, remove it there.",
    )


def read_tailnet_address(
    run_as_root: Callable[[bytes, float], RawRun],
    *,
    node: str,
    log: Callable[[RunRequest, Result], None] | None = None,
) -> str:
    """The VM's tailnet address from ``tailscale ip -4``, retrying a read that fails.

    A read fails on a non-zero status, an unknown outcome, a value
    :func:`parse_tailnet_address` refuses, or a ``ConnectivityError`` from
    the channel. Any other error propagates at once, since reading again
    cannot change it, and so does :class:`ChannelUnreachable`, whose channel
    has already waited for the VM. After :data:`ADDRESS_READS` failed reads
    this raises ``ConnectivityError`` naming ``node``. It never logs the node
    out.
    ``log`` records each read in the caller's operation log.
    """
    reason = ""
    cause: ConnectivityError | None = None
    for attempt in range(ADDRESS_READS):
        if attempt:
            time.sleep(ADDRESS_PAUSE_SECONDS)
        cause = None
        try:
            result = root_script.run(run_as_root, "tailscale ip -4", deadline=ADDRESS_READ_DEADLINE_SECONDS, log=log)
        except ChannelUnreachable:
            raise
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
            reason = "the VM reported a value that is not an assignable IPv4 address"
            continue
        output.detail(f"Tailscale IP: {address}")
        return address
    raise ConnectivityError(
        f"node '{node}' did not report its tailnet address in {ADDRESS_READS} reads ({reason})"
    ) from cause


def settle_tailnet_address(
    run_as_root: Callable[[bytes, float], RawRun],
    *,
    node: str,
    rerun_hint: str,
    log: Callable[[RunRequest, Result], None] | None = None,
) -> str:
    """The joined node's address, or the node logged out and ``ConnectivityError``.

    A node whose address cannot be read is unusable, and nothing else would
    log it out (after a create, core has not even enrolled its host key), so
    when the reads are exhausted :func:`log_out` runs once over the same
    channel. The error's hint is ``rerun_hint`` (``agw vm recover`` after a
    rekey or a recover, a fresh key before a create runs again); when the
    logout ran and failed, it first names ``node`` for removal in the
    Tailscale admin console. A :class:`ChannelUnreachable` read propagates at
    once with ``rerun_hint`` and no logout: nothing reaches the VM, so the
    node may well be healthy. ``log`` records the reads and the logout in the
    caller's operation log.
    """
    try:
        return read_tailnet_address(run_as_root, node=node, log=log)
    except ChannelUnreachable as error:
        error.add_hint(rerun_hint)
        raise
    except ConnectivityError as error:
        failure = error
    try:
        status = log_out(run_as_root, log=log).status
    except ConnectivityError:
        status = None
    if status == 0:
        raise ConnectivityError(f"{failure}; the node was logged out of the tailnet", hint=rerun_hint) from failure
    if status is None:
        raise ConnectivityError(f"{failure}, and the logout did not run to an outcome", hint=rerun_hint) from failure
    raise ConnectivityError(
        f"{failure}, and its logout exited with status {status}",
        hint=f"Remove '{node}' in the Tailscale admin console. {rerun_hint}",
    ) from failure


def log_out(
    run_as_root: Callable[[bytes, float], RawRun], *, log: Callable[[RunRequest, Result], None] | None = None
) -> RootScriptResult:
    """Run :data:`LOGOUT_SCRIPT` once, carrying no secret; the caller reads the status and reports it."""
    return root_script.run(run_as_root, LOGOUT_SCRIPT, deadline=LOGOUT_DEADLINE_SECONDS, log=log)


def complete_cloud_bootstrap(
    target: PublicSSH,
    auth_key: str,
    *,
    node: str,
    readiness_command: str = DEFAULT_READINESS_COMMAND,
    readiness_label: str = DEFAULT_READINESS_LABEL,
) -> str:
    """Finish a cloud VM's key-free bootstrap: wait for it, join as ``node`` once, and return the settled address.

    The wait is the cloud channel's own, for :data:`READINESS_WAIT_SECONDS`
    and retrying a refused key, since a cloud's guest agent may install the
    key after sshd starts. A readiness failure raises before the key is
    sent, so the platform's create rollback removes the partial VM with the
    key never used. The default readiness command's
    :data:`CLOUD_INIT_RECOVERABLE_STATUS` warns instead, as Proxmox's wait
    for cloud-init does.
    """
    output.detail(f"Waiting for {readiness_label} bootstrap to complete (this may take several minutes)...")
    try:
        target.wait_until_answering(seconds=READINESS_WAIT_SECONDS, retry_refused_key=True)
    except SudoRefused as error:
        # Not chained: the refusal's own hint (fix sudo on the VM, or recreate it) does not fit a VM the
        # create's rollback is about to remove.
        raise ProvisioningError(
            f"{error}, so create-time bootstrap cannot run",
            hint="The image's admin account must have passwordless sudo; fix the image, then create the VM again.",
        ) from None
    except ConnectivityError as error:
        # A refused key's hint (load an encrypted key into the agent) is the operator's next step.
        raise ProvisioningError("the VM did not answer over SSH for create-time bootstrap", hint=error.hint) from error

    incomplete = f"{readiness_label} did not complete successfully during create-time bootstrap"
    try:
        result = root_script.run(target.run_as_root, readiness_command, deadline=READINESS_COMMAND_DEADLINE_SECONDS)
    except ConnectivityError as error:
        raise ProvisioningError(incomplete, hint=error.hint) from error
    if result.status == CLOUD_INIT_RECOVERABLE_STATUS and readiness_command == DEFAULT_READINESS_COMMAND:
        output.warn(f"{readiness_label} completed with recoverable errors; continuing")
    elif result.status != 0:
        raise ProvisioningError(incomplete)
    join_tailscale(target.run_as_root, auth_key, node=node)
    return settle_tailnet_address(target.run_as_root, node=node, rerun_hint=CREATE_RERUN_HINT)
