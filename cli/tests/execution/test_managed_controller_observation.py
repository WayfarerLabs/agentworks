"""Native query boundaries and separately correlated controller observations."""

from __future__ import annotations

import json
import selectors
import subprocess
import sys
from dataclasses import replace

import pytest

from agentworks.execution import _managed_controller_guest as native
from agentworks.execution import _managed_observation_guest as guest
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, Stream
from agentworks.execution._managed_observation_protocol import (
    FACT_ORDER,
    MAX_CONTROL_BYTES,
    ControllerObservation,
    ControllerState,
    ManagedObservationError,
    ManagedOperation,
    ManagedResultControl,
    checked_controller,
    decode_result,
    encode_result,
)
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    Deadline,
    Failure,
    PreparedInvocation,
)

from .test_managed_observation import (
    GUEST,
    RUN,
    ScriptedCarrier,
    _exchange,
    _launch,
    _records,
    _request,
)
from .test_managed_observation import (
    store as observation_store,
)

store_fixture = observation_store
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux query and managed store")
UNIT = f"agw-managed-{RUN}.service"


def _properties(**changes: str) -> bytes:
    fields = dict(
        zip(native._FIELDS, (UNIT, "loaded", "active", "running", "123", "0", "123", "0", "0", "10", "0"), strict=True)
    )
    fields.update(changes)
    return "".join(f"{key}={value}\n" for key, value in fields.items()).encode("ascii")


def _controller(state: ControllerState = ControllerState.RUNNING) -> ControllerObservation:
    import hashlib

    return ControllerObservation(state, UNIT, vm_guest_boot_id(GUEST), hashlib.sha256(_launch()).hexdigest())


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ControllerState.RUNNING),
        ({"MainPID": "0"}, ControllerState.UNKNOWN),
        ({"ActiveState": "activating", "SubState": "start"}, ControllerState.UNKNOWN),
        ({"ActiveState": "deactivating", "SubState": "stop"}, ControllerState.UNKNOWN),
        ({"ActiveState": "inactive", "SubState": "dead", "MainPID": "0"}, ControllerState.UNKNOWN),
        (
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "ExecMainCode": "1",
                "ExecMainExitTimestampMonotonic": "20",
            },
            ControllerState.EXITED,
        ),
        (
            {
                "ActiveState": "failed",
                "SubState": "failed",
                "MainPID": "0",
                "ExecMainCode": "2",
                "ExecMainStatus": "9",
                "ExecMainExitTimestampMonotonic": "20",
            },
            ControllerState.EXITED,
        ),
        (
            {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "ExecMainPID": "0",
                "ExecMainStartTimestampMonotonic": "0",
            },
            ControllerState.ABSENT,
        ),
        ({"LoadState": "error"}, ControllerState.UNKNOWN),
        ({"Id": "unowned.service"}, ControllerState.UNKNOWN),
        ({"MainPID": "01"}, ControllerState.UNKNOWN),
        ({"MainPID": "-1"}, ControllerState.UNKNOWN),
        ({"ControlPID": "4294967296"}, ControllerState.UNKNOWN),
        ({"ExecMainCode": "999"}, ControllerState.UNKNOWN),
        ({"ExecMainStatus": "999"}, ControllerState.UNKNOWN),
        ({"ExecMainExitTimestampMonotonic": "20"}, ControllerState.UNKNOWN),
    ],
)
def test_native_states_are_positive_and_distinct(changes: dict[str, str], expected: ControllerState) -> None:
    assert native._native_state(_properties(**changes), UNIT) is expected


@pytest.mark.parametrize("fault", ["duplicate", "missing", "extra", "blank", "truncated", "oversized", "nonascii"])
def test_malformed_native_properties_stay_unknown(fault: str) -> None:
    data = _properties()
    data = {
        "duplicate": data + b"Id=" + UNIT.encode() + b"\n",
        "missing": data.split(b"\n", 1)[1],
        "extra": data + b"Unexpected=1\n",
        "blank": data + b"\n",
        "truncated": data[:-1],
        "oversized": data + b"x" * 4097,
        "nonascii": data + b"\xff\n",
    }[fault]
    assert native._native_state(data, UNIT) is ControllerState.UNKNOWN


@pytest.mark.parametrize("state", list(ControllerState))
def test_native_codec_and_host_exposure(state: ControllerState) -> None:
    control = ManagedResultControl((FactName.LAUNCH,), _controller(state))
    assert decode_result(encode_result(control)) == control
    assert len(encode_result(ManagedResultControl(FACT_ORDER, control.controller))) <= MAX_CONTROL_BYTES
    checked_controller(control.controller, _launch())  # type: ignore[arg-type]
    candidate = _exchange(ScriptedCarrier(lambda request: _records(request.nonce, control, (_launch(),))))
    assert candidate.observation is not None
    assert candidate.observation.controller == control.controller
    assert candidate.observation.facts == ((FactName.LAUNCH, _launch()),)


def test_output_response_cannot_smuggle_a_native_query_claim() -> None:
    control = ManagedResultControl((FactName.LAUNCH,), _controller())
    candidate = _exchange(
        ScriptedCarrier(lambda request: _records(request.nonce, control, (_launch(),))),
        stream=Stream.STDOUT,
    )
    assert candidate.observation is not None and candidate.observation.controller is None
    assert candidate.observation.facts == ()


@pytest.mark.parametrize("field", ["state", "unit", "boot_id", "receipt_sha256"])
def test_native_external_values_and_correlation(field: str) -> None:
    encoded = encode_result(ManagedResultControl((FactName.LAUNCH,), _controller()))
    value = json.loads(encoded)
    value["controller"][field] = {
        "state": "finished",
        "unit": f"agw-managed-{'e' * 32}.service",
        "boot_id": "00000000-0000-4000-8000-000000000002",
        "receipt_sha256": "e" * 64,
    }[field]
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ManagedObservationError):
        result = decode_result(data)
        checked_controller(result.controller, _launch())  # type: ignore[arg-type]
    del value["controller"][field]
    with pytest.raises(ManagedObservationError):
        decode_result(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def test_observe_queries_only_after_exact_launch_and_read_output_never_queries(
    store_fixture: ManagedJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_fixture
    calls: list[str] = []

    def query(run_id: str) -> ControllerState:
        calls.append(run_id)
        return ControllerState.ABSENT

    monkeypatch.setattr(guest, "observe_controller", query)
    with pytest.raises(ManagedObservationError):
        guest._prepare(_request(), store)
    assert calls == []
    store.publish_fact(FactName.LAUNCH, _launch())
    observed = guest._prepare(_request(), store)
    assert observed.control.controller == _controller(ControllerState.ABSENT)
    assert calls == [RUN]
    output = guest._prepare(_request(ManagedOperation.READ_OUTPUT, Stream.STDOUT), store)
    assert output.control.controller is None
    assert calls == [RUN]


@pytest.mark.parametrize("fault", ["timeout", "nonzero", "stderr", "oversized", "error", "interrupt", "valid"])
def test_actual_query_client_is_bounded_and_reaped(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    owned: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen
    source = {
        "timeout": "import time; time.sleep(10)",
        "interrupt": "import time; time.sleep(10)",
        "nonzero": "raise SystemExit(1)",
        "stderr": "import os; os.write(2,b'noise'); import time; time.sleep(10)",
        "oversized": "import os; os.write(1,b'x'*8192); import time; time.sleep(10)",
        "valid": "import os; os.write(1," + repr(_properties()) + ")",
    }.get(fault, "")

    def spawn(
        argv: tuple[str, ...],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        close_fds: bool,
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        assert argv == native._query_argv(RUN)
        assert env == native._ENV
        assert stdin == subprocess.DEVNULL
        if fault == "error":
            raise OSError
        child = original(
            (sys.executable, "-I", "-S", "-c", source),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=close_fds,
            env=env,
        )
        owned.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(native, "_QUERY_SECONDS", 0.2)
    if fault == "interrupt":

        def interrupted(*_args: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr(selectors.EpollSelector, "select", interrupted)
    try:
        if fault == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                native.observe_controller(RUN)
        else:
            assert native.observe_controller(RUN) is (
                ControllerState.RUNNING if fault == "valid" else ControllerState.UNKNOWN
            )
        assert all(child.poll() is not None for child in owned)
    finally:
        for child in owned:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


@pytest.mark.parametrize("fault", ["stderr", "nonzero", "incomplete", "unknown"])
def test_host_drops_native_success_on_carrier_fault(fault: str) -> None:
    from agentworks.execution.carrier import Dispatch

    control = ManagedResultControl((FactName.LAUNCH,), _controller())
    carrier = ScriptedCarrier(
        lambda request: _records(request.nonce, control, (_launch(),)),
        stderr=b"noise" if fault == "stderr" else b"",
        code=1 if fault == "nonzero" else 0,
        complete=fault != "incomplete",
        dispatch=Dispatch.UNKNOWN if fault == "unknown" else Dispatch.SENT,
    )
    candidate = _exchange(carrier)
    assert candidate.observation is not None and candidate.observation.controller is None


@pytest.mark.parametrize("failure", [Failure.DEADLINE, Failure.INPUT, Failure.OUTPUT, Failure.OBSERVATION])
def test_host_preserves_raw_carrier_faults_and_original_deadline(failure: Failure) -> None:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._managed_observation_exchange import observe_managed_run
    from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS

    from .test_managed_observation import _plan

    deadline = Deadline.after(0.1)
    control = ManagedResultControl((FactName.LAUNCH,), _controller())

    class FaultedCarrier(ScriptedCarrier):
        def execute(
            self,
            invocation: PreparedInvocation,
            *,
            io: CarrierIO,
            deadline: Deadline,
            custody: LocalDeliveryCustody | None = None,
        ) -> CarrierReport:
            assert deadline is original_deadline
            report = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            return replace(report, local_status=17, failure=failure)

    original_deadline = deadline
    carrier = FaultedCarrier(lambda request: _records(request.nonce, control, (_launch(),)))
    candidate = observe_managed_run(
        carrier,
        expected_launch=_launch(),
        plan=_plan(),
        deadline=deadline,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        guest=GUEST,
    )
    assert candidate.carrier_failure is failure
    assert candidate.carrier_local_status == 17
    assert candidate.carrier_completion is not None and candidate.carrier_completion.code == 0
    assert candidate.observation is not None and candidate.observation.controller is None
    assert carrier.calls == 1


def test_reap_fault_preserves_original_interruption_and_does_not_claim_success(monkeypatch: pytest.MonkeyPatch) -> None:
    original_spawn = subprocess.Popen
    owned: list[subprocess.Popen[bytes]] = []
    original_waits = []
    interruption = KeyboardInterrupt()

    def spawn(
        _argv: tuple[str, ...],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        close_fds: bool,
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        child = original_spawn(
            (sys.executable, "-I", "-S", "-c", "import time; time.sleep(10)"),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=close_fds,
            env=env,
        )
        owned.append(child)
        original_waits.append(child.wait)

        def reap_failure(*_args: object, **_kwargs: object) -> int:
            raise OSError

        monkeypatch.setattr(child, "wait", reap_failure)
        return child

    def interrupt(*_args: object) -> object:
        raise interruption

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(selectors.EpollSelector, "select", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as error:
            native.observe_controller(RUN)
        assert error.value is interruption
    finally:
        for child, wait in zip(owned, original_waits, strict=True):
            if child.poll() is None:
                child.kill()
            wait(timeout=2)
