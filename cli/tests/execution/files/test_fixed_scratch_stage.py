"""Fixed scratch staging admission, separate from generic file-path staging."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _file_stage_guest as guest
from agentworks.execution import _scratch_root as root
from agentworks.execution._file_stage_bundle import ROOT_SCRATCH_PROGRAM
from agentworks.execution._file_stage_exchange import stage_begin
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageFailureCode,
    decode_file_stage_request,
    encode_file_stage_request,
    parse_file_stage_failure,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._file_wire_reader import FileRecordReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    FiniteInput,
    PreparedInvocation,
)

NONCE = "a" * 32
TOKEN = bytes(range(16))
ROOT = IdentityExpectation(0, 0, (0,))
GUEST = VMGuestIdentity("b" * 32, "00000000-0000-4000-8000-000000000001", 1234)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux fixed scratch admission")


def _request() -> FileStageBeginRequest:
    return FileStageBeginRequest(NONCE, "/tmp", "prepared-input", TOKEN, 1024, ROOT, 10.0)


@pytest.mark.parametrize(
    "change",
    [
        {"root_path": "/var/tmp"},
        {"relative_path": "nested/input"},
        {"identity": IdentityExpectation(1001, 1001, (1001,))},
    ],
)
def test_fixed_parent_refuses_path_selection_and_nonroot_before_open(
    change: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        pytest.fail("fixed parent opened for a refused request")

    monkeypatch.setattr(guest, "open_scratch_root", unexpected_open)
    request = replace(_request(), **change)
    with pytest.raises(guest._SafeFailure) as failure:
        guest._operate(request, None, fixed_scratch_root=True)
    assert failure.value.failure.code is FileStageFailureCode.ROOT_REFUSED


@pytest.mark.parametrize("mode", [0o777, 0o700, 0o1777])
def test_fixed_parent_reuses_real_scratch_root_mode_admission(
    mode: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "scratch"
    selected.mkdir()
    selected.chmod(mode)
    monkeypatch.setattr(root, "_LINUX_SCRATCH_ROOT", str(selected))
    monkeypatch.setattr(root, "_EXPECTED_OWNER_UID", os.geteuid())
    if mode != 0o1777:
        with pytest.raises(guest._SafeFailure) as failure:
            guest._open_parent(_request(), None, fixed_scratch_root=True)
        assert failure.value.failure.code is FileStageFailureCode.ROOT_REFUSED
    else:
        descriptor, parent = guest._open_parent(_request(), None, fixed_scratch_root=True)
        try:
            assert parent is None
            assert os.fstat(descriptor).st_ino == selected.stat().st_ino
        finally:
            os.close(descriptor)
    assert not tuple(selected.iterdir())


def test_fixed_parent_closes_descriptor_when_primitive_control_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(guest, "open_scratch_root", lambda **_kwargs: descriptor)
    control = KeyboardInterrupt()

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise control

    monkeypatch.setattr(guest, "begin_scratch", interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        guest._operate(_request(), None, fixed_scratch_root=True)
    assert raised.value is control
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_packed_fixed_entry_decodes_existing_wire_and_refuses_other_root(interpreter: str) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    program = ROOT_SCRATCH_PROGRAM
    # Exercise the packed body without claiming native root or guest admission.
    source = program.loader_source + (
        "_agw_load_identity()\n_agw_load_remaining()\n"
        "g=sys.modules['_agw_file_stage._file_stage_guest']\n"
        "g.matches_current_identity=lambda identity:True\n"
        "raise SystemExit(_agw_enter_body())\n"
    )
    request = replace(_request(), root_path="/var/tmp")
    completed = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, NONCE],
        input=program.prefix + encode_file_stage_request(request),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0 and completed.stderr == b""
    records: list[FileRecord] = []
    reader = FileRecordReader(NONCE, records.append)
    assert reader.try_write(memoryview(completed.stdout)) == len(completed.stdout)
    reader.finish()
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
    assert parse_file_stage_failure(records[0].body, TOKEN, ROOT).code is FileStageFailureCode.ROOT_REFUSED


class _CaptureCarrier:
    features = ChannelFeatures()

    def __init__(self) -> None:
        self.input: bytes | None = None
        self.argv: tuple[str, ...] | None = None
        self.control = KeyboardInterrupt()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        assert isinstance(io.input, FiniteInput)
        self.input = io.input.data
        self.argv = invocation.argv
        raise self.control


def test_fixed_exchange_requires_bootstrap_before_dispatch_and_uses_selected_program() -> None:
    carrier = _CaptureCarrier()
    plan = IdentityPlan(ROOT, IdentityMode.DIRECT)

    def begin(bootstrap: _NumericGuestBootstrap | None = None) -> None:
        stage_begin(
            carrier,
            trusted_root_path="/tmp",
            relative_path="prepared-input",
            token=TOKEN,
            expected_length=1024,
            plan=plan,
            deadline=Deadline.after(10),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
            fixed_scratch_root=True,
            bootstrap=bootstrap,
        )

    with pytest.raises(ValidationError):
        begin()
    assert carrier.input is None
    with pytest.raises(KeyboardInterrupt) as raised:
        begin(_NumericGuestBootstrap(plan, GUEST))
    assert raised.value is carrier.control
    assert carrier.input is not None and carrier.input.startswith(ROOT_SCRATCH_PROGRAM.prefix)
    wire = decode_file_stage_request(carrier.input[len(ROOT_SCRATCH_PROGRAM.prefix) :])
    assert wire.root_path == "/tmp" and wire.relative_path == "prepared-input"
    assert carrier.argv is not None
