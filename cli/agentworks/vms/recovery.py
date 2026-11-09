"""Core's recovery scripts, run as root through the platform's own channel.

Each of the two scripts has one function that runs it once with
``root_script.run`` over ``platform.run_as_root``, so it reaches the VM
without its Tailscale or, on a platform whose channel is not sshd, its SSH
server:

- :func:`repair`, ``agw vm recover``'s one script: rewrite the admin's
  ``authorized_keys`` as reinit does, restart a broken sshd, install python3 when agw-launch's
  interpreter does not run, start ``tailscaled`` if it is not running and
  wait for its state to settle, and join Tailscale only when the node needs
  a login.
- :func:`rekey`, ``vm rekey``'s logout and rejoin with a new auth key.

``vm delete``'s logout is ``tailscale_join.log_out``, the one logout.

A script that finds a problem prints its reason on stderr and exits 1, so
core reads its status only as zero or not and the reason reaches the
operation log, never an error. The auth key is ``root_script``'s one secret,
handed to Tailscale by ``JOIN_FRAGMENT`` on stdin.

Rekey can take the network away from its own channel, so it ignores
``SIGHUP`` and ``SIGPIPE`` and writes nothing until it is done: each
step's output is captured with its status, the first failure stops the
sequence, and the buffer is printed once at the end with ``|| true``. A
channel that drops mid-script therefore leaves the remaining steps running
on the guest, and the outcome unknown.
"""

from __future__ import annotations

import shlex
from functools import partial
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution import root_script, ssh_process

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

JOINED = "joined"
"""The line the repair prints on stdout after it joined the tailnet."""

BACKEND_STATE_FRAGMENT = (
    """tailscale status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["BackendState"])'"""
)
"""The command that prints the node's ``BackendState``: ``Running``, or ``NeedsLogin`` once the node is gone."""

SETTLING_STATE_READS = 15
SETTLING_STATE_INTERVAL_SECONDS = 2
"""How often, and how far apart, the repair reads a ``BackendState`` that is still settling: about 30 seconds."""

STABILIZE_SECONDS = 15
"""Rekey's pause after each step, for the daemon and the network to settle: without it a rekeyed node could be
unreachable from its peers."""

_REPAIR = """\
set -eu
# A root channel's PATH can lack the sbin directories, and dpkg refuses to
# install a package unless ldconfig and start-stop-daemon are on it; sshd
# lives there too.
PATH="$PATH:/usr/sbin:/sbin"
admin={admin}

entry=$(getent passwd "$admin") || {{ echo "the admin account '$admin' does not exist" >&2; exit 1; }}
home=$(printf '%s\\n' "$entry" | cut -d: -f6)
keys="$home/.ssh/authorized_keys"
mkdir -p "$home/.ssh"
# The whole managed file, as reinit writes it, so a key it no longer lists
# (one the configured key replaced) stops working now, not at the next reinit.
(umask 077 && printf '%s' {authorized_keys} > "$keys.agw-recover")
mv -f "$keys.agw-recover" "$keys"
chown -R "$admin:" "$home/.ssh"
chmod 700 "$home/.ssh"
chmod 600 "$keys"

# Before the package install, which can take minutes: a VM whose sshd is
# broken gets its SSH access back first. This runs on every channel, sshd's
# own included: a channel that runs this script can still face a stopped,
# masked or misconfigured sshd (Lima's persistent connection outlives the
# daemon, and a running daemon does not reread its configuration), and
# restarting ssh.service, whose KillMode is process, ends no session. The
# unit creates sshd's privilege separation directory; a stopped or masked
# one may have left none, and the configuration test needs it.
install -d -m 0755 /run/sshd
sshd -t || {{ echo "sshd's configuration test failed" >&2; exit 1; }}
systemctl unmask ssh.service
systemctl enable ssh.service
# A unit that crash-looped on a bad configuration hit its start limit, and
# systemd refuses to restart it until that is cleared.
systemctl reset-failed ssh.service
systemctl restart ssh.service

# The interpreter agw-launch runs is what is tested, not the package: dpkg
# still reports a removed but not purged python3, and python3-minimal alone
# runs but has no json. Debian's python3 package depends on its standard
# library. Package output goes to stderr, which the operation log keeps, so
# stdout carries only the joined line. The lock timeout waits out an
# unattended upgrade instead of failing.
python_runs() {{ {python} -I -S -c 'import json' >/dev/null 2>&1; }}
if ! python_runs; then
    apt-get -o DPkg::Lock::Timeout=120 update >&2
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y python3 >&2
    python_runs || {{ echo "{python} does not run with its standard library after installing python3" >&2; exit 1; }}
fi

command -v tailscale >/dev/null || {{ echo "Tailscale is not installed" >&2; exit 1; }}
systemctl is-active --quiet tailscaled || systemctl start tailscaled
# A daemon that has just started reads NoState, then Starting, before it
# settles on Running or NeedsLogin, so those two are read again, a bounded
# number of times.
reads=0
while :; do
    state=$({backend_state}) || {{ echo "Tailscale's state could not be read" >&2; exit 1; }}
    case "$state" in NoState|Starting) ;; *) break ;; esac
    reads=$((reads + 1))
    [ "$reads" -lt {state_reads} ] || break
    sleep {state_interval}
done
case "$state" in
    Running) ;;
    NeedsLogin)
        {join}
        echo {joined}
        ;;
    *) echo "Tailscale is in state '$state', which recovery does not repair" >&2; exit 1 ;;
esac
"""

# Rekey's prologue: nothing reaches the channel until ``finish``.
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
step timeout 60 tailscale logout || finish
sleep {pause}
step join_tailnet || finish
sleep {pause}
step systemctl restart tailscaled || finish
sleep {pause}
finish
"""


def repair(
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    *,
    authorized_keys: str,
    auth_key: str,
    log: Callable[[RunRequest, Result], None] | None,
) -> RootScriptResult:
    """Run the repair script once; :func:`joined` says whether it joined the tailnet.

    ``authorized_keys`` is the admin's whole managed file
    (``ssh_keys.authorized_keys_content``). The sshd repair runs on
    every platform: that this script runs at all says the channel works now,
    not that sshd will accept the next connection or survive the next boot.
    """
    payload = _REPAIR.format(
        admin=shlex.quote(vm.admin_username),
        authorized_keys=shlex.quote(authorized_keys),
        python=shlex.quote(ssh_process.DEFAULT_PYTHON),
        backend_state=BACKEND_STATE_FRAGMENT,
        state_reads=SETTLING_STATE_READS,
        state_interval=SETTLING_STATE_INTERVAL_SECONDS,
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
    rekeyed node that peers cannot reach; :data:`STABILIZE_SECONDS` after
    each step lets the daemon and the network settle.
    """
    payload = _CAPTURED + _REKEY_STEPS.format(join=JOIN_FRAGMENT, pause=STABILIZE_SECONDS)
    return root_script.run(
        partial(platform.run_as_root, vm, ctx), payload, deadline=REKEY_DEADLINE_SECONDS, secret=auth_key, log=log
    )


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
