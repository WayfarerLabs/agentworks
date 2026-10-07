"""Private guest operation lease boundaries with actual protected store files."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from agentworks.execution import _managed_job_request as requests
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_lease_controller as controller
from agentworks.execution import _managed_lease_guest as guest
from agentworks.execution import _managed_lease_store as store_wire
from agentworks.execution import _managed_lease_wire as lease_wire
from agentworks.execution import _managed_start_guest as start
from agentworks.execution._file_wire import FileRecordKind
from agentworks.execution._managed_disposal_store import _inventory
from agentworks.execution._managed_job_store import FactName, RequestAsset, StoreError
from agentworks.execution._managed_lease_protocol import (
    ClockObservation,
    LeasePublication,
    LeaseRequest,
    decode_request,
    decode_result,
    encode_request,
    encode_result,
)
from agentworks.execution._managed_start_protocol import ManagedStartRequest
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id

from .test_managed_disposal import _fact
from .test_managed_service_guest import RUN, _launch, _store
from .test_managed_start import GUEST, NONCE, ROOT

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux protected guest control")


@pytest.fixture(autouse=True)
def publication_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_wire, "boottime_ns", lambda: 30_000_000_000)


def launch() -> bytes:
    value = wire.decode_fact(_launch())
    value["owner"] = {"kind": "operation", "owner_id": "c" * 32}
    value["lifetime"] = "operation"
    cast("dict[str, object]", value["target"])["boot_id"] = vm_guest_boot_id(GUEST)
    return wire.encode_fact(value)


def job(sample: int = 1000) -> requests.ManagedJobRequest:
    receipt = launch()
    return requests.ManagedJobRequest(
        receipt,
        "command",
        ("/bin/true",),
        None,
        "discard",
        None,
        (),
        b"",
        b"",
        lease_wire.sampled_lease(receipt, sample),
    )


def test_operation_request_requires_exact_lease_and_independent_cannot_convert() -> None:
    request = job()
    assert requests.decode_request(requests.encode_request(request)) == request
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(request, operation_lease=None))
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(request, launch=_launch()))
    assert "operation_lease" not in requests.decode_control(
        requests.encode_request(replace(request, launch=_launch(), operation_lease=None))["request-control"]
    )


@pytest.mark.parametrize("field", ["run_id", "receipt_sha256", "boot_id"])
def test_exact_lease_binding_rejects_foreign_fields(field: str) -> None:
    initial = lease_wire.sampled_lease(launch(), 1000)
    value = {"run_id": "b" * 32, "receipt_sha256": "b" * 64, "boot_id": GUEST.boot_id}[field]
    with pytest.raises(lease_wire.LeaseError):
        lease_wire.checked_lease(replace(initial, **{field: value}), launch(), 1000)


@pytest.mark.parametrize(
    "sample", [True, -1, 1.0, None, lease_wire.MAX_CLOCK_NS + 1, lease_wire.MAX_CLOCK_NS - lease_wire.WINDOW_NS + 1]
)
def test_clock_and_expiry_bounds(sample: object) -> None:
    with pytest.raises(lease_wire.LeaseError):
        lease_wire.sampled_lease(launch(), sample)


@pytest.mark.parametrize("fault", ["extra", "boolean", "overflow", "window", "duplicate", "oversize"])
def test_persisted_lease_decode_is_closed_and_bounded(fault: str) -> None:
    data = lease_wire.encode_lease(lease_wire.sampled_lease(launch(), 1000))
    value = json.loads(data)
    if fault == "extra":
        value["extra"] = 1
    elif fault == "boolean":
        value["sampled_ns"] = True
    elif fault == "overflow":
        value["expires_ns"] = lease_wire.MAX_CLOCK_NS + 1
    elif fault == "window":
        value["expires_ns"] += 1
    elif fault == "duplicate":
        data = data[:-1] + b',"version":1}'
    else:
        data = b" " * (lease_wire.MAX_LEASE_BYTES + 1)
    if fault not in {"duplicate", "oversize"}:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(lease_wire.LeaseError):
        lease_wire.decode_lease(data)


@pytest.mark.parametrize("now", [999, 1000 + lease_wire.WINDOW_NS])
def test_future_or_expired_sample_is_not_reminted(now: int) -> None:
    with pytest.raises(lease_wire.LeaseError):
        lease_wire.checked_lease(lease_wire.sampled_lease(launch(), 1000), launch(), now)


def test_missing_boot_clock_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(time, "CLOCK_BOOTTIME")
    with pytest.raises(lease_wire.LeaseError):
        lease_wire.boottime_ns()


def test_initial_expiry_prevents_assets_and_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = job()
    monkeypatch.setattr(start, "boottime_ns", lambda: 1000 + lease_wire.WINDOW_NS)
    calls: list[tuple[str, ...]] = []
    with _store(tmp_path) as store:
        with pytest.raises(lease_wire.LeaseError):
            start._prepare_start(
                ManagedStartRequest(NONCE, ROOT, request, GUEST),
                store,
                python=sys.executable,
                runner=lambda argv: calls.append(argv),
            )
        assert all(store.read_request_asset(name) is None for name in RequestAsset)
        assert not calls and store.read_fact(FactName.LAUNCH) is None
        assert not (tmp_path / "managed" / RUN).exists()


def test_exact_publisher_stores_original_sample_and_duplicate_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 1000)
    renewal = lease_wire.sampled_lease(receipt, 2000)
    monkeypatch.setattr(store_wire, "boottime_ns", lambda: 3000)
    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, initial)
        request = LeaseRequest(NONCE, ROOT, GUEST, receipt, renewal)
        assert decode_request(encode_request(request)) == request
        assert guest._publish(request, store) == LeasePublication(renewal.expires_ns)
        assert guest._publish(request, store) == LeasePublication(renewal.expires_ns)
        assert store_wire.read_lease(store, receipt) == renewal
        leaf = tmp_path / "managed" / RUN / store_wire.LEASE_LEAF
        assert leaf.stat().st_mode & 0o777 == 0o400 and leaf.stat().st_nlink == 1
        with pytest.raises(StoreError):
            store_wire.publish_lease(store, receipt, initial)


@pytest.mark.parametrize("fault", ["absent", "malformed", "wrong-boot", "duplicate", "unavailable"])
def test_invalid_record_never_extends_remembered_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 1000)
    clock = [2000]
    monkeypatch.setattr(controller, "boottime_ns", lambda: clock[0])
    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        if fault != "absent":
            store_wire.publish_lease(store, receipt, initial)
        if fault in {"malformed", "wrong-boot"}:
            leaf = tmp_path / "managed" / RUN / store_wire.LEASE_LEAF
            os.chmod(leaf, 0o600)
            leaf.write_bytes(
                b"bad" if fault == "malformed" else lease_wire.encode_lease(replace(initial, boot_id=GUEST.boot_id))
            )
            os.chmod(leaf, 0o400)
        if fault == "unavailable":
            monkeypatch.setattr(controller, "read_lease", lambda *_: (_ for _ in ()).throw(OSError()))
        control = controller.LeaseControl(receipt, initial.expires_ns)
        assert not control.stop_due(store)
        assert control.expires_ns == initial.expires_ns
        clock[0] = initial.expires_ns
        assert control.stop_due(store) and control.closed


def test_remembered_expiry_checked_before_new_record_and_stop_latches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 1000)
    renewal = lease_wire.sampled_lease(receipt, 2000)
    clock = [2000]
    monkeypatch.setattr(controller, "boottime_ns", lambda: clock[0])
    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, renewal)
        control = controller.LeaseControl(receipt, initial.expires_ns)
        assert not control.stop_due(store) and control.expires_ns == renewal.expires_ns
        stalled = controller.LeaseControl(receipt, initial.expires_ns)
        clock[0] = initial.expires_ns
        assert stalled.stop_due(store) and stalled.expires_ns == initial.expires_ns
        clock[0] = 2000
        assert stalled.stop_due(store)
        control.closed = True
        assert control.stop_due(store)


@pytest.mark.parametrize("fault", ["valid", "absent", "malformed", "unavailable", "clock-unavailable", "preexpired"])
def test_read_crossing_remembered_expiry_latches_before_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 0)
    renewal = lease_wire.sampled_lease(receipt, 30_000_000_000)
    clock = [initial.expires_ns if fault == "preexpired" else 59_000_000_000]

    def now() -> int:
        if fault == "clock-unavailable" and clock[0] > initial.expires_ns:
            raise lease_wire.LeaseError("unavailable")
        return clock[0]

    monkeypatch.setattr(controller, "boottime_ns", now)
    real_read = store_wire.read_lease

    def delayed_read(store, expected):
        try:
            if fault == "preexpired":
                pytest.fail("expired control read the store")
            if fault == "unavailable":
                raise OSError
            return real_read(store, expected)
        finally:
            clock[0] = initial.expires_ns + 1

    monkeypatch.setattr(controller, "read_lease", delayed_read)
    with _store(tmp_path) as store:
        store.publish_request(job(0))
        store.publish_fact(FactName.LAUNCH, receipt)
        if fault != "absent":
            store_wire.publish_lease(store, receipt, renewal)
        if fault == "malformed":
            leaf = tmp_path / "managed" / RUN / store_wire.LEASE_LEAF
            os.chmod(leaf, 0o600)
            leaf.write_bytes(b"bad")
            os.chmod(leaf, 0o400)
        control = controller.LeaseControl(receipt, initial.expires_ns)
        assert control.stop_due(store) and control.closed
        assert control.expires_ns == initial.expires_ns
        clock[0] = 59_000_000_000
        monkeypatch.setattr(controller, "read_lease", lambda *_: pytest.fail("closed control read again"))
        assert control.stop_due(store)


@pytest.mark.parametrize("sample,after,accepted", [(20, 20, True), (21, 20, False), (0, 61, False)])
def test_read_candidate_freshness_uses_post_read_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample: int, after: int, accepted: bool
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 10_000_000_000)
    candidate = lease_wire.sampled_lease(receipt, sample * 1_000_000_000)
    clock = [19_000_000_000]
    monkeypatch.setattr(controller, "boottime_ns", lambda: clock[0])
    real_read = store_wire.read_lease

    def delayed_read(store, expected):
        result = real_read(store, expected)
        clock[0] = after * 1_000_000_000
        return result

    monkeypatch.setattr(controller, "read_lease", delayed_read)
    with _store(tmp_path) as store:
        store.publish_request(job(10_000_000_000))
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, candidate)
        control = controller.LeaseControl(receipt, initial.expires_ns)
        assert not control.stop_due(store) and not control.closed
        assert control.expires_ns == (candidate.expires_ns if accepted else initial.expires_ns)


@pytest.mark.parametrize("after_replace", [False, True])
def test_interrupted_replacement_leaves_bounded_recognized_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool
) -> None:
    receipt = launch()
    initial, renewal = lease_wire.sampled_lease(receipt, 1000), lease_wire.sampled_lease(receipt, 2000)
    real = os.replace
    interrupted = KeyboardInterrupt()

    def fail(*args, **kwargs):
        if after_replace:
            real(*args, **kwargs)
        raise interrupted

    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, initial)
        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(KeyboardInterrupt) as raised:
            store_wire.publish_lease(store, receipt, renewal)
        assert raised.value is interrupted
        assert store_wire.read_lease(store, receipt) == (renewal if after_replace else initial)
        directory = store._run_dir(create=False)
        assert directory is not None
        try:
            inventory = _inventory(directory, os.getuid())
            assert sum(bool(store_wire.LEASE_STAGE.fullmatch(name)) for name in inventory) == int(not after_replace)
        finally:
            os.close(directory)


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "mode", "unknown"])
def test_unsafe_control_inventory_refuses(tmp_path: Path, fault: str) -> None:
    receipt = launch()
    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, lease_wire.sampled_lease(receipt, 1000))
        root = tmp_path / "managed" / RUN
        leaf = root / store_wire.LEASE_LEAF
        if fault == "symlink":
            leaf.unlink()
            leaf.symlink_to(root / "request-control")
        elif fault == "hardlink":
            os.link(leaf, root / (".lease-stage-" + "d" * 32))
        elif fault == "mode":
            leaf.chmod(0o600)
        else:
            (root / "foreign").touch(mode=0o400)
        directory = store._run_dir(create=False)
        assert directory is not None
        try:
            if fault != "unknown":
                with pytest.raises(StoreError):
                    store_wire.read_lease(store, receipt)
                with pytest.raises(StoreError):
                    store_wire.publish_lease(store, receipt, lease_wire.sampled_lease(receipt, 2000))
            with pytest.raises(StoreError):
                _inventory(directory, os.getuid())
        finally:
            os.close(directory)


def test_clock_response_and_publication_are_distinct_typed_facts() -> None:
    assert decode_result(encode_result(ClockObservation(1000))) == ClockObservation(1000)
    assert decode_result(encode_result(LeasePublication(lease_wire.WINDOW_NS))) == LeasePublication(
        lease_wire.WINDOW_NS
    )


@pytest.mark.parametrize("failure", [None, "identity", "boot", "clock"])
def test_clock_helper_fences_before_sampling_without_run_store(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    records: list[tuple[FileRecordKind, bytes]] = []
    calls: list[str] = []

    class Writer:
        def __init__(self, nonce: str) -> None:
            assert nonce == NONCE

        def write(self, kind: FileRecordKind, data: bytes) -> None:
            records.append((kind, data))

    def identity():
        calls.append("identity")
        return replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1) if failure == "boot" else GUEST

    def clock():
        calls.append("clock")
        if failure == "clock":
            raise lease_wire.LeaseError()
        return 1000

    def forbidden(*args, **kwargs):
        pytest.fail("clock observation opened run store")

    monkeypatch.setattr(guest, "FileRecordWriter", Writer)
    monkeypatch.setattr(guest, "_read_request", lambda: LeaseRequest(NONCE, ROOT, GUEST))
    monkeypatch.setattr(guest, "matches_current_identity", lambda _: failure != "identity")
    monkeypatch.setattr(guest, "_identity", identity)
    monkeypatch.setattr(guest, "boottime_ns", clock)
    monkeypatch.setattr(guest, "ManagedJobStore", forbidden)
    assert guest.main(NONCE) == 0
    assert calls == ([] if failure == "identity" else ["identity"] if failure == "boot" else ["identity", "clock"])
    assert records[-1] == (FileRecordKind.FINISHED, b"")
    if failure is None:
        assert records[0][0] is FileRecordKind.RESULT
        assert decode_result(records[0][1]) == ClockObservation(1000)
    else:
        assert records[0] == (FileRecordKind.FAILED, b"")


@pytest.mark.parametrize("fault", ["future", "expired", "foreign-launch"])
def test_publisher_rejects_without_replacing_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    receipt = launch()
    initial = lease_wire.sampled_lease(receipt, 1000)
    renewal = lease_wire.sampled_lease(receipt, 2000)
    now = 1999 if fault == "future" else renewal.expires_ns if fault == "expired" else 2000
    with _store(tmp_path) as store:
        store.publish_request(job())
        store_wire.publish_initial_lease(store, receipt, initial)
        store.publish_fact(FactName.LAUNCH, _launch() if fault == "foreign-launch" else receipt)
        monkeypatch.setattr(store_wire, "boottime_ns", lambda: now)
        with pytest.raises((lease_wire.LeaseError, StoreError)):
            guest._publish(LeaseRequest(NONCE, ROOT, GUEST, receipt, renewal), store)
        assert store_wire.read_lease(store, receipt) == initial


def test_initial_expiry_after_staging_still_prevents_systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    samples = iter((1000, 1000 + lease_wire.WINDOW_NS))
    monkeypatch.setattr(start, "boottime_ns", lambda: next(samples))
    calls: list[tuple[str, ...]] = []
    with _store(tmp_path) as store:
        with pytest.raises(lease_wire.LeaseError):
            start._prepare_start(
                ManagedStartRequest(NONCE, ROOT, job(), GUEST),
                store,
                python=sys.executable,
                runner=lambda argv: calls.append(argv),
            )
        assert store.read_request() == job()
        assert store_wire.read_lease(store, launch()) == job().operation_lease
        assert not calls and store.read_fact(FactName.LAUNCH) is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_disposal_validates_exact_lease_and_removes_recognized_stage(tmp_path: Path, corrupt: bool) -> None:
    receipt = launch()
    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        for name in (FactName.BOUNDARY_EMPTY, FactName.STDOUT_END, FactName.STDERR_END):
            store.publish_fact(name, _fact(name, receipt))
        initial = lease_wire.sampled_lease(receipt, 1000)
        store_wire.publish_lease(store, receipt, initial)
        root = tmp_path / "managed" / RUN
        stage = root / (".lease-stage-" + "d" * 32)
        stage.write_bytes(b"partial")
        stage.chmod(0o400)
        if corrupt:
            leaf = root / store_wire.LEASE_LEAF
            leaf.chmod(0o600)
            leaf.write_bytes(lease_wire.encode_lease(replace(initial, receipt_sha256="d" * 64)))
            leaf.chmod(0o400)
            with pytest.raises(StoreError):
                store.dispose(receipt)
            assert stage.exists() and not (root / "disposal").exists()
        else:
            assert store.dispose(receipt)
            assert [path.name for path in root.iterdir()] == ["disposal"]


@pytest.mark.parametrize("after_replace", [False, True])
def test_interrupted_fsync_does_not_fabricate_publication_or_disposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool
) -> None:
    receipt = launch()
    initial, renewal = lease_wire.sampled_lease(receipt, 1000), lease_wire.sampled_lease(receipt, 2000)
    interrupted = SystemExit(7)
    real = os.fsync
    calls = 0

    def sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == (2 if after_replace else 1):
            raise interrupted
        real(fd)

    with _store(tmp_path) as store:
        store.publish_request(job())
        store.publish_fact(FactName.LAUNCH, receipt)
        store_wire.publish_lease(store, receipt, initial)
        monkeypatch.setattr(os, "fsync", sync)
        with pytest.raises(SystemExit) as raised:
            store_wire.publish_lease(store, receipt, renewal)
        assert raised.value is interrupted
        assert store_wire.read_lease(store, receipt) == (renewal if after_replace else initial)
        assert not (tmp_path / "managed" / RUN / "disposal").exists()
