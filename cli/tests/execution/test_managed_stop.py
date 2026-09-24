"""Private managed stop intent, bounded helper and carrier evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_store as job_store
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_stop_guest as guest
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, RequestAsset, StoreError
from agentworks.execution._managed_stop_bundle import FIXED_BUNDLE
from agentworks.execution._managed_stop_exchange import ManagedStopState, _Collector, stop_managed_run
from agentworks.execution._managed_stop_protocol import (
    MAX_OBSERVATION_MS,
    ManagedStopError,
    ManagedStopRequest,
    ManagedStopResult,
    decode_request,
    decode_result,
    encode_request,
    encode_result,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed stop")
RUN = "a" * 32
NONCE = "b" * 32


def _launch(*, target: str = "vm-one", boot: str = "00000000-0000-4000-8000-000000000001") -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {"kind": "vm", "name": target, "incarnation": "v1:" + "c" * 64, "boot_id": boot},
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


def _boundary(launch: bytes) -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "boundary-empty",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "receipt_sha256": hashlib.sha256(launch).hexdigest(),
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[ManagedJobStore, None, None]:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ManagedJobStore(RUN, _namespace="managed", _owner_uid=os.getuid(), _anchor_fd=fd) as item:
            yield item
    finally:
        os.close(fd)


def _request(launch: bytes | None = None, budget: int = 1) -> ManagedStopRequest:
    return ManagedStopRequest(NONCE, launch or _launch(), IdentityExpectation(0, 0, (0,)), budget)


def _records(nonce: str, result: ManagedStopResult, facts: tuple[bytes, ...]) -> bytes:
    entries = [(FileRecordKind.RESULT, encode_result(result))]
    entries.extend((FileRecordKind.DATA, fact) for fact in facts)
    entries.append((FileRecordKind.FINISHED, b""))
    return b"".join(
        encode_file_record(nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(entries)
    )


class Carrier:
    def __init__(
        self,
        response: Callable[[ManagedStopRequest], bytes],
        *,
        dispatch: Dispatch = Dispatch.SENT,
        code: int | None = 0,
        complete: bool = True,
        stderr: bytes = b"",
    ) -> None:
        self.response = response
        self.dispatch = dispatch
        self.code = code
        self.complete = complete
        self.stderr = stderr
        self.calls = 0
        self.validations = 0
        self.budget = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1
        assert isinstance(io.input, FiniteInput) and io.input.data.startswith(FIXED_BUNDLE.prefix)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        self.budget = request.observation_ms
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + self.response(request)
        remaining = memoryview(payload)
        while remaining:
            consumed = io.output.stdout.try_write(remaining)
            assert consumed is not None and consumed > 0
            remaining = remaining[consumed:]
        if self.stderr:
            io.output.stderr.try_write(memoryview(self.stderr))
        channel = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        completion = ExitStatus(code=self.code) if self.dispatch is Dispatch.SENT and self.code is not None else None
        return CarrierReport(self.dispatch, completion, stdout=channel, stderr=channel)


def _exchange(carrier: Carrier, *, deadline: Deadline | None = None):  # type: ignore[no-untyped-def]
    return stop_managed_run(
        carrier,
        expected_launch=_launch(),
        plan=IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        deadline=deadline or Deadline.after(10),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
    )


def test_store_stop_is_empty_separate_idempotent_and_launch_bound(store: ManagedJobStore, tmp_path: Path) -> None:
    launch = _launch()
    with pytest.raises(StoreError):
        store.publish_stop_request(launch)
    store.publish_fact(FactName.LAUNCH, launch)
    assert not store.read_stop_request()
    store.publish_stop_request(launch)
    store.publish_stop_request(launch)
    leaf = tmp_path / "managed" / RUN / "request-stop"
    assert leaf.read_bytes() == b""
    assert len(tuple(RequestAsset)) == 5
    assert store.read_request() is None
    store.publish_request_asset(RequestAsset.SOURCE, b"source")
    with pytest.raises(StoreError):
        store.read_request()
    with pytest.raises(StoreError):
        store.publish_stop_request(_launch(target="vm-other"))
    with pytest.raises(StoreError):
        store.publish_stop_request(_launch(boot="00000000-0000-4000-8000-000000000002"))


@pytest.mark.parametrize("variant", ["nonempty", "mode", "symlink", "hardlink", "fifo"])
def test_store_refuses_unsafe_stop_leaf(store: ManagedJobStore, tmp_path: Path, variant: str) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    leaf = tmp_path / "managed" / RUN / "request-stop"
    if variant == "symlink":
        leaf.symlink_to("launch")
    elif variant == "fifo":
        os.mkfifo(leaf)
    else:
        leaf.write_bytes(b"x" if variant == "nonempty" else b"")
        if variant == "mode":
            leaf.chmod(0o600)
        if variant == "hardlink":
            os.link(leaf, leaf.with_name("outside"))
        if variant != "mode":
            leaf.chmod(0o400)
    with pytest.raises(StoreError):
        store.read_stop_request()
    with pytest.raises(StoreError):
        store.publish_stop_request(launch)


def test_store_rejects_wrong_stop_leaf_owner(store: ManagedJobStore, tmp_path: Path) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    store.publish_stop_request(launch)
    directory = os.open(tmp_path / "managed" / RUN, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(StoreError):
            job_store._open_request_leaf(directory, "request-stop", os.getuid() + 1)
    finally:
        os.close(directory)


def test_guest_publishes_before_reply_and_only_empty_establishes_termination(store: ManagedJobStore) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    accepted, facts = guest._prepare(_request(), store)
    assert store.read_stop_request()
    assert accepted.facts == (FactName.LAUNCH,) and facts == (launch,)
    store.publish_fact(FactName.BOUNDARY_EMPTY, _boundary(launch))
    terminated, facts = guest._prepare(_request(), store)
    assert terminated.facts == (FactName.LAUNCH, FactName.BOUNDARY_EMPTY)
    assert facts == (launch, _boundary(launch))


@pytest.mark.parametrize("variant", ["stale", "malformed"])
def test_guest_keeps_accepted_after_invalid_boundary(store: ManagedJobStore, tmp_path: Path, variant: str) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    if variant == "stale":
        store.publish_fact(FactName.BOUNDARY_EMPTY, _boundary(_launch(target="vm-other")))
    else:
        leaf = tmp_path / "managed" / RUN / "boundary-empty"
        leaf.write_bytes(b"malformed")
        leaf.chmod(0o400)
    result, facts = guest._prepare(_request(), store)
    assert store.read_stop_request()
    assert result.facts == (FactName.LAUNCH,)
    assert facts == (launch,)


def test_publication_failure_may_leave_durable_stop(store: ManagedJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    publish = store.publish_stop_request

    def fail_after_publication(expected_launch: bytes) -> None:
        publish(expected_launch)
        raise OSError("injected post-link failure")

    monkeypatch.setattr(store, "publish_stop_request", fail_after_publication)
    with pytest.raises(OSError):
        guest._prepare(_request(), store)
    assert store.read_stop_request()


def test_protocol_bounds_and_exact_source_python311(tmp_path: Path) -> None:
    request = _request(budget=MAX_OBSERVATION_MS)
    encoded = encode_request(request)
    assert decode_request(encoded) == request
    for mutation in ({"observation_ms": 0}, {"observation_ms": MAX_OBSERVATION_MS + 1}, {"unit": "other"}):
        value = {**json.loads(encoded), **mutation}
        with pytest.raises(ManagedStopError):
            decode_request(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ManagedStopError):
        decode_result(b'{"facts":["launch","wait"],"version":1}')
    with pytest.raises(ManagedStopError):
        encode_request(ManagedStopRequest(NONCE, _launch(), IdentityExpectation(1001, 1001, (1001,)), 1))
    for interpreter in (sys.executable, "/usr/bin/python3.11"):
        if not Path(interpreter).exists():
            continue
        result = subprocess.run(
            [interpreter, "-I", "-S", "-B", "-c", FIXED_BUNDLE.bootstrap, NONCE],
            input=FIXED_BUNDLE.prefix + b"bad",
            capture_output=True,
            cwd=tmp_path,
            env={"PYTHONPATH": str(tmp_path)},
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert b"FAILED" in result.stdout
        source = build_helper_modules(
            "_agw_stop_parity",
            (
                "_helper_identity",
                "_managed_job_wire",
                "_managed_job_request",
                "_managed_job_store",
                "_file_wire",
                "_managed_observation_protocol",
                "_managed_stop_protocol",
            ),
        )
        source += (
            "p=sys.modules['_agw_stop_parity._managed_stop_protocol']\n"
            "data=bytes.fromhex(sys.argv[1])\n"
            "assert p.encode_request(p.decode_request(data))==data\n"
            "assert p.decode_result(p.encode_result(p.ManagedStopResult(("
            "sys.modules['_agw_stop_parity._managed_job_store'].FactName.LAUNCH,)))).facts[0].value=='launch'\n"
        )
        parity = subprocess.run(
            [interpreter, "-I", "-S", "-B", "-c", source, encoded.hex()],
            capture_output=True,
            cwd=tmp_path,
            env={"PYTHONPATH": str(tmp_path)},
            timeout=10,
            check=False,
        )
        assert parity.returncode == 0, parity.stderr


def test_exchange_accepted_terminated_budget_and_validator() -> None:
    launch = _launch()
    for facts, state in (
        ((launch,), ManagedStopState.ACCEPTED),
        ((launch, _boundary(launch)), ManagedStopState.TERMINATED),
    ):
        names = (FactName.LAUNCH,) if len(facts) == 1 else (FactName.LAUNCH, FactName.BOUNDARY_EMPTY)
        carrier = Carrier(
            lambda request, names=names, facts=facts: _records(request.nonce, ManagedStopResult(names), facts)
        )
        candidate = _exchange(carrier, deadline=Deadline.after(None))
        assert candidate.observation is not None and candidate.observation.state is state
        assert carrier.budget == MAX_OBSERVATION_MS
        assert carrier.validations >= 1 and carrier.calls == 1
        assert launch not in repr(candidate).encode()
    pending = Carrier(lambda request: _records(request.nonce, ManagedStopResult((FactName.LAUNCH,)), (launch,)))
    _exchange(pending, deadline=Deadline.after(1))
    assert 1 <= pending.budget <= 1000


def test_huge_finite_deadline_still_dispatches_with_capped_budget() -> None:
    launch = _launch()
    carrier = Carrier(lambda request: _records(request.nonce, ManagedStopResult((FactName.LAUNCH,)), (launch,)))
    candidate = _exchange(carrier, deadline=Deadline.after(1e308))
    assert candidate.observation is not None and candidate.observation.state is ManagedStopState.ACCEPTED
    assert carrier.calls == 1 and carrier.budget == MAX_OBSERVATION_MS


def test_exchange_preserves_unknown_on_complete_helper_failure() -> None:
    failed = Carrier(
        lambda request: b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, b""))
            for index, kind in enumerate((FileRecordKind.FAILED, FileRecordKind.FINISHED))
        )
    )
    candidate = _exchange(failed)
    assert candidate.observation is not None and candidate.observation.state is ManagedStopState.UNKNOWN
    assert candidate.observation.facts == ()
    malformed = Carrier(
        lambda request: (
            encode_file_record(request.nonce, FileRecord(0, FileRecordKind.RESULT, b"bad"))
            + encode_file_record(request.nonce, FileRecord(1, FileRecordKind.FINISHED, b""))
        )
    )
    candidate = _exchange(malformed)
    assert candidate.observation is not None and candidate.observation.state is ManagedStopState.INVALID


@pytest.mark.parametrize(
    "fault", ["missing", "post", "stderr", "incomplete", "nonzero", "lost", "unknown", "not_sent", "stale"]
)
def test_exchange_does_not_promote_uncertain_or_stale_evidence(fault: str) -> None:
    launch = _launch()

    def response(request: ManagedStopRequest) -> bytes:
        data = _records(
            request.nonce,
            ManagedStopResult((FactName.LAUNCH, FactName.BOUNDARY_EMPTY)),
            (launch, _boundary(_launch(target="other")) if fault == "stale" else _boundary(launch)),
        )
        if fault == "missing":
            return data.rsplit(b"AGWF1", 1)[0]
        if fault == "post":
            return data + encode_file_record(request.nonce, FileRecord(4, FileRecordKind.DATA, b"x"))
        return data

    carrier = Carrier(
        response,
        dispatch={"unknown": Dispatch.UNKNOWN, "not_sent": Dispatch.NOT_SENT}.get(fault, Dispatch.SENT),
        code=1 if fault == "nonzero" else None if fault == "lost" else 0,
        complete=fault != "incomplete",
        stderr=b"noise" if fault == "stderr" else b"",
    )
    candidate = _exchange(carrier)
    assert candidate.observation is not None
    assert candidate.observation.state not in (ManagedStopState.ACCEPTED, ManagedStopState.TERMINATED)
    assert candidate.observation.facts == ()


def test_expired_deadline_is_not_sent_after_validation() -> None:
    carrier = Carrier(lambda request: b"")
    candidate = _exchange(carrier, deadline=Deadline.after(0))
    assert candidate.dispatch is Dispatch.NOT_SENT and candidate.observation is None
    assert carrier.validations == 1 and carrier.calls == 0
    with pytest.raises(ValidationError):
        stop_managed_run(
            carrier,
            expected_launch=b"secret",
            plan=IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
            deadline=Deadline.after(1),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        )
    assert carrier.calls == 0


def test_root_linux_and_carrier_validation_precede_dispatch() -> None:
    carrier = Carrier(lambda request: b"")
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
            stop_managed_run(
                carrier, expected_launch=_launch(), plan=plan, deadline=Deadline.after(1), runtime_selection=runtime
            )
    assert carrier.validations == 0 and carrier.calls == 0

    class RefusingCarrier(Carrier):
        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            super().validate(invocation, io=io)
            raise ValidationError("incompatible carrier")

    refusing = RefusingCarrier(lambda request: b"")
    with pytest.raises(ValidationError):
        _exchange(refusing)
    assert refusing.validations == 1 and refusing.calls == 0


def test_collector_rejects_extra_data() -> None:
    collector = _Collector(_launch())
    collector.accept(FileRecord(0, FileRecordKind.RESULT, encode_result(ManagedStopResult((FactName.LAUNCH,)))))
    collector.accept(FileRecord(1, FileRecordKind.DATA, _launch()))
    collector.accept(FileRecord(2, FileRecordKind.DATA, b"extra"))
    assert collector.issue is not None
