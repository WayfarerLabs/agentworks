"""Real gate coverage at the private upload helper boundary."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import _file_effect_gate, _file_publication_guest, _file_stage_guest
from agentworks.execution._file_effect_gate import FileEffectGateError, advance_file_effect_gate, setup_file_effect_gate
from agentworks.execution._file_publication import Create, CreateMetadata, PublicationCleanupDebt
from agentworks.execution._file_publication_bundle import _MODULE_NAMES as PUBLICATION_MODULES
from agentworks.execution._file_publication_bundle import _PACKAGE as PUBLICATION_PACKAGE
from agentworks.execution._file_publication_protocol import (
    FilePublicationCleanupRequest,
    FilePublicationFailureCode,
    FilePublicationReconcileRequest,
    FilePublicationRequestError,
    FilePublishRequest,
    decode_file_publication_request,
    encode_file_publication_request,
)
from agentworks.execution._file_publication_wire import bind_publication_cleanup_debt
from agentworks.execution._file_stage_bundle import _MODULE_NAMES as STAGE_MODULES
from agentworks.execution._file_stage_bundle import _PACKAGE as STAGE_PACKAGE
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageCleanupRequest,
    FileStageFailureCode,
    FileStageReconcileRequest,
    FileStageRequestError,
    decode_file_stage_request,
    encode_file_stage_request,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._publication_receipt import _Identity, publication_stage_name
from agentworks.execution._scratch import _cleanup_debt
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._publication_test_support import open_parent, ready_scratch

if TYPE_CHECKING:
    from agentworks.execution._file_effect_gate import FileEffectGateBinding

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the upload effect gate requires Linux")

_NONCE = "a" * 32
_TOKEN = bytes(range(16))
_GUEST = VMGuestIdentity("b" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileEffectGateBinding:
    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    uid_dir = namespace / str(os.geteuid())
    uid_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())
    return setup_file_effect_gate(str(uid_dir / ("c" * 64 + ".db")), _GUEST, os.geteuid(), "vm-a", lambda: _GUEST)


def _identity() -> IdentityExpectation:
    return IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))


def _reference(root: Path):
    parent_fd = open_parent(root)
    try:
        return ready_scratch(parent_fd, _TOKEN, b"x")._reference
    finally:
        os.close(parent_fd)


def _stage_requests(root: Path, gate: FileEffectGateBinding):
    identity = _identity()
    reference = _reference(root)
    return (
        FileStageBeginRequest(_NONCE, str(root), "target", _TOKEN, 1, identity, 1.0, gate),
        FileStageChunkRequest(
            _NONCE, str(root), "target", _TOKEN, reference, 0, b"x", hashlib.sha256(b"x").digest(), identity, 1.0, gate
        ),
        FileStageReconcileRequest(_NONCE, str(root), "target", _TOKEN, identity, 1.0, gate),
        FileStageCleanupRequest(_NONCE, str(root), "target", _TOKEN, _cleanup_debt(reference), identity, 1.0, gate),
    )


def _publication_requests(root: Path, gate: FileEffectGateBinding):
    identity = _identity()
    reference = _reference(root)
    parent = root.stat()
    cleanup = bind_publication_cleanup_debt(
        reference,
        _Identity(parent.st_dev, parent.st_ino),
        PublicationCleanupDebt(publication_stage_name(_TOKEN), parent.st_dev, parent.st_ino + 1),
    )
    return (
        FilePublishRequest(
            _NONCE,
            _TOKEN,
            str(root),
            "target",
            reference,
            hashlib.sha256(b"x").digest(),
            Create(),
            CreateMetadata(os.geteuid(), os.getegid(), 0o600),
            identity,
            1.0,
            gate,
        ),
        FilePublicationReconcileRequest(_NONCE, _TOKEN, str(root), "target", reference, identity, 1.0, gate),
        FilePublicationCleanupRequest(_NONCE, _TOKEN, str(root), "target", reference, cleanup, identity, 1.0, gate),
    )


class _Writer:
    records: list[tuple[FileRecordKind, bytes]] = []

    def __init__(self, nonce: str) -> None:
        assert nonce == _NONCE
        self.records = []
        _Writer.records = self.records

    def write(self, kind: FileRecordKind, body: bytes) -> None:
        self.records.append((kind, body))


@pytest.mark.parametrize("family", ["stage", "publication"])
def test_stale_generation_refuses_each_request_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: FileEffectGateBinding, family: str
) -> None:
    root = tmp_path / "destination"
    root.mkdir()
    requests = _stage_requests(root, gate) if family == "stage" else _publication_requests(root, gate)
    module = _file_stage_guest if family == "stage" else _file_publication_guest
    refused = (
        FileStageFailureCode.EFFECT_GATE_REFUSED
        if family == "stage"
        else FilePublicationFailureCode.EFFECT_GATE_REFUSED
    )
    encode = encode_file_stage_request if family == "stage" else encode_file_publication_request
    decode = decode_file_stage_request if family == "stage" else decode_file_publication_request
    for request in requests:
        assert decode(encode(request)) == request
    advance_file_effect_gate(replace(gate, proposed_generation=b"d" * 16), lambda: _GUEST)
    monkeypatch.setattr(module, "FileRecordWriter", _Writer)
    monkeypatch.setattr(module, "_identity", lambda: _GUEST)

    def unexpected_effect(*args: object, **kwargs: object) -> None:
        raise AssertionError("stale request reached upload operation")

    monkeypatch.setattr(module, "_operate", unexpected_effect)
    for request in requests:
        monkeypatch.setattr(module, "_read_request", lambda request=request: request)
        assert module.main(_NONCE) == 0
        assert [kind for kind, _ in _Writer.records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
        assert refused.value.encode() in _Writer.records[0][1]
    assert not (root / "target").exists()


@pytest.mark.parametrize("family", ["stage", "publication"])
def test_active_upload_effect_holds_gate_against_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: FileEffectGateBinding, family: str
) -> None:
    root = tmp_path / "destination"
    root.mkdir()
    if family == "stage":
        request = FileStageBeginRequest(_NONCE, str(root), "target", _TOKEN, 1, _identity(), 5.0, gate)
        module: ModuleType = _file_stage_guest
        original = module.begin_scratch
        effect_name = "begin_scratch"
    else:
        request = _publication_requests(root, gate)[0]
        module = _file_publication_guest
        original = module.publish_file
        effect_name = "publish_file"
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def effect(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, effect_name, effect)
    monkeypatch.setattr(module, "FileRecordWriter", _Writer)
    monkeypatch.setattr(module, "_identity", lambda: _GUEST)
    monkeypatch.setattr(module, "_read_request", lambda: request)

    def run() -> None:
        try:
            assert module.main(_NONCE) == 0
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(FileEffectGateError):
            advance_file_effect_gate(
                replace(gate, proposed_generation=b"d" * 16),
                lambda: _GUEST,
                expires_at=time.monotonic() + 0.05,
            )
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    assert _Writer.records[0][0] is FileRecordKind.RESULT
    if family == "stage":
        assert (root / scratch_name(_TOKEN)).is_dir()
    else:
        assert (root / "target").read_bytes() == b"x"
    advance_file_effect_gate(replace(gate, proposed_generation=b"d" * 16), lambda: _GUEST)


@pytest.mark.parametrize("family", ["stage", "publication"])
def test_upload_rejects_gate_namespace_destination(tmp_path: Path, gate: FileEffectGateBinding, family: str) -> None:
    root = tmp_path / "destination"
    root.mkdir()
    request = _stage_requests(root, gate)[0] if family == "stage" else _publication_requests(root, gate)[0]
    namespace = Path(gate.path).parent.parent
    targeted = replace(request, root_path=str(namespace.parent), relative_path=namespace.name + "/target")
    error = FileStageRequestError if family == "stage" else FilePublicationRequestError
    encode = encode_file_stage_request if family == "stage" else encode_file_publication_request
    with pytest.raises(error):
        encode(targeted)
    assert not (namespace / "target").exists()


@pytest.mark.parametrize("family", ["stage", "publication"])
def test_fixed_python311_bundle_enforces_generation(tmp_path: Path, gate: FileEffectGateBinding, family: str) -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("distribution Python 3.11 is unavailable")
    root = tmp_path / "destination"
    root.mkdir()
    package, modules, guest, request, encode = (
        (STAGE_PACKAGE, STAGE_MODULES, "_file_stage_guest", _stage_requests(root, gate)[2], encode_file_stage_request)
        if family == "stage"
        else (
            PUBLICATION_PACKAGE,
            PUBLICATION_MODULES,
            "_file_publication_guest",
            _publication_requests(root, gate)[1],
            encode_file_publication_request,
        )
    )
    patch = (
        f"gate=sys.modules[{(package + '._file_effect_gate')!r}]\n"
        f"gate._GATE_NAMESPACE={_file_effect_gate._GATE_NAMESPACE!r}\n"
        "gate._ROOT_UID=os.geteuid()\n"
        f"guest._identity=lambda: sys.modules[{(package + '._vm_guest_identity_protocol')!r}]."
        f"VMGuestIdentity({_GUEST.instance_marker!r},{_GUEST.boot_id!r},{_GUEST.init_start_ticks!r})\n"
    )
    bundle = fixture_file_bundle(package, modules, guest, "import os\n" + patch)

    def run() -> list[FileRecord]:
        completed = subprocess.run(
            [str(runtime), "-I", "-S", "-B", "-c", bundle.bootstrap, _NONCE],
            input=bundle.prefix + encode(request),
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0
        assert completed.stderr == b""
        records: list[FileRecord] = []
        reader = FileRecordReader(_NONCE, records.append)
        reader.try_write(memoryview(completed.stdout))
        reader.finish()
        assert reader.error is None
        return records

    assert [record.kind for record in run()] == [FileRecordKind.RESULT, FileRecordKind.FINISHED]
    advance_file_effect_gate(replace(gate, proposed_generation=b"d" * 16), lambda: _GUEST)
    assert [record.kind for record in run()] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
