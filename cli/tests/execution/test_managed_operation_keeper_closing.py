"""Bound CLOSING helpers preserve independent remote and local custody facts."""

from __future__ import annotations

import os
import signal
import threading
from typing import cast

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution import _managed_observation_bundle as observation_bundle
from agentworks.execution import _managed_operation_keeper as keeper_module
from agentworks.execution import _managed_stop_bundle as stop_bundle
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, launch_sha256
from agentworks.execution._managed_observation_protocol import (
    ControllerObservation,
    ControllerState,
    ManagedObservationRequest,
    ManagedResultControl,
)
from agentworks.execution._managed_observation_protocol import decode_request as decode_observation
from agentworks.execution._managed_observation_protocol import encode_result as encode_observation
from agentworks.execution._managed_stop_protocol import ManagedStopRequest, ManagedStopResult
from agentworks.execution._managed_stop_protocol import decode_request as decode_stop
from agentworks.execution._managed_stop_protocol import encode_result as encode_stop
from agentworks.execution.carrier import CapturedOutput, CarrierReport, Deadline, Dispatch, ExitStatus, Retention

from . import test_managed_operation_keeper as keeper_tests
from .test_managed_lease_exchange import ScriptedCarrier, _success
from .test_managed_operation_keeper import clean_start, make_keeper

bound = keeper_tests.bound

# Real SQLite custody and interrupted host threads are also exercised on Windows.
pytestmark = pytest.mark.windows


class ClosingCarrier(ScriptedCarrier):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_custodies: list[LocalDeliveryCustody] = []
        self.closing_error: BaseException | None = None

    def execute(self, invocation, *, io, deadline, custody):
        data = io.input.data
        request: ManagedStopRequest | ManagedObservationRequest
        if data.startswith(stop_bundle.FIXED_BUNDLE.prefix):
            request = decode_stop(data[len(stop_bundle.FIXED_BUNDLE.prefix) :])
            control = encode_stop(ManagedStopResult((FactName.LAUNCH,)))
        elif data.startswith(observation_bundle.FIXED_BUNDLE.prefix):
            request = decode_observation(data[len(observation_bundle.FIXED_BUNDLE.prefix) :])
            launch = decode_fact(request.expected_launch)
            target = cast("dict[str, object]", launch["target"])
            controller = ControllerObservation(
                ControllerState.ABSENT,
                cast("str", launch["unit"]),
                cast("str", target["boot_id"]),
                launch_sha256(launch),
            )
            control = encode_observation(ManagedResultControl((FactName.LAUNCH,), controller))
        else:
            return super().execute(invocation, io=io, deadline=deadline, custody=custody)
        self.calls += 1
        self.cleanup_custodies.append(custody)
        if self.closing_error is not None:
            custody.begin_process()
            raise self.closing_error
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode()
        payload += b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, body))
            for index, (kind, body) in enumerate(
                (
                    (FileRecordKind.RESULT, control),
                    (FileRecordKind.DATA, request.expected_launch),
                    (FileRecordKind.FINISHED, b""),
                )
            )
        )
        pending = memoryview(payload)
        while pending:
            count = io.output.stdout.try_write(pending)
            assert count is not None and count > 0
            pending = pending[count:]
        channel = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(0), 23, channel, channel)


def test_unknown_publication_allows_bound_stop_after_drain_without_resolving_debt(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    original_debt = owner.register_lifecycle_obligation("managed-start", payload_version=1, payload=b"start")
    original_debt.mark_possible_effect()
    carrier = ClosingCarrier()
    keeper = make_keeper(owner, receipt, carrier)

    def unknown_publication(request):
        if request.lease is not None:
            carrier.dispatch = Dispatch.UNKNOWN
        return _success(request)

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        initial_custody = carrier.custody
        carrier.response = unknown_publication
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        keeper.acknowledge_start(clean_start(receipt))
        assert keeper._worker_done.wait(1)
        assert keeper.publication_uncertain
        owner.stop_admission()
        assert keeper.drain(Deadline.after(1)).drained
        stop = keeper.request_stop(Deadline.after(1))
        assert stop.observation is not None and stop.observation.facts == (
            (FactName.LAUNCH, encode_managed_job_fact(receipt)),
        )
        observation = keeper.observe_cleanup(Deadline.after(1))
        assert observation.observation is not None and observation.observation.controller is not None
        assert observation.observation.controller.state is ControllerState.ABSENT
        assert keeper.last_stop is stop and keeper.last_cleanup is observation
        assert all(custody is initial_custody for custody in carrier.cleanup_custodies)
        assert keeper.publication_uncertain
        assert original_debt.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert all(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in owner.list_lifecycle_obligations())
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("method", ["request_stop", "observe_cleanup"])
def test_interrupted_closing_helper_retains_store_until_explicit_redrain(bound, method) -> None:
    _, owner, receipt = bound
    carrier = ClosingCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    error = KeyboardInterrupt()
    try:
        keeper.admit()
        with pytest.raises(StateError):
            keeper.request_stop(Deadline.after(1))
        owner.stop_admission()
        assert keeper.drain(Deadline.after(1)).drained
        carrier.closing_error = error
        with pytest.raises(KeyboardInterrupt) as caught:
            getattr(keeper, method)(Deadline.after(1))
        assert caught.value is error and keeper.failed
        assert keeper.last_stop is None and keeper.last_cleanup is None
        assert not keeper._custody.settled
        with pytest.raises(StateError):
            keeper.observe_cleanup(Deadline.after(1))
        assert keeper.drain(Deadline.after(1)).drained
        assert keeper.closing_exchange_pending
        calls = carrier.calls
        with pytest.raises(StateError):
            keeper.request_stop(Deadline.after(1))
        assert carrier.calls == calls
    finally:
        assert keeper.drain(Deadline.after(1)).drained


def test_lost_thread_liveness_metadata_does_not_allow_pipe_cleanup(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    entered, release = threading.Event(), threading.Event()
    keeper = make_keeper(owner, receipt, carrier)

    def blocked(request):
        if request.lease is not None:
            entered.set()
            assert release.wait(2)
            keeper._stop.set()
        return _success(request)

    worker = None
    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        carrier.response = blocked
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        keeper.acknowledge_start(clean_start(receipt))
        assert entered.wait(1)
        worker = keeper._worker
        assert worker is not None
        monkeypatch.setattr(worker, "is_alive", lambda: False)
        facts = keeper.drain(Deadline.after(0.01))
        assert facts.worker_active and not facts.drained
        with pytest.raises(StateError):
            keeper.observe_cleanup(Deadline.after(1))
    finally:
        release.set()
        assert keeper._worker_done.wait(1)
        monkeypatch.undo()
        assert keeper.drain(Deadline.after(1)).drained


def test_native_creation_refusal_permits_drain_and_exact_cleanup(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ClosingCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    error = RuntimeError("native thread creation refused")

    def refused(*args, **kwargs):
        raise error

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        # Python 3.13+ uses joinable native threads; earlier hosts use this spawn.
        spawn = "_start_joinable_thread" if hasattr(threading, "_start_joinable_thread") else "_start_new_thread"
        monkeypatch.setattr(threading, spawn, refused)
        with pytest.raises(RuntimeError) as caught:
            keeper.acknowledge_start(clean_start(receipt))
        assert caught.value is error and keeper.failed
        assert carrier.calls == 1 and keeper._worker is not None and not keeper._worker_permission
        facts = keeper.drain(Deadline.after(1))
        assert facts.drained and not facts.worker_active and not facts.startup_pending
        stop = keeper.request_stop(Deadline.after(1))
        assert stop.observation is not None and stop.observation.facts == (
            (FactName.LAUNCH, encode_managed_job_fact(receipt)),
        )
        assert keeper.obligation is not None and keeper.obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
    finally:
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.skipif(os.name == "nt", reason="POSIX process SIGINT delivery interrupts a native join")
def test_real_sigint_join_retains_active_delivery_until_target_done(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    entered, release, joining = threading.Event(), threading.Event(), threading.Event()
    signaler = None

    def response(request):
        if request.lease is not None:
            entered.set()
            assert release.wait(3)
        return _success(request)

    def interrupt_join():
        assert joining.wait(1)
        os.kill(os.getpid(), signal.SIGINT)

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        carrier.response = response
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        keeper.acknowledge_start(clean_start(receipt))
        assert entered.wait(1)
        worker = keeper._worker
        assert worker is not None
        original_join = worker.join

        def join(timeout=None):
            joining.set()
            original_join(timeout)

        monkeypatch.setattr(worker, "join", join)
        signaler = threading.Thread(target=interrupt_join)
        signaler.start()
        with pytest.raises(KeyboardInterrupt):
            keeper.drain(Deadline.after(1))
        assert not keeper._worker_done.is_set()
        facts = keeper.drain(Deadline.after(0.01))
        assert facts.worker_active and not facts.drained
        with pytest.raises(StateError):
            keeper.request_stop(Deadline.after(1))
    finally:
        release.set()
        if signaler is not None:
            signaler.join(1)
            assert not signaler.is_alive()
        if keeper._worker is not None:
            assert keeper._worker_done.wait(1)
        monkeypatch.undo()
        assert keeper.drain(Deadline.after(1)).drained
