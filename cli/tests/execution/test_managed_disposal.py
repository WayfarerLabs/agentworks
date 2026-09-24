"""Private exact-run disposal and receipt recovery."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_disposal_bundle import FIXED_BUNDLE
from agentworks.execution._managed_disposal_exchange import DisposalState, _Collector
from agentworks.execution._managed_disposal_protocol import (
    DisposalRequest,
    DisposalResult,
    decode_request,
    encode_request,
    encode_result,
)
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, RequestAsset, StoreError, Stream
from agentworks.execution.carrier import Dispatch, ExitStatus

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed disposal")
RUN = "a" * 32
NONCE = "b" * 32


def _launch(target: str = "vm-one") -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {
                "kind": "vm",
                "name": target,
                "incarnation": "v1:" + "c" * 64,
                "boot_id": "00000000-0000-4000-8000-000000000001",
            },
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


def _fact(name: FactName, launch: bytes) -> bytes:
    fields: dict[str, object] = {
        "version": 1,
        "kind": name.value,
        "run_id": RUN,
        "unit": f"agw-managed-{RUN}.service",
        "receipt_sha256": hashlib.sha256(launch).hexdigest(),
    }
    if name in (FactName.STDOUT_END, FactName.STDERR_END):
        fields.update(
            kind="stream-end",
            stream=name.value[:-4],
            retained_bytes=0,
            retained_sha256=hashlib.sha256(b"").hexdigest(),
            disposition="discarded",
        )
    if name is FactName.WAIT:
        fields.update(exit_code=0, signal=None)
    return wire.encode_fact(fields)


def _store(tmp_path: Path) -> ManagedJobStore:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return ManagedJobStore(RUN, _namespace="managed", _owner_uid=os.getuid(), _anchor_fd=fd)
    finally:
        os.close(fd)


def _terminal(store: ManagedJobStore, *, wait: bool = True) -> bytes:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    for name in (
        *((FactName.WAIT,) if wait else ()),
        FactName.BOUNDARY_EMPTY,
        FactName.STDOUT_END,
        FactName.STDERR_END,
    ):
        store.publish_fact(name, _fact(name, launch))
    return launch


@pytest.mark.parametrize("wait", [True, False])
def test_terminal_disposal_and_exact_retry(tmp_path: Path, wait: bool) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store, wait=wait)
        store.publish_request_asset(RequestAsset.SOURCE, b"payload")
        store.publish_stop_request(launch)
        directory = tmp_path / "managed" / RUN
        (directory / "stdout").write_bytes(b"corrupt capture")
        (directory / "stdout").chmod(0o600)
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]
        assert (directory / "disposal").read_bytes() == launch
        assert (directory / "disposal").stat().st_nlink == 1
        assert store.dispose(launch)
        with pytest.raises(StoreError):
            store.publish_fact(FactName.WAIT, _fact(FactName.WAIT, launch))
        with pytest.raises(StoreError):
            store.publish_request_asset(RequestAsset.SOURCE, b"payload")
        with pytest.raises(StoreError):
            store.publish_stop_request(launch)
        with pytest.raises(StoreError):
            store.open_capture(Stream.STDOUT, 1)


def test_missing_terminal_never_commits(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _launch()
        store.publish_fact(FactName.LAUNCH, launch)
        store.publish_fact(FactName.BOUNDARY_EMPTY, _fact(FactName.BOUNDARY_EMPTY, launch))
        assert not store.dispose(launch)
        assert not (tmp_path / "managed" / RUN / "disposal").exists()


@pytest.mark.parametrize("name", [FactName.BOUNDARY_EMPTY, FactName.STDOUT_END, FactName.STDERR_END])
def test_each_missing_terminal_fact_is_not_ready(tmp_path: Path, name: FactName) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        (tmp_path / "managed" / RUN / name.value).unlink()
        assert not store.dispose(launch)
        assert not (tmp_path / "managed" / RUN / "disposal").exists()


def test_stale_or_malformed_fact_refuses_before_commit(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        leaf = directory / "stdout-end"
        leaf.unlink()
        leaf.write_bytes(_fact(FactName.STDOUT_END, _launch("other")))
        leaf.chmod(0o400)
        with pytest.raises(StoreError):
            store.dispose(launch)
        leaf.unlink()
        leaf.write_bytes(b"partial")
        leaf.chmod(0o400)
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert not (directory / "disposal").exists()


@pytest.mark.parametrize("stage_kind", ["fact", "request"])
def test_partial_and_linked_crash_stages_are_cleaned(tmp_path: Path, stage_kind: str) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        name = f".{stage_kind}-stage-" + "d" * 32
        if stage_kind == "fact":
            os.link(directory / "launch", directory / name)
        else:
            (directory / name).write_bytes(b"partial")
            (directory / name).chmod(0o400)
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]


@pytest.mark.parametrize(
    "removed", ["launch", "wait", "boundary-empty", "request-stop", "stdout", ".fact-stage-" + "d" * 32]
)
def test_each_partial_cleanup_position_resumes(tmp_path: Path, removed: str) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        store.publish_stop_request(launch)
        directory = tmp_path / "managed" / RUN
        (directory / "stdout").write_bytes(b"corrupt")
        (directory / "stdout").chmod(0o600)
        (directory / (".fact-stage-" + "d" * 32)).write_bytes(b"partial")
        (directory / (".fact-stage-" + "d" * 32)).chmod(0o400)
        os.link(directory / "launch", directory / "disposal")
        (directory / removed).unlink()
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]


@pytest.mark.parametrize("name", ["unknown", "fifo", ".fact-stage-" + "d" * 32])
def test_unsafe_object_refuses_before_commit(tmp_path: Path, name: str) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        if name == "fifo":
            os.mkfifo(directory / name)
        else:
            (directory / name).write_bytes(b"partial")
            if name.startswith(".fact"):
                (directory / name).chmod(0o600)
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert (directory / "launch").exists()
        assert not (directory / "disposal").exists()


def test_partial_cleanup_retry_and_simultaneous_exact_retry(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        os.link(directory / "launch", directory / "disposal")
        (directory / "stdout-end").unlink()
        (directory / "stderr-end").unlink()
        outcomes: list[bool] = []
        errors: list[Exception] = []

        def attempt() -> None:
            try:
                outcomes.append(store.dispose(launch))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]
        assert outcomes or errors


def test_receipt_and_fact_stage_after_launch_removed_resumes(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        os.link(directory / "launch", directory / "disposal")
        os.link(directory / "launch", directory / (".fact-stage-" + "d" * 32))
        (directory / "launch").unlink()
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]


def test_missing_launch_and_receipt_never_proves_disposal(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        (tmp_path / "managed" / RUN / "launch").unlink()
        with pytest.raises(StoreError):
            store.dispose(launch)


def test_open_capture_writer_refuses_after_observed_receipt(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        writer = store.open_capture(Stream.STDOUT, 8)
        launch = _terminal(store)
        try:
            assert store.dispose(launch)
            with pytest.raises(StoreError):
                writer.write(b"late")
            with pytest.raises(StoreError):
                writer.finish()
        finally:
            writer.close()


def test_transcript_requires_exact_receipt_and_complete_success() -> None:
    launch = _launch()
    collector = _Collector(launch)
    collector.accept(FileRecord(0, FileRecordKind.RESULT, encode_result(DisposalResult.DISPOSED)))
    collector.accept(FileRecord(1, FileRecordKind.DATA, launch))
    collector.accept(FileRecord(2, FileRecordKind.FINISHED, b""))
    assert (
        collector.finish(
            None,
            complete=True,
            stderr_noise=False,
            dispatch=Dispatch.SENT,
            completion=ExitStatus(code=0),
            carrier_failure=None,
        ).state
        is DisposalState.DISPOSED
    )
    assert (
        collector.finish(
            None,
            complete=False,
            stderr_noise=False,
            dispatch=Dispatch.SENT,
            completion=ExitStatus(code=0),
            carrier_failure=None,
        ).state
        is DisposalState.INCOMPLETE
    )
    bad = _Collector(launch)
    bad.accept(FileRecord(0, FileRecordKind.FAILED, b""))
    bad.accept(FileRecord(1, FileRecordKind.FINISHED, b""))
    assert (
        bad.finish(
            None,
            complete=True,
            stderr_noise=False,
            dispatch=Dispatch.SENT,
            completion=ExitStatus(code=0),
            carrier_failure=None,
        ).state
        is DisposalState.UNKNOWN
    )


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_source_python_311(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    request = encode_request(DisposalRequest(NONCE, _launch(), IdentityExpectation(0, 0, (0,))))
    source = build_helper_modules(
        "_agw_disposal_parity",
        (
            "_helper_identity",
            "_managed_job_wire",
            "_managed_job_request",
            "_managed_job_store",
            "_file_wire",
            "_managed_observation_protocol",
            "_managed_disposal_store",
            "_managed_disposal_protocol",
        ),
    )
    source += (
        "p=sys.modules['_agw_disposal_parity._managed_disposal_protocol']\n"
        "data=bytes.fromhex(sys.argv[1])\n"
        "assert p.encode_request(p.decode_request(data))==data\n"
    )
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, request.hex()],
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert decode_request(request).expected_launch == _launch()
    assert FIXED_BUNDLE.bootstrap
    helper = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", FIXED_BUNDLE.bootstrap, NONCE],
        input=FIXED_BUNDLE.prefix + b"invalid-request",
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert helper.returncode == 0
    assert helper.stderr == b""
    assert b"FAILED" in helper.stdout and b"FINISHED" in helper.stdout
