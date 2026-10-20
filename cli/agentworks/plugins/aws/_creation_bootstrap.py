"""Public early cloud-init and bounded independent SSH readiness observations."""

from __future__ import annotations

import gzip
import socket
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml

from agentworks.capabilities.vm_platform.bootstrap_script import generate_instance_marker_installer
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Capture, CarrierIO, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.plugins.aws.config import _generate_ec2_user_data
from agentworks.vms.identity import VM_INSTANCE_MARKER_PATH

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh import SSHConnection


def early_user_data(*, admin: str, public_key: str, hostname: str, marker: str) -> bytes:
    """Install only public prerequisites before core's guest configuration."""
    config = yaml.safe_load(_generate_ec2_user_data(
        admin_username=admin, ssh_public_key=public_key, hostname=hostname, bootstrap_script=None
    ))
    config["packages"] = ["python3", "sudo", "util-linux"]
    config["runcmd"] = [[
        "/bin/bash", "--noprofile", "--norc", "-c",
        "set -euo pipefail\n" + generate_instance_marker_installer(instance_marker=marker),
    ]]
    payload = gzip.compress(("#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)).encode("utf-8"), mtime=0)
    if len(payload) > 16384:
        raise ValidationError("EC2 early cloud-init exceeds the raw user-data limit")
    return payload


class SSHReadiness:
    """Retain each actual TCP original before connecting to one literal endpoint.

    Listening proves neither SSH authentication nor guest readiness. The caller
    enrolls once, then uses strict SSH to observe cloud-init and prerequisites.
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
            raise StateError("EC2 SSH listening probe retains its original socket")
        while not deadline.expired:
            remaining = deadline.remaining()
            assert remaining is not None
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
                raise StateError("EC2 SSH listening probe retains its original socket")
            if connected and not deadline.expired:
                return
            remaining = deadline.remaining()
            assert remaining is not None
            time.sleep(min(0.1, remaining))
        raise TimeoutError("EC2 SSH listening deadline expired")


def observe_early_guest(
    connection: SSHConnection, marker: str, deadline: Deadline, custody: LocalDeliveryCustody,
) -> None:
    """One read-only shell gate, after enrollment and before Python helpers."""
    nonce = uuid4().hex
    # Cloud-init may still be installing the helper runtime when sshd listens.
    source = r"""set -euo pipefail
/usr/bin/cloud-init status --wait >/dev/null 2>&1
[ "$(/usr/bin/cloud-init status)" = 'status: done' ]
[ -x /usr/bin/sudo ] && [ -x /usr/bin/setpriv ]
[ ! -L /tmp ] && [ "$(stat -c '%u:%g:%a' /tmp)" = 0:0:1777 ]
[ "$(cat "$1")" = "$2" ]
/usr/bin/python3 -I -S -B -c 'import base64,bz2,hashlib,json,os,sys,types; assert sys.version_info >= (3,11)'
printf '%s\n' "$3"
"""
    invocation = PreparedInvocation((
        "/usr/bin/sudo", "-n", "--user=#0", "--", "/usr/bin/env", "-i",
        "PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", "/bin/bash", "--noprofile", "--norc", "-c",
        source, "agentworks-aws-early-guest", VM_INSTANCE_MARKER_PATH, marker, nonce,
    ))
    report = SSHCarrier(connection).execute(
        invocation, io=CarrierIO(output=Capture(512)), deadline=deadline, custody=custody
    )
    if (
        deadline.expired or not custody.settled or report.completion != ExitStatus(code=0)
        or report.failure is not None or not report.stdout.complete
        or report.stdout.data != (nonce + "\n").encode("ascii")
    ):
        raise StateError("EC2 early guest prerequisites remain unproved")
