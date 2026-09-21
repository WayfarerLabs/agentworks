"""Cross-family checks for fixed stdin-delivered file-helper bundles."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_inventory_bundle import FIXED_BUNDLE as INVENTORY_BUNDLE
from agentworks.execution._file_inventory_protocol import parse_file_inventory_failure
from agentworks.execution._file_metadata_bundle import FIXED_BUNDLE as METADATA_BUNDLE
from agentworks.execution._file_metadata_protocol import parse_file_metadata_failure
from agentworks.execution._file_object_bundle import FIXED_BUNDLE as OBJECT_BUNDLE
from agentworks.execution._file_object_protocol import parse_file_object_failure
from agentworks.execution._file_read_bundle import FIXED_BUNDLE as READ_BUNDLE
from agentworks.execution._file_read_protocol import parse_file_read_failure
from agentworks.execution._file_snapshot_bundle import FIXED_BUNDLE as SNAPSHOT_BUNDLE
from agentworks.execution._file_snapshot_protocol import parse_file_snapshot_failure
from agentworks.execution._file_stage_bundle import FIXED_BUNDLE as STAGE_BUNDLE
from agentworks.execution._file_stage_protocol import parse_file_stage_failure
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import build_runtime_identity_helper_argv
from agentworks.execution.carrier import CarrierIO, Deadline, FiniteInput, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles
from tests.execution.files._runtime_support import runtime_selection

_NONCE = "0" * 32
_BUNDLES = (
    ("read", READ_BUNDLE),
    ("object", OBJECT_BUNDLE),
    ("metadata", METADATA_BUNDLE),
    ("inventory", INVENTORY_BUNDLE),
    ("stage", STAGE_BUNDLE),
    ("snapshot", SNAPSHOT_BUNDLE),
)


class _Sink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


def _invocation(bundle: FixedFileHelperBundle, plan: IdentityPlan) -> PreparedInvocation:
    return PreparedInvocation(
        build_runtime_identity_helper_argv(
            plan,
            selection=runtime_selection("/usr/bin/python3"),
            fixed_source=bundle.bootstrap,
            nonce=_NONCE,
        )[0]
    )


def _failure_code(family: str, body: bytes) -> str:
    identity = IdentityExpectation(0, 0, (0,))
    token = bytes(16)
    if family == "read":
        return parse_file_read_failure(body).value
    if family == "object":
        return parse_file_object_failure(body).code.value
    if family == "metadata":
        return parse_file_metadata_failure(body).code.value
    if family == "inventory":
        return parse_file_inventory_failure(body).value
    if family == "stage":
        return parse_file_stage_failure(body, token, identity).code.value
    if family == "snapshot":
        return parse_file_snapshot_failure(body, token, identity).code.value
    raise AssertionError(f"unknown fixed file family: {family}")


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
def test_every_fixed_family_reports_parsed_invalid_request_in_isolated_distribution_python311(
    family: str,
    bundle: FixedFileHelperBundle,
) -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("distribution Python 3.11 is unavailable")
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", bundle.bootstrap, _NONCE],
        input=bundle.prefix + b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )

    records: list[FileRecord] = []
    reader = FileRecordReader(_NONCE, records.append)
    offset = 0
    while offset < len(completed.stdout):
        offset += reader.try_write(memoryview(completed.stdout)[offset:])
    reader.finish()

    assert completed.returncode == 0, family
    assert completed.stderr == b""
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
    assert _failure_code(family, records[0].body) == "invalid_request"
    assert records[1].body == b"{}"
    assert bundle.prefix not in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="the bounded pipe-size check requires Linux")
def test_bootstrap_accepts_fragmented_prefix_and_preserves_manifest_boundary() -> None:
    manifest = b"raise SystemExit(42)"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", READ_BUNDLE.bootstrap, _NONCE],
        input=READ_BUNDLE.prefix + manifest,
        capture_output=True,
        pipesize=4096,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout and completed.stderr == b""
    assert manifest not in completed.stdout


@pytest.mark.parametrize(
    "runtime",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "distribution-3.11"],
)
@pytest.mark.parametrize(
    "payload",
    [READ_BUNDLE.prefix[:-1], bytes([READ_BUNDLE.prefix[0] ^ 1]) + READ_BUNDLE.prefix[1:] + b"{}"],
    ids=["short", "changed"],
)
def test_bootstrap_refuses_unverified_prefix_without_protocol_output(payload: bytes, runtime: Path) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", READ_BUNDLE.bootstrap, _NONCE],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 125
    assert completed.stdout == completed.stderr == b""


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
    ],
    ids=["direct", "root", "demote"],
)
def test_complete_provider_body_fits_and_oversize_refuses_before_wire(
    family: str,
    bundle: FixedFileHelperBundle,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(bundle, plan)
    maximum_manifest = b"x" * 32_768
    representative = json.dumps(
        {"command": invocation.argv, "input-data": (bundle.prefix + maximum_manifest).decode("ascii")}
    ).encode("ascii")
    assert len(representative) < 65_536, family

    carrier = ProxmoxCarrier(
        ProxmoxConnection(
            "https://pve.example:8006",
            "node-a",
            101,
            "root@pam!token",
            "secret",
        )
    )

    def unexpected_wire(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized provider body reached the wire")

    monkeypatch.setattr(carrier._wire, "request", unexpected_wire)
    provider_maximum = bundle.prefix + b"x" * (65_536 - len(bundle.prefix))
    io = CarrierIO(
        input=FiniteInput(provider_maximum, sensitive=True),
        output=SinkOutput(_Sink(), _Sink(), require_live=False),
        sensitive=True,
    )
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline.after(1))


@pytest.mark.parametrize(("family", "bundle"), _BUNDLES)
@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
    ],
    ids=["direct", "root", "demote"],
)
def test_windows_ssh_command_contains_only_short_fixed_bootstrap(
    family: str,
    bundle: FixedFileHelperBundle,
    plan: IdentityPlan,
) -> None:
    native_root = Path(Path.cwd().anchor)
    trust = SSHTrustFiles((native_root / "keys" / "known-hosts",))
    connection = SSHConnection("host.example", "agent", native_root / "keys" / "identity", trust)
    argv = build_ssh_argv(connection, _invocation(bundle, plan), trust=trust)
    windows_command = subprocess.list2cmdline(argv)

    assert len(windows_command) < 32_767, family
    assert bundle.prefix.decode("ascii") not in windows_command
