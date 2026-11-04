"""Core's recovery scripts, run as root through the platform's own channel.

Each of the three scripts has one function that runs it once with
``root_script.run`` over ``platform.run_as_root``, so it reaches the VM
without its Tailscale or, on a platform whose channel is not sshd, its SSH
server:

- :func:`repair`, ``agw vm recover``'s one script: restore the admin's
  authorized key, install python3, restart a broken sshd where the channel
  does not depend on it, and join Tailscale only when the node needs a login.
- :func:`rekey`, ``vm rekey``'s logout and rejoin with a new auth key.
- :func:`logout`, ``vm delete``'s Tailscale logout.

A script that finds a problem prints its reason on stderr and exits 1, so
core reads its status only as zero or not and the reason reaches the
operation log, never an error. The auth key is ``root_script``'s one secret,
handed to Tailscale by ``JOIN_FRAGMENT`` on stdin.

Rekey and logout can take the network away from their own channel, so they
ignore ``SIGHUP`` and ``SIGPIPE`` and write nothing until they are done: each
step's output is captured with its status, the first failure stops the
sequence, and the buffer is printed once at the end with ``|| true``. A
channel that drops mid-script therefore leaves the remaining steps running
on the guest, and the outcome unknown.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import shlex
from functools import partial
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution import root_script

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import VMRow
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result
    from agentworks.execution.root_script import RootScriptResult

REPAIR_DEADLINE_SECONDS = 600
REKEY_DEADLINE_SECONDS = 180
LOGOUT_DEADLINE_SECONDS = 30

JOINED = "joined"
"""The line the repair prints on stdout after it joined the tailnet."""

BACKEND_STATE_FRAGMENT = (
    """tailscale status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["BackendState"])'"""
)
"""The command that prints the node's ``BackendState``: ``Running``, or ``NeedsLogin`` once the node is gone."""

STABILIZE_SECONDS = 15
"""Rekey's pause after each step, for the daemon and the network to settle (incident row I29)."""

_REPAIR = """\
set -eu
PATH="$PATH:/usr/sbin:/sbin"
admin={admin}
key={key}
repair_sshd={repair_sshd}

entry=$(getent passwd "$admin") || {{ echo "the admin account '$admin' does not exist" >&2; exit 1; }}
home=$(printf '%s\\n' "$entry" | cut -d: -f6)
mkdir -p "$home/.ssh"
grep -qxF "$key" "$home/.ssh/authorized_keys" 2>/dev/null || printf '%s\\n' "$key" >> "$home/.ssh/authorized_keys"
chown -R "$admin:" "$home/.ssh"
chmod 700 "$home/.ssh"
chmod 600 "$home/.ssh/authorized_keys"

# Debian's python3 package depends on its standard library, so this also
# catches a VM with only python3-minimal. Package output goes to stderr,
# which the operation log keeps, so stdout carries only the joined line.
if ! dpkg -s python3 >/dev/null 2>&1; then
    apt-get update >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 >&2
fi

if [ "$repair_sshd" = 1 ]; then
    # The unit creates sshd's privilege separation directory; a stopped or
    # masked one may have left none, and the configuration test needs it.
    install -d -m 0755 /run/sshd
    sshd -t || {{ echo "sshd's configuration test failed" >&2; exit 1; }}
    systemctl unmask ssh.service
    systemctl enable ssh.service
    systemctl restart ssh.service
fi

command -v tailscale >/dev/null || {{ echo "Tailscale is not installed" >&2; exit 1; }}
state=$({backend_state}) || {{ echo "Tailscale's state could not be read" >&2; exit 1; }}
case "$state" in
    Running) ;;
    NeedsLogin)
        {join}
        echo {joined}
        ;;
    *) echo "Tailscale is in state '$state', which recovery does not repair" >&2; exit 1 ;;
esac
"""

# The prologue rekey and logout share: nothing reaches the channel until ``finish``.
_CAPTURED = """\
trap '' HUP PIPE
set -eu
buffer=''
status=0
step() {
    out=$("$@" 2>&1) || status=$?
    buffer="$buffer$*: $out
"
    [ "$status" -eq 0 ]
}
finish() {
    printf '%s' "$buffer" || true
    exit "$status"
}
"""

_REKEY_STEPS = """\
join_tailnet() {{
    {join}
}}
step systemctl restart tailscaled || finish
sleep {pause}
step tailscale logout || finish
sleep {pause}
step join_tailnet || finish
sleep {pause}
step systemctl restart tailscaled || finish
sleep {pause}
finish
"""

_LOGOUT = (
    _CAPTURED
    + """\
step tailscale logout || finish
finish
"""
)


def repair(
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    *,
    public_key: str,
    auth_key: str,
    log: Callable[[RunRequest, Result], None] | None,
) -> RootScriptResult:
    """Run the repair script once; :func:`joined` says whether it joined the tailnet.

    ``public_key`` is the configured key's one line. The sshd repair runs only
    where the platform's channel does not go through the guest's sshd: where
    it does, a broken sshd means nothing could run.
    """
    payload = _REPAIR.format(
        admin=shlex.quote(vm.admin_username),
        key=shlex.quote(public_key),
        repair_sshd=0 if platform.recovery_needs_guest_sshd else 1,
        backend_state=BACKEND_STATE_FRAGMENT,
        join=JOIN_FRAGMENT,
        joined=JOINED,
    )
    return root_script.run(
        partial(platform.run_as_root, vm, ctx), payload, deadline=REPAIR_DEADLINE_SECONDS, secret=auth_key, log=log
    )


def joined(result: RootScriptResult) -> bool:
    """Whether a repair that exited 0 joined the tailnet."""
    return JOINED in result.stdout.decode("utf-8", errors="replace").splitlines()


def rekey(
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    *,
    auth_key: str,
    log: Callable[[RunRequest, Result], None] | None,
) -> RootScriptResult:
    """Run the rekey script once: restart ``tailscaled``, log out, join with ``auth_key``, restart.

    The restarts clear a stale daemon that would hang the logout, and a
    rekeyed node that peers cannot reach (incident row I29).
    """
    payload = _CAPTURED + _REKEY_STEPS.format(join=JOIN_FRAGMENT, pause=STABILIZE_SECONDS)
    return root_script.run(
        partial(platform.run_as_root, vm, ctx), payload, deadline=REKEY_DEADLINE_SECONDS, secret=auth_key, log=log
    )


def logout(platform: VMPlatform, vm: VMRow, ctx: RunContext) -> RootScriptResult:
    """Run the logout script once, with no operation log: it carries no secret and delete reports it."""
    return root_script.run(partial(platform.run_as_root, vm, ctx), _LOGOUT, deadline=LOGOUT_DEADLINE_SECONDS)


def require_success(result: RootScriptResult, *, script: str, vm_name: str, hint: str) -> None:
    """Raise unless the script exited 0: ``StateError`` for a status, ``ConnectivityError`` for none.

    The script's reason is in the operation log, never the error: ``hint``
    names the log and the next step.
    """
    if result.status == 0:
        return
    if result.status is None:
        raise ConnectivityError(
            f"the outcome of the {script} script on VM '{vm_name}' is unknown",
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
        )
    raise StateError(
        f"the {script} script on VM '{vm_name}' exited with status {result.status}",
        entity_kind="vm",
        entity_name=vm_name,
        hint=hint,
    )
