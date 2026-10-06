"""Shared preparation through synthetic and installed SSH process boundaries.

The executable fixture replaces the installed client with a POSIX-shell handoff.
It tests quoting, byte pumping and shared framing together, not authentication,
server compatibility or live SSH delivery. The integration-marked environment
case additionally uses the fixture-owned loopback sshd.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_control import WaitKind
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Provenance,
)
from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection
from agentworks.execution.carriers.ssh.connection import build_ssh_argv
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
from agentworks.execution.models import Command
from tests.execution._bound_carrier_support import FixtureBoundCarrier
from tests.execution.conformance import check_buffered_contract

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Shared bootstrap requires Linux userspace")


@pytest.fixture
def local_binding(tmp_path: Path) -> SSHConnection:
    tmp_path = tmp_path.resolve()
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
        trust=SSHTrustFiles((trust,)),
        ssh_executable=str(executable),
    )


def test_shared_vectors_through_ssh_process_delivery(local_binding: SSHConnection) -> None:
    observations = check_buffered_contract(SSHCarrier(local_binding))
    ambiguous = next(row for row in observations if row.case == "exit-255")
    assert ambiguous.local_status == 255
    assert ambiguous.reported_exit is None
    assert ambiguous.streams_complete
    assert observations[-1].suppressed


def test_framing_failure_diagnostics_do_not_include_payloads() -> None:
    payload = b"untrusted-output-canary"

    class MalformedCarrier:
        features = ChannelFeatures()

        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            del invocation, io

        def execute(
            self,
            invocation: PreparedInvocation,
            *,
            io: CarrierIO,
            deadline: Deadline,
            custody: LocalDeliveryCustody,
        ) -> CarrierReport:
            del invocation, io, deadline
            return CarrierReport(
                Dispatch.SENT,
                ExitStatus(code=0),
                local_status=0,
                stdout=CapturedOutput(payload, complete=True, provenance=Provenance.CARRIER_STDOUT),
                stderr=CapturedOutput(payload, complete=True, provenance=Provenance.MIXED_STDERR),
            )

    with pytest.raises(AssertionError) as failure:
        check_buffered_contract(MalformedCarrier())
    assert payload.decode("ascii") not in str(failure.value)


def _check_literal_environment_delivery(connection: SSHConnection, custody: LocalDeliveryCustody) -> None:
    environment = {
        "AGW_ISSUE_845_JSON": json.dumps(
            {
                "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/fixture%40project.iam.gserviceaccount.com",
                "private_key": "synthetic-only\\key\nsecond line\n",
                "note": 'embedded "quotes" and 雪',
            },
            ensure_ascii=False,
        ),
        "AGW_LITERAL_VALUE": "literal %40 %h %% ${HOME} ' \" \\ 雪\n\r\t",
        "AGW_EMPTY_VALUE": "",
    }
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    source = (
        "import os,sys; "
        f"sys.stdout.buffer.write(b'\\0'.join(os.environ[name].encode('utf-8') for name in {tuple(environment)!r}))"
    )
    prepared = prepare_inline_candidate(
        Command(("/usr/bin/python3", "-I", "-S", "-B", "-c", source)),
        plan=plan,
        env=environment,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
    )
    assert isinstance(connection.trust, SSHTrustFiles)
    argv = build_ssh_argv(connection, prepared.invocation, trust=connection.trust)
    assert not any(argument.startswith("SetEnv=") for argument in argv)
    assert all(value not in argument for value in environment.values() if value for argument in argv)
    assert "fixture%40project" not in subprocess.list2cmdline(argv)

    result = execute_inline_candidate(
        FixtureBoundCarrier(SSHCarrier(connection), custody), prepared, deadline=Deadline.after(15)
    )
    assert result.dispatch is Dispatch.SENT
    assert result.carrier_completion == ExitStatus(code=0)
    assert result.carrier_failure is None
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    observation = result.observation
    assert observation is not None and observation.trusted_terminal
    assert observation.error is None and observation.failure is None
    assert observation.wait is not None and observation.wait.kind is WaitKind.EXIT and observation.wait.value == 0
    assert observation.stdout is not None and observation.stdout.complete and not observation.stdout.truncated
    assert observation.stdout.data == b"\0".join(value.encode("utf-8") for value in environment.values())
    assert observation.stderr is not None and observation.stderr.complete and observation.stderr.data == b""


def test_inline_environment_preserves_percent_json_through_ssh_process(
    local_binding: SSHConnection, custody: LocalDeliveryCustody
) -> None:
    _check_literal_environment_delivery(local_binding, custody)


@pytest.mark.integration
def test_installed_ssh_inline_environment_preserves_percent_json(
    local_sshd: SSHConnection, custody: LocalDeliveryCustody
) -> None:
    _check_literal_environment_delivery(local_sshd, custody)


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
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
from agentworks.execution.carrier import Deadline
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.models import Command
from agentworks.execution.preparation import prepare, decode_output

connection = SSHConnection(
    host="fixture.invalid", user="fixture", ssh_executable=sys.argv[1],
    identity_file=Path(sys.argv[2]), trust=SSHTrustFiles((Path(sys.argv[3]),)),
)
prepared = prepare(Command(("/bin/cat",)), stdin=b"\x00\xff\r\n")
custody = LocalDeliveryCustody()
try:
    result = SSHCarrier(connection).execute(
        prepared.invocation, io=prepared.io, deadline=Deadline.after(10), custody=custody,
    )
    if not custody.settled:
        raise AssertionError("SSH fixture returned pending local delivery")
finally:
    if not custody.close(Deadline.after(3)):
        raise AssertionError("SSH fixture retained local delivery")
output = decode_output(prepared, result.stdout)
if result.failure is not None or result.completion is None or result.completion.code != 0:
    raise AssertionError("SSH fixture did not complete")
if output.stdout != b"\x00\xff\r\n" or not output.stdout_complete:
    raise AssertionError("SSH fixture did not preserve bytes")
if any(name in sys.modules for name in retired):
    raise AssertionError("SSH fixture loaded legacy execution")
"""
    assert isinstance(local_binding.trust, SSHTrustFiles)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            local_binding.ssh_executable,
            str(local_binding.identity_file),
            str(local_binding.trust.known_hosts[0]),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
