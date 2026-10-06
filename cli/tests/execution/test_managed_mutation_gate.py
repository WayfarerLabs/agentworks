"""Cooperative publication ordering on real independently opened run directories."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from agentworks.execution import _managed_lease_store as leases
from agentworks.execution._managed_job_store import FactName, StoreError, _acquire_mutation_gate, _write_all
from agentworks.execution._managed_lease_wire import LeaseError, sampled_lease

from .test_managed_disposal import _fact
from .test_managed_operation_lease import job, launch
from .test_managed_service_guest import RUN, _store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux cooperative run directory locks")


@pytest.mark.parametrize("closure", ["stop", "dispose"])
@pytest.mark.parametrize("pause", ["before", "during"])
def test_publisher_and_permanent_closure_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closure: str, pause: str
) -> None:
    receipt = launch()
    initial, renewal = sampled_lease(receipt, 1000), sampled_lease(receipt, 2000)
    monkeypatch.setattr(leases, "boottime_ns", lambda: 3000)
    entered, resume = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    with _store(tmp_path) as store:
        store.publish_request(job())
        leases.publish_initial_lease(store, receipt, initial)
        store.publish_fact(FactName.LAUNCH, receipt)
        for name in (FactName.BOUNDARY_EMPTY, FactName.STDOUT_END, FactName.STDERR_END):
            store.publish_fact(name, _fact(name, receipt))
        root = tmp_path / "managed" / RUN
        inode = root.stat().st_ino
        real = _acquire_mutation_gate if pause == "before" else _write_all

        def paused(*args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("publication barrier")
            return real(*args, **kwargs)

        monkeypatch.setattr(leases, "_acquire_mutation_gate" if pause == "before" else "_write_all", paused)

        def publish() -> None:
            try:
                leases.publish_lease(store, receipt, renewal)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=publish)
        worker.start()
        try:
            assert entered.wait(5)
            action = (
                (lambda: store.publish_stop_request(receipt)) if closure == "stop" else lambda: store.dispose(receipt)
            )
            if pause == "during":
                before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()}
                with pytest.raises(StoreError):
                    action()
                assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()} == before
                assert not store.read_stop_request() and not (root / "disposal").exists()
            else:
                action()
                before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()}
        finally:
            resume.set()
            worker.join(5)
        assert not worker.is_alive()
        if pause == "during":
            assert not failures
            assert leases.read_lease(store, receipt) == renewal
            action()
        else:
            assert len(failures) == 1 and isinstance(failures[0], StoreError)
            assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()} == before
        with pytest.raises(StoreError):
            leases.publish_lease(store, receipt, initial if pause == "before" else renewal)
        assert root.stat().st_ino == inode
        if closure == "dispose":
            stage = root / (".lease-stage-" + "e" * 32)
            stage.write_bytes(b"partial")
            stage.chmod(0o400)
            assert store.dispose(receipt)
            assert [p.name for p in root.iterdir()] == ["disposal"]
            assert root.stat().st_ino == inode


def test_expiry_crossed_before_gate_refuses_without_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = launch()
    renewal = sampled_lease(receipt, 2000)
    clock = [3000]
    monkeypatch.setattr(leases, "boottime_ns", lambda: clock[0])
    entered, resume = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    with _store(tmp_path) as store:
        store.publish_request(job())
        leases.publish_initial_lease(store, receipt, sampled_lease(receipt, 1000))
        store.publish_fact(FactName.LAUNCH, receipt)
        root = tmp_path / "managed" / RUN
        before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()}
        real = _acquire_mutation_gate

        def delayed(directory: int) -> None:
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("publication barrier")
            real(directory)

        monkeypatch.setattr(leases, "_acquire_mutation_gate", delayed)

        def publish() -> None:
            try:
                leases.publish_lease(store, receipt, renewal)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=publish)
        worker.start()
        try:
            assert entered.wait(5)
            clock[0] = renewal.expires_ns
        finally:
            resume.set()
            worker.join(5)
        assert not worker.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], LeaseError)
        assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()} == before


@pytest.mark.parametrize("fault", ["partial", "lease-mismatch", "launched"])
def test_initial_publication_requires_complete_exact_unlaunched_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from agentworks.execution._managed_job_store import RequestAsset

    receipt = launch()
    monkeypatch.setattr(leases, "boottime_ns", lambda: 3000)
    with _store(tmp_path) as store:
        store.publish_request(job())
        if fault == "partial":
            (tmp_path / "managed" / RUN / RequestAsset.STDIN.value).unlink()
        elif fault == "launched":
            store.publish_fact(FactName.LAUNCH, receipt)
        sample = 2000 if fault == "lease-mismatch" else 1000
        with pytest.raises(StoreError):
            leases.publish_initial_lease(store, receipt, sampled_lease(receipt, sample))
        assert leases.read_lease(store, receipt) is None
        assert not any(leases.LEASE_STAGE.fullmatch(p.name) for p in (tmp_path / "managed" / RUN).iterdir())


def test_renewal_requires_launch_inside_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = launch()
    monkeypatch.setattr(leases, "boottime_ns", lambda: 3000)
    with _store(tmp_path) as store:
        store.publish_request(job())
        with pytest.raises(StoreError):
            leases.publish_lease(store, receipt, sampled_lease(receipt, 1000))
        assert leases.read_lease(store, receipt) is None


@pytest.mark.parametrize("read", ["request", "launch", "previous", "duplicate"])
def test_final_sample_follows_all_binding_and_previous_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    receipt = launch()
    proposed = sampled_lease(receipt, 1000)
    clock = [3000]
    monkeypatch.setattr(leases, "boottime_ns", lambda: clock[0])
    with _store(tmp_path) as store:
        store.publish_request(job())
        if read != "request":
            leases.publish_initial_lease(store, receipt, proposed)
            store.publish_fact(FactName.LAUNCH, receipt)
        root = tmp_path / "managed" / RUN
        before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()}
        owner = store if read in {"request", "launch"} else leases
        name = "read_request" if read == "request" else "read_fact" if read == "launch" else "read_lease"
        real = getattr(owner, name)

        def delayed(*args, **kwargs):
            result = real(*args, **kwargs)
            clock[0] = proposed.expires_ns
            return result

        monkeypatch.setattr(owner, name, delayed)
        publisher = leases.publish_initial_lease if read == "request" else leases.publish_lease
        if read in {"launch", "previous"}:
            proposed = sampled_lease(receipt, 2000)
        with pytest.raises(LeaseError):
            publisher(store, receipt, proposed)
        assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in root.iterdir()} == before


def test_lost_stop_commit_reconciles_and_keeps_lease_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = launch()
    initial = sampled_lease(receipt, 1000)
    monkeypatch.setattr(leases, "boottime_ns", lambda: 3000)
    with _store(tmp_path) as store:
        store.publish_request(job())
        leases.publish_initial_lease(store, receipt, initial)
        store.publish_fact(FactName.LAUNCH, receipt)
        real = os.fsync
        failure = KeyboardInterrupt()
        calls = 0

        def failed_commit(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise failure
            real(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", failed_commit)
            with pytest.raises(KeyboardInterrupt) as raised:
                store.publish_stop_request(receipt)
            assert raised.value is failure
        assert store.read_stop_request()
        store.publish_stop_request(receipt)
        with pytest.raises(StoreError):
            leases.publish_lease(store, receipt, initial)
        assert leases.read_lease(store, receipt) == initial


def test_independent_descriptions_contend_and_dup_shares_custody(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.publish_request(job())
        first, second = store._run_dir(create=False), store._run_dir(create=False)
        assert first is not None and second is not None
        duplicate: int | None = os.dup(first)
        try:
            _acquire_mutation_gate(first)
            _acquire_mutation_gate(duplicate)
            with pytest.raises(StoreError):
                _acquire_mutation_gate(second)
            os.close(first)
            first = None
            with pytest.raises(StoreError):
                _acquire_mutation_gate(second)
            os.close(duplicate)
            duplicate = None
            _acquire_mutation_gate(second)
        finally:
            if first is not None:
                os.close(first)
            if duplicate is not None:
                os.close(duplicate)
            os.close(second)
