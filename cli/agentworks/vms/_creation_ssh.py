"""Retained SSH listening and public prerequisite observations for VM creation."""

from __future__ import annotations

import math
import socket
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.errors import StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_identity_argv
from agentworks.execution.carrier import Capture, CarrierIO, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.vms.identity import VM_INSTANCE_MARKER_PATH

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh import SSHConnection

CLOUD_INIT_CHECK = r"""/usr/bin/cloud-init status --wait >/dev/null 2>&1
[ "$(/usr/bin/cloud-init status)" = 'status: done' ]"""


def _remaining(deadline: Deadline) -> float:
    remaining = deadline.remaining()
    if remaining is None:
        raise ValidationError("Creation SSH readiness requires a finite deadline")
    if remaining <= 0:
        raise TimeoutError("Creation SSH readiness deadline expired")
    return remaining


class SSHReadiness:
    """Retain each TCP original before connecting to one selected literal IPv4.

    Listening proves neither authentication nor prerequisites. The caller owns
    route/operation admission, enrolls once, then performs strict observation.
    """

    def __init__(self) -> None:
        self._socket: socket.socket | None = None

    def close(self) -> bool:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            else:
                self._socket = None
        return self._socket is None

    def wait(self, endpoint: str, deadline: Deadline) -> None:
        if self._socket is not None:
            raise StateError("Creation SSH listening probe retains its original socket")
        while True:
            remaining = _remaining(deadline)
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            control: BaseException | None = None
            connected = False
            try:
                self._socket.settimeout(min(1.0, remaining))
                connected = self._socket.connect_ex((endpoint, 22)) == 0
            except OSError:
                pass
            except BaseException as error:
                control = error
                raise
            finally:
                try:
                    closed = self.close()
                except BaseException:
                    if control is not None:
                        raise control from None
                    raise
            if not closed:
                raise StateError("Creation SSH listening probe retains its original socket")
            remaining = _remaining(deadline)
            if connected:
                return
            time.sleep(min(0.1, remaining))


def observe_early_guest(
    connection: SSHConnection,
    marker: str,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
    *,
    bootstrap_check: str,
) -> None:
    """Observe prerequisites through strict SSH before Python helpers can run.

    bootstrap_check is fixed, public, provider-owned shell source, not operator
    input. The provider retains admission, route and trust custody. A remote
    timeout bounds the read-only check even if local SSH observation is lost;
    it does not prove descendant cleanup or authorize resource acceptance.
    """
    remaining = _remaining(deadline)
    nonce = uuid4().hex
    source = (
        "set -euo pipefail\n"
        + bootstrap_check
        + "\n"
        + r"""
[ "$(id -u)" = 0 ]
[ -x /usr/bin/sudo ] && [ -x /usr/bin/setpriv ]
[ ! -L /tmp ] && [ "$(stat -c '%u:%g:%a' /tmp)" = 0:0:1777 ]
[ "$(cat "$1")" = "$2" ]
/usr/bin/python3 -I -S -B -c 'import base64,bz2,hashlib,json,os,sys,types; assert sys.version_info >= (3,11)'
printf '%s\n' "$3"
"""
    )
    root_entry = IdentityPlan(
        IdentityExpectation(0, 0, (0,)),
        IdentityMode.DIRECT if connection.user == "root" else IdentityMode.SUDO_ROOT,
    )
    invocation = PreparedInvocation(
        build_identity_argv(
            root_entry,
            (
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "LANG=C",
                "LC_ALL=C",
                "/usr/bin/timeout",
                "--kill-after=5s",
                str(math.ceil(remaining)) + "s",
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                source,
                "agentworks-creation-early-guest",
                VM_INSTANCE_MARKER_PATH,
                marker,
                nonce,
            ),
        )
    )
    report = SSHCarrier(connection).execute(
        invocation, io=CarrierIO(output=Capture(512)), deadline=deadline, custody=custody
    )
    if (
        deadline.expired
        or not custody.settled
        or report.dispatch is not Dispatch.SENT
        or report.completion != ExitStatus(code=0)
        or report.failure is not None
        or not report.stdout.complete
        or report.stdout.data != (nonce + "\n").encode("ascii")
    ):
        raise StateError("Created guest prerequisites remain unproved")
