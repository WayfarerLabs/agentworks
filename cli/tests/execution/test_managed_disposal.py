"""Private exact-run disposal and receipt recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_request as request_wire
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_disposal_bundle import FIXED_BUNDLE
from agentworks.execution._managed_disposal_exchange import (
    DisposalCandidate,
    DisposalState,
    _Collector,
    dispose_managed_run,
)
from agentworks.execution._managed_disposal_protocol import (
    DisposalRequest,
    DisposalResult,
    decode_request,
    encode_request,
    encode_result,
)
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, RequestAsset, StoreError, Stream
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

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


@pytest.mark.parametrize(
    "variant",
    [
        "nonempty_stop",
        "conflicting_launch",
        "malformed_launch",
        "malformed_control",
        "wrong_run_control",
        "malformed_environment",
        "oversize_source",
        "oversize_stdin",
        "cross_asset_mismatch",
    ],
)
def test_malformed_fixed_request_final_refuses_before_deletion(tmp_path: Path, variant: str) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        if variant == "cross_asset_mismatch":
            store.publish_request(
                request_wire.ManagedJobRequest(launch, "command", ("/bin/true",), None, "discard", None, (), b"", b"")
            )
            leaf = directory / RequestAsset.SOURCE.value
            leaf.unlink()
            data = b"changed"
        elif variant == "nonempty_stop":
            leaf, data = directory / "request-stop", b"x"
        elif variant in ("conflicting_launch", "malformed_launch"):
            leaf = directory / RequestAsset.LAUNCH.value
            data = _launch("other") if variant == "conflicting_launch" else b"invalid"
        elif variant in ("malformed_control", "wrong_run_control"):
            leaf = directory / RequestAsset.CONTROL.value
            if variant == "malformed_control":
                data = b"invalid"
            else:
                valid = request_wire.encode_request(
                    request_wire.ManagedJobRequest(
                        launch, "command", ("/bin/true",), None, "discard", None, (), b"", b""
                    )
                )[RequestAsset.CONTROL.value]
                control = json.loads(valid)
                control["run_id"] = "b" * 32
                data = json.dumps(control, sort_keys=True, separators=(",", ":")).encode("ascii")
        elif variant == "malformed_environment":
            leaf, data = directory / RequestAsset.ENVIRONMENT.value, b"invalid"
        else:
            name = RequestAsset.SOURCE if variant == "oversize_source" else RequestAsset.STDIN
            leaf, data = directory / name.value, b"x" * (request_wire.MAX_SOURCE_BYTES + 1)
        leaf.write_bytes(data)
        leaf.chmod(0o400)
        before = {item.name for item in directory.iterdir()}
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert {item.name for item in directory.iterdir()} == before
        assert (directory / "launch").read_bytes() == launch
        assert not (directory / "disposal").exists()


def test_partial_binary_request_assets_remain_disposable(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        store.publish_request_asset(RequestAsset.SOURCE, b"\x00\xff\x80")
        store.publish_request_asset(RequestAsset.STDIN, b"\xff\x00")
        assert store.dispose(launch)
        assert sorted(item.name for item in (tmp_path / "managed" / RUN).iterdir()) == ["disposal"]


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


@pytest.mark.parametrize("variant", ["symlink", "directory", "fifo", "mode", "hardlink"])
def test_unsafe_known_object_refuses_before_commit(tmp_path: Path, variant: str) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        if variant == "hardlink":
            os.link(directory / "launch", tmp_path / "external-link")
        else:
            leaf = directory / "stdout"
            if variant == "symlink":
                leaf.symlink_to("launch")
            elif variant == "directory":
                leaf.mkdir()
            elif variant == "fifo":
                os.mkfifo(leaf)
            else:
                leaf.write_bytes(b"capture")
                leaf.chmod(0o644)
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert (directory / "launch").read_bytes() == launch
        assert not (directory / "disposal").exists()


def test_mismatched_disposal_receipt_refuses_without_deletion(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        receipt = directory / "disposal"
        receipt.write_bytes(_launch("other"))
        receipt.chmod(0o400)
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert (directory / "launch").read_bytes() == launch
        assert (directory / "boundary-empty").exists()


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
        assert outcomes
        assert all(isinstance(error, StoreError) for error in errors)


def test_receipt_link_loses_to_exact_disposer_after_launch_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        link = os.link
        calls = 0

        def raced_link(source: str, target: str, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            assert source == "launch" and target == "disposal"
            link(directory / "launch", directory / "disposal")
            for leaf in directory.iterdir():
                if leaf.name != "disposal":
                    leaf.unlink()
            raise FileNotFoundError("launch removed by exact disposer")

        monkeypatch.setattr(os, "link", raced_link)
        assert store.dispose(launch)
        assert calls == 1
        assert store.dispose(launch)
        assert sorted(leaf.name for leaf in directory.iterdir()) == ["disposal"]


def test_receipt_and_fact_stage_after_launch_removed_resumes(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = tmp_path / "managed" / RUN
        os.link(directory / "launch", directory / "disposal")
        os.link(directory / "launch", directory / (".fact-stage-" + "d" * 32))
        (directory / "launch").unlink()
        assert store.dispose(launch)
        assert sorted(p.name for p in directory.iterdir()) == ["disposal"]


def test_existing_receipt_requires_directory_sync_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _store(tmp_path) as store:
        launch = _terminal(store)
        store.publish_request_asset(RequestAsset.SOURCE, b"payload")
        directory = tmp_path / "managed" / RUN
        os.link(directory / "launch", directory / "disposal")
        before = {leaf.name: leaf.read_bytes() for leaf in directory.iterdir()}
        assert len(before) > 1

        def failed_sync(fd: int) -> None:
            raise OSError("injected receipt barrier failure")

        monkeypatch.setattr(os, "fsync", failed_sync)
        with pytest.raises(OSError):
            store.dispose(launch)
        assert {leaf.name: leaf.read_bytes() for leaf in directory.iterdir()} == before


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


def _records(nonce: str, entries: tuple[tuple[FileRecordKind, bytes], ...]) -> bytes:
    return b"".join(
        encode_file_record(nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(entries)
    )


def _disposed(request: DisposalRequest, receipt: bytes | None = None) -> bytes:
    return _records(
        request.nonce,
        (
            (FileRecordKind.RESULT, encode_result(DisposalResult.DISPOSED)),
            (FileRecordKind.DATA, request.expected_launch if receipt is None else receipt),
            (FileRecordKind.FINISHED, b""),
        ),
    )


class ExchangeCarrier:
    def __init__(
        self,
        response: Callable[[DisposalRequest], bytes],
        *,
        dispatch: Dispatch = Dispatch.SENT,
        code: int | None = 0,
        complete: bool = True,
        retention: Retention = Retention.DELIVERED,
        stderr: bytes = b"",
        failure: Failure | None = None,
        refuse_validation: bool = False,
    ) -> None:
        self.response = response
        self.dispatch = dispatch
        self.code = code
        self.complete = complete
        self.retention = retention
        self.stderr = stderr
        self.failure = failure
        self.refuse_validation = refuse_validation
        self.validations = 0
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1
        assert isinstance(io.input, FiniteInput) and io.input.data.startswith(FIXED_BUNDLE.prefix)
        assert io.input.sensitive and io.sensitive
        assert isinstance(io.output, SinkOutput)
        if self.refuse_validation:
            raise ValidationError("unsupported I/O")

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + self.response(request)
        remaining = memoryview(payload)
        while remaining:
            consumed = io.output.stdout.try_write(remaining)
            assert consumed is not None and consumed > 0
            remaining = remaining[consumed:]
        if self.stderr:
            io.output.stderr.try_write(memoryview(self.stderr))
        channel = CapturedOutput(complete=self.complete, retention=self.retention)
        completion = ExitStatus(code=self.code) if self.dispatch is Dispatch.SENT and self.code is not None else None
        return CarrierReport(self.dispatch, completion, stdout=channel, stderr=channel, failure=self.failure)


def _exchange(carrier: ExchangeCarrier, *, deadline: Deadline | None = None) -> DisposalCandidate:
    return dispose_managed_run(
        carrier,
        expected_launch=_launch(),
        plan=IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        deadline=deadline or Deadline.after(10),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
    )


@pytest.mark.parametrize("result", [DisposalResult.DISPOSED, DisposalResult.NOT_READY])
def test_host_exchange_admits_only_complete_result(result: DisposalResult) -> None:
    def respond(request: DisposalRequest) -> bytes:
        if result is DisposalResult.DISPOSED:
            return _disposed(request)
        return _records(
            request.nonce,
            ((FileRecordKind.RESULT, encode_result(result)), (FileRecordKind.FINISHED, b"")),
        )

    carrier = ExchangeCarrier(respond)
    candidate = _exchange(carrier)
    assert candidate.observation is not None
    assert candidate.observation.state is DisposalState(result.value)
    assert candidate.dispatch is Dispatch.SENT
    assert carrier.validations == carrier.calls == 1
    assert _launch() not in repr(candidate).encode()
    assert _launch().hex() not in repr(candidate)


@pytest.mark.parametrize(
    ("fault", "state"),
    [
        ("failed", DisposalState.UNKNOWN),
        ("unknown", DisposalState.UNKNOWN),
        ("not_sent", DisposalState.INCOMPLETE),
        ("partial", DisposalState.INCOMPLETE),
        ("malformed", DisposalState.INCOMPLETE),
        ("partial_receipt", DisposalState.INCOMPLETE),
        ("malformed_receipt", DisposalState.INCOMPLETE),
        ("stale_receipt", DisposalState.INCOMPLETE),
        ("stderr", DisposalState.UNKNOWN),
        ("nonzero", DisposalState.UNKNOWN),
        ("incomplete", DisposalState.INCOMPLETE),
        ("retention", DisposalState.INCOMPLETE),
        ("carrier_loss", DisposalState.UNKNOWN),
    ],
)
def test_host_exchange_preserves_uncertain_evidence(fault: str, state: DisposalState) -> None:
    def respond(request: DisposalRequest) -> bytes:
        if fault == "failed":
            return _records(
                request.nonce,
                ((FileRecordKind.FAILED, b""), (FileRecordKind.FINISHED, b"")),
            )
        if fault == "partial":
            return _records(request.nonce, ((FileRecordKind.RESULT, encode_result(DisposalResult.DISPOSED)),))
        if fault == "malformed":
            return _records(
                request.nonce,
                ((FileRecordKind.RESULT, b"bad"), (FileRecordKind.FINISHED, b"")),
            )
        receipts = {
            "partial_receipt": request.expected_launch[:20],
            "malformed_receipt": b"invalid",
            "stale_receipt": _launch("other"),
        }
        return _disposed(request, receipts.get(fault))

    carrier = ExchangeCarrier(
        respond,
        dispatch={"unknown": Dispatch.UNKNOWN, "not_sent": Dispatch.NOT_SENT}.get(fault, Dispatch.SENT),
        code=1 if fault == "nonzero" else 0,
        complete=fault != "incomplete",
        retention=Retention.DISCARDED if fault == "retention" else Retention.DELIVERED,
        stderr=b"noise" if fault == "stderr" else b"",
        failure=Failure.OUTPUT if fault == "carrier_loss" else None,
    )
    candidate = _exchange(carrier)
    assert candidate.observation is not None and candidate.observation.state is state
    assert carrier.validations == carrier.calls == 1


def test_host_exchange_refuses_before_dispatch() -> None:
    carrier = ExchangeCarrier(_disposed)
    expired = _exchange(carrier, deadline=Deadline.after(0))
    assert expired.dispatch is Dispatch.NOT_SENT and expired.observation is None
    assert carrier.validations == 1 and carrier.calls == 0
    for plan, runtime in (
        (
            IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
            RuntimeSelection(RuntimeTargetOS.LINUX),
        ),
        (
            IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
            RuntimeSelection(RuntimeTargetOS.DARWIN),
        ),
    ):
        with pytest.raises(ValidationError):
            dispose_managed_run(
                carrier,
                expected_launch=_launch(),
                plan=plan,
                deadline=Deadline.after(1),
                runtime_selection=runtime,
            )
    assert carrier.validations == 1 and carrier.calls == 0
    refusing = ExchangeCarrier(_disposed, refuse_validation=True)
    with pytest.raises(ValidationError):
        _exchange(refusing)
    assert refusing.validations == 1 and refusing.calls == 0


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
