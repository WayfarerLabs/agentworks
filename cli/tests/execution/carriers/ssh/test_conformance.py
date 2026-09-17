"""Shared preparation through SSH's process boundary, without a network target.

The executable fixture replaces the installed client with a POSIX-shell handoff.
It tests quoting, byte pumping and shared framing together, not authentication,
server compatibility or live SSH delivery.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection
from tests.execution.conformance import check_buffered_contract

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Shared bootstrap requires Linux userspace")


@pytest.fixture
def local_binding(tmp_path: Path) -> SSHConnection:
    executable = tmp_path / "ssh-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['-V']:\n"
        "    print('OpenSSH_9.2p1', file=sys.stderr)\n"
        "    raise SystemExit(0)\n"
        "os.execv('/bin/sh', ('sh', '-c', sys.argv[-1]))\n"
    )
    executable.chmod(0o700)
    identity = tmp_path / "identity"
    trust = tmp_path / "known hosts"
    identity.write_text("synthetic private identity, never read by this executable\n")
    trust.write_text("synthetic pinned trust, never read by this executable\n")
    return SSHConnection(
        host="fixture.invalid",
        user="fixture",
        identity_file=identity,
        known_hosts_file=trust,
        ssh_executable=str(executable),
    )


def test_shared_vectors_through_ssh_process_delivery(local_binding: SSHConnection) -> None:
    observations = check_buffered_contract(SSHCarrier(local_binding))
    ambiguous = next(row for row in observations if row.case == "exit-255")
    assert ambiguous.local_status == 255
    assert ambiguous.reported_exit is None
    assert ambiguous.streams_complete
    assert observations[-1].suppressed


def test_ssh_executes_in_fresh_process_without_legacy(local_binding: SSHConnection) -> None:
    script = r"""
import importlib.abc
import sys
from pathlib import Path

retired = (
    "agentworks.transports", "agentworks.ssh", "agentworks.remote_exec",
    "agentworks.harness_setup.runner", "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
)
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in retired):
            raise ImportError("Retired execution module is unavailable: " + fullname)

sys.meta_path.insert(0, BlockRetired())
from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection
from agentworks.execution.carrier import Deadline
from agentworks.execution.preparation import Command, prepare, decode_output

connection = SSHConnection(
    host="fixture.invalid", user="fixture", ssh_executable=sys.argv[1],
    identity_file=Path(sys.argv[2]), known_hosts_file=Path(sys.argv[3]),
)
prepared = prepare(Command(("/bin/cat",)), stdin=b"\x00\xff\r\n")
result = SSHCarrier(connection).execute(prepared.invocation, io=prepared.io, deadline=Deadline.after(10))
output = decode_output(prepared, result.stdout)
if result.failure is not None or result.completion is None or result.completion.code != 0:
    raise AssertionError("SSH fixture did not complete")
if output.stdout != b"\x00\xff\r\n" or not output.stdout_complete:
    raise AssertionError("SSH fixture did not preserve bytes")
if any(name in sys.modules for name in retired):
    raise AssertionError("SSH fixture loaded legacy execution")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            local_binding.ssh_executable,
            str(local_binding.identity_file),
            str(local_binding.known_hosts_file),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
