"""Local staged input and packed Python 3.11 proof with injected admission seams."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.execution import _guest_bootstrap as root_bootstrap
from agentworks.execution import _scratch as scratch
from agentworks.execution import _staged_helper_guest as guest
from agentworks.execution._helper_bundle import FixedFileHelperBundle, RootGuestDelivery, _build_file_helper_bundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._scratch_receipt import ScratchOperation, current_receipt_context, scratch_name
from agentworks.execution._staged_helper_bundle import build_staged_helper_program
from agentworks.execution._staged_helper_protocol import StagedHelperRequest, decode_request, encode_request
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import CarrierIO, FiniteInput, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier

from .test_staged_helper_protocol import NONCE, TOKEN, _request

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux staged helper and procfs admission")


def _bundle() -> FixedFileHelperBundle:
    source = """import hashlib,json,os,sys
def main(nonce):
 assert sys.argv==['agentworks-fixed-helper',nonce]
 assert nonce=='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
 digest=hashlib.sha256()
 size=0
 while chunk:=os.read(0,65536):
  digest.update(chunk)
  size+=len(chunk)
 os.write(1,json.dumps({'size':size,'sha256':digest.hexdigest()}).encode())
 return 23
"""
    return _build_file_helper_bundle("_staged_payload_test", (("entry", source),), "entry")


@contextmanager
def _staged(tmp_path: Path, content: bytes) -> Iterator[tuple[int, StagedHelperRequest]]:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    context = current_receipt_context(ScratchOperation.STAGE)
    reference = scratch.begin_scratch(parent, len(content), TOKEN, context)
    try:
        for offset in range(0, len(content), 12288):
            chunk = content[offset : offset + 12288]
            scratch.write_scratch_chunk(parent, reference, offset, chunk, hashlib.sha256(chunk).digest())
        request = StagedHelperRequest(
            NONCE, context.identity, TOKEN, reference, hashlib.sha256(content).digest(), 10_000_000_000
        )
        yield parent, request
    finally:
        scratch.cleanup_scratch(parent, reference)
        os.close(parent)


def _run_packed(
    interpreter: str, tmp_path: Path, bundle: FixedFileHelperBundle, request: StagedHelperRequest
) -> subprocess.CompletedProcess[bytes]:
    program = build_staged_helper_program(bundle)
    source = program.loader_source + (
        "_agw_load_identity()\n_agw_load_remaining()\n"
        "p=sys.modules['_agw_staged_helper._staged_helper_protocol']\n"
        "g=sys.modules['_agw_staged_helper._staged_helper_guest']\n"
        "request=p.decode_request(os.read(0,4097))\n"
        f"parent=os.open({str(tmp_path)!r},os.O_RDONLY|os.O_DIRECTORY)\n"
        "try:\n"
        " import time\n"
        f" result=g._load(request,{bundle.bootstrap!r},parent,time.monotonic()+request.remaining_ns/1000000000)\n"
        "finally:os.close(parent)\n"
        "raise SystemExit(result)\n"
    )
    return subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, NONCE],
        input=program.prefix + encode_request(request),
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("size", [0, 113, 4 * 1024 * 1024])
def test_packed_original_helper_reads_exact_large_binary_suffix(interpreter: str, size: int, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    bundle = _bundle()
    payload = (bytes(range(256)) * ((size + 255) // 256))[:size]
    with _staged(tmp_path, bundle.prefix + payload) as (_, request):
        result = _run_packed(interpreter, tmp_path, bundle, request)
    assert result.returncode == 23 and result.stderr == b"", result.stderr
    assert json.loads(result.stdout) == {"size": size, "sha256": hashlib.sha256(payload).hexdigest()}


@pytest.mark.parametrize(
    "variant", ["whole-hash", "prefix-hash", "prefix-truncated", "data-tampered", "wrong-inode", "wrong-token"]
)
def test_integrity_failure_never_enters_original_helper(tmp_path: Path, variant: str) -> None:
    bundle = _bundle()
    content = bundle.prefix + b"request"
    if variant == "prefix-hash":
        content = b"!" + content[1:]
    elif variant == "prefix-truncated":
        content = bundle.prefix[:-1]
    with _staged(tmp_path, content) as (_, request):
        if variant == "whole-hash":
            request = replace(request, sha256=b"x" * 32)
        elif variant == "data-tampered":
            path = tmp_path / scratch_name(TOKEN) / "data"
            with path.open("r+b") as stream:
                stream.write(b"!")
        elif variant == "wrong-inode":
            reference = request.reference
            ownership = reference._ownership
            request = replace(
                request,
                reference=scratch.ScratchReference(
                    replace(ownership, _data=replace(ownership._data, inode=ownership._data.inode + 1))
                ),
            )
        elif variant == "wrong-token":
            value = json.loads(encode_request(request))
            value["token"] = "f" * 32
            request = decode_request(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        result = _run_packed(sys.executable, tmp_path, bundle, request)
    assert result.returncode == 125
    assert result.stdout == result.stderr == b""


def test_borrow_ready_descriptor_checks_timestamps_and_closes_fd(tmp_path: Path) -> None:
    with _staged(tmp_path, b"prepared") as (parent, request):
        ready = scratch.verify_scratch(parent, request.reference, request.sha256)
        with scratch.borrow_ready_scratch(parent, ready) as descriptor:
            assert os.read(descriptor, 8) == b"prepared"
        with pytest.raises(OSError):
            os.fstat(descriptor)
        path = tmp_path / scratch_name(TOKEN) / "data"
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        with pytest.raises(scratch.ScratchTransferError) as failure, scratch.borrow_ready_scratch(parent, ready):
            pytest.fail("changed ready object was borrowed")
        assert failure.value.cleanup_debt is not None


def test_borrow_preserves_consumer_control_and_cleanup_debt(tmp_path: Path) -> None:
    control = KeyboardInterrupt()
    with _staged(tmp_path, b"prepared") as (parent, request):
        ready = scratch.verify_scratch(parent, request.reference, request.sha256)
        with pytest.raises(KeyboardInterrupt) as raised, scratch.borrow_ready_scratch(parent, ready) as descriptor:
            raise control
        assert raised.value is control
        assert isinstance(control.__cause__, scratch.ScratchTransferError)
        assert control.__cause__.cleanup_debt is not None
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_loader_restores_descriptors_argv_and_original_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle()
    with _staged(tmp_path, bundle.prefix + b"payload") as (parent, request):
        before = set(os.listdir("/proc/self/fd"))
        argv = sys.argv
        control = KeyboardInterrupt()
        # This source is trusted test fixture code, never protocol input.
        monkeypatch.setattr(guest, "_TEST_CONTROL", control, raising=False)
        bootstrap = "from agentworks.execution import _staged_helper_guest as g\nraise g._TEST_CONTROL\n"
        with pytest.raises(KeyboardInterrupt) as raised:
            guest._load(request, bootstrap, parent, time.monotonic() + 1)
        assert raised.value is control and control.__cause__ is None
        assert sys.argv is argv
        assert set(os.listdir("/proc/self/fd")) == before


def test_expired_budget_refuses_before_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _staged(tmp_path, b"prepared") as (parent, request):

        def verify(*_args: object, **_kwargs: object) -> None:
            pytest.fail("verification after elapsed budget")

        monkeypatch.setattr(guest, "verify_scratch", verify)
        assert guest._load(request, "raise SystemExit(23)", parent, time.monotonic() - 1) == 125


@pytest.mark.parametrize("field", ["nonce", "identity"])
def test_guest_identity_refusal_precedes_scratch_open(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    monkeypatch.setattr(guest, "_read_request", lambda: request)
    monkeypatch.setattr(guest, "matches_current_identity", lambda _identity: field != "identity")

    def open_parent(**_kwargs: object) -> int:
        pytest.fail("scratch opened before identity admission")

    monkeypatch.setattr(guest, "open_scratch_root", open_parent)
    assert guest.main("c" * 32 if field == "nonce" else NONCE, _bundle().bootstrap) == 125


def test_complete_proxmox_loader_envelope_fits_for_existing_managed_start() -> None:
    from agentworks.execution._managed_start_bundle import FIXED_BUNDLE

    root = IdentityExpectation(0, 0, (0,))
    program = build_staged_helper_program(FIXED_BUNDLE)
    argv, candidates, shim = build_root_guest_bootstrap_argv(
        IdentityPlan(root, IdentityMode.SUDO_ROOT),
        root,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        program=program,
        nonce=NONCE,
        expected_guest=VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234),
    )
    body = ProxmoxCarrier._request_body(
        PreparedInvocation(argv), CarrierIO(input=FiniteInput(program.prefix + encode_request(_request())))
    )
    assert program.delivery is RootGuestDelivery.FIXED_PREFIX
    assert len(body) < 65536
    assert candidates == ("/usr/bin/python3",) and shim is None


@pytest.mark.parametrize("wrong_guest", [False, True])
def test_outer_root_and_guest_checkpoint_precede_bulk_and_scratch(
    monkeypatch: pytest.MonkeyPatch,
    wrong_guest: bool,
) -> None:
    program = build_staged_helper_program(_bundle())
    expected = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
    observed = replace(expected, init_start_ticks=1235) if wrong_guest else expected
    events: list[str] = []
    pending = bytearray(program.prefix + encode_request(_request()))
    actual_read = os.read

    def read(fd: int, length: int) -> bytes:
        if fd != 0:
            return actual_read(fd, length)
        events.append("prefix")
        chunk = bytes(pending[:length])
        del pending[:length]
        return chunk

    @contextmanager
    def admit(*_args: object) -> Iterator[object]:
        events.append("root")
        try:
            yield lambda: b"held init stat"
        finally:
            events.append("closed")

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(root_bootstrap, "_admit", admit)
    loader = program.loader_source + (
        "_original_identity=_agw_load_identity\n"
        "def _agw_load_identity():\n"
        " _original_identity()\n"
        " g=sys.modules[_agw_guest_module]\n"
        " g._identity=lambda:sys.modules['_agw_staged_helper._vm_guest_identity_protocol'].VMGuestIdentity("
        f"{observed.instance_marker!r},{observed.boot_id!r},{observed.init_start_ticks})\n"
        "def _agw_load_remaining():raise RuntimeError('bulk boundary reached')\n"
    )
    failure = root_bootstrap._BootstrapRefusal if wrong_guest else RuntimeError
    with pytest.raises(failure):
        root_bootstrap._run(0, 0, (0,), loader, (expected.instance_marker, expected.boot_id, expected.init_start_ticks))
    assert events == ["root", "prefix", "closed"]
    assert bytes(pending) == encode_request(_request())


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_staged_original_managed_request_keeps_script_and_stdin_separate(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    from agentworks.execution import _managed_start_bundle as start
    from agentworks.execution._managed_job_request import ManagedJobRequest
    from agentworks.execution._managed_start_protocol import ManagedStartRequest
    from agentworks.execution._managed_start_protocol import encode_request as encode_start

    from .test_managed_service_guest import _launch

    source = b"printf should-not-execute"
    stdin = b"binary-stdin\0\xff" * 5000
    job = ManagedJobRequest(_launch(shell="sh"), "script", (), None, "discard", None, (), source, stdin)
    request = ManagedStartRequest(
        NONCE,
        IdentityExpectation(0, 0, (0,)),
        job,
        VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234),
    )
    # Keep the original request decoder and replace only service activation.
    patch = (
        "\ndef main(nonce):\n request=_read_request()\n assert request.nonce==nonce\n"
        " os.write(1,request.job.source+b'\\0'+request.job.stdin)\n return 23\n"
    )
    sources = tuple((name, text + patch if name == "_managed_start_guest" else text) for name, text in start._SOURCES)
    bundle = _build_file_helper_bundle("_agw_managed_start", sources, "_managed_start_guest")
    with _staged(tmp_path, bundle.prefix + encode_start(request)) as (_, staged):
        result = _run_packed(interpreter, tmp_path, bundle, staged)
    assert result.returncode == 23 and result.stderr == b"", result.stderr
    assert result.stdout == source + b"\0" + stdin


def test_loader_never_requests_an_unbounded_payload_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle()
    payload = b"\0\xff" * (2 * 1024 * 1024)
    with _staged(tmp_path, bundle.prefix + payload) as (parent, request):
        actual_read, actual_pread = os.read, os.pread
        bounds: list[int] = []

        def read(fd: int, length: int) -> bytes:
            bounds.append(length)
            assert length <= 65536
            return actual_read(fd, length)

        def pread(fd: int, length: int, offset: int) -> bytes:
            bounds.append(length)
            assert length <= 65536
            return actual_pread(fd, length, offset)

        monkeypatch.setattr(os, "read", read)
        monkeypatch.setattr(os, "pread", pread)
        assert guest._load(request, bundle.bootstrap, parent, time.monotonic() + 5) == 23
        assert max(bounds) == 65536
    captured = capfd.readouterr()
    assert json.loads(captured.out) == {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def test_expiry_after_verification_prevents_bootstrap_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle()
    with _staged(tmp_path, bundle.prefix + b"payload") as (parent, request):
        samples = iter((0.0, float("inf")))
        monkeypatch.setattr(guest, "time", SimpleNamespace(monotonic=lambda: next(samples)))
        assert guest._load(request, "raise AssertionError('entered')", parent, time.monotonic() + 5) == 125


def test_original_bootstrap_result_returns_through_outer_admission_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    bundle = _bundle()
    program = build_staged_helper_program(bundle)
    observed = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
    events: list[str] = []
    pending = bytearray(program.prefix)
    actual_read = os.read

    def read(fd: int, length: int) -> bytes:
        if fd == 0 and pending:
            chunk = bytes(pending[:length])
            del pending[:length]
            return chunk
        return actual_read(fd, length)

    @contextmanager
    def admit(*_args: object) -> Iterator[object]:
        events.append("admitted")
        try:
            yield lambda: b"held init stat"
        finally:
            events.append("closed")

    with _staged(tmp_path, bundle.prefix + b"payload") as (parent, request):
        monkeypatch.setattr(os, "read", read)
        monkeypatch.setattr(root_bootstrap, "_admit", admit)
        loader = program.loader_source + (
            "_original_identity=_agw_load_identity\n"
            "def _agw_load_identity():\n"
            " _original_identity()\n"
            " g=sys.modules[_agw_guest_module]\n"
            " g._identity=lambda:sys.modules['_agw_staged_helper._vm_guest_identity_protocol'].VMGuestIdentity("
            f"{observed.instance_marker!r},{observed.boot_id!r},{observed.init_start_ticks})\n"
            "def _agw_enter_body():\n"
            " import time\n"
            " p=sys.modules['_agw_staged_helper._staged_helper_protocol']\n"
            " g=sys.modules['_agw_staged_helper._staged_helper_guest']\n"
            f" return g._load(p.decode_request({encode_request(request)!r}),"
            f"{bundle.bootstrap!r},{parent},time.monotonic()+5)\n"
        )
        assert (
            root_bootstrap._run(
                0, 0, (0,), loader, (observed.instance_marker, observed.boot_id, observed.init_start_ticks)
            )
            == 23
        )
    assert events == ["admitted", "closed"]
    assert json.loads(capfd.readouterr().out)["size"] == 7


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_staged_suffix_is_literal_binary_not_compiled_source(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    source = "import os\ndef main(nonce):\n while chunk:=os.read(0,4096):\n  os.write(1,chunk)\n return 1\n"
    bundle = _build_file_helper_bundle("_staged_literal_suffix", (("entry", source),), "entry")
    payload = b"raise AssertionError('request compiled')\n\0\xff" * 1000
    with _staged(tmp_path, bundle.prefix + payload) as (_, request):
        result = _run_packed(interpreter, tmp_path, bundle, request)
    assert result.returncode == 1 and result.stderr == b"", result.stderr
    assert result.stdout == payload


@pytest.mark.parametrize("code", [0, 1, 255])
def test_original_bootstrap_exit_status_is_preserved(tmp_path: Path, code: int) -> None:
    source = f"def main(nonce):return {code}\n"
    bundle = _build_file_helper_bundle("_staged_status", (("entry", source),), "entry")
    with _staged(tmp_path, bundle.prefix) as (_, request):
        result = _run_packed(sys.executable, tmp_path, bundle, request)
    assert result.returncode == code
    assert result.stdout == result.stderr == b""


def test_budget_elapsed_after_decode_refuses_before_root_open(monkeypatch: pytest.MonkeyPatch) -> None:
    request = replace(_request(), remaining_ns=1_000_000_000)
    samples = iter((1.0, 3.0))
    monkeypatch.setattr(guest, "time", SimpleNamespace(monotonic=lambda: next(samples)))
    monkeypatch.setattr(guest, "_read_request", lambda: request)
    monkeypatch.setattr(guest, "matches_current_identity", lambda _identity: True)

    def open_parent(**_kwargs: object) -> int:
        pytest.fail("scratch opened after expiry")

    monkeypatch.setattr(guest, "open_scratch_root", open_parent)
    assert guest.main(NONCE, _bundle().bootstrap) == 125
