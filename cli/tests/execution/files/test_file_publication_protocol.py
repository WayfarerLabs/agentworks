"""Protocol and host-reducer checks for private publication exchanges."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    PublicationCleanupDebt,
)
from agentworks.execution._file_publication_bundle import FIXED_BUNDLE
from agentworks.execution._file_publication_exchange import (
    FilePublicationObservationError,
    FilePublicationObservationState,
    publication_cleanup,
    publication_reconcile,
    publish,
)
from agentworks.execution._file_publication_protocol import (
    FilePublicationCleanupRequest,
    FilePublicationCleanupResult,
    FilePublicationFailureCode,
    FilePublicationFailureControl,
    FilePublicationReconcileRequest,
    FilePublicationReconcileResult,
    FilePublicationRequest,
    FilePublicationRequestError,
    FilePublishRequest,
    FilePublishResult,
    PublicationCleanupState,
    decode_file_publication_request,
    empty_file_publication_body,
    encode_file_publication_cleanup_result,
    encode_file_publication_failure,
    encode_file_publication_reconcile_result,
    encode_file_publication_request,
    encode_file_publish_result,
    publication_context,
)
from agentworks.execution._file_publication_wire import (
    BoundPublicationCleanupDebt,
    FilePublicationWireError,
    bind_publication_cleanup_debt,
    encode_publication_cleanup_debt,
)
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_helper_argv
from agentworks.execution._publication_receipt import (
    PublicationReceiptFailureKind,
    PublicationStageCleanupDebt,
    PublicationStageOwnership,
    publication_stage_name,
)
from agentworks.execution._publication_receipt import (
    _Identity as PublicationIdentity,
)
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import ScratchOwnership
from agentworks.execution._scratch_receipt import _Identity as ScratchIdentity
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
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv

_TOKEN = bytes(range(16))
_DIGEST = hashlib.sha256(b"content").digest()


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _reference(plan: IdentityPlan, *, length: int = 7) -> ScratchReference:
    return ScratchReference(
        ScratchOwnership(
            _TOKEN,
            publication_context(plan.expected),
            ScratchIdentity(11, 12),
            ScratchIdentity(11, 13),
            ScratchIdentity(11, 14),
            1002,
            length,
            ScratchIdentity(11, 15),
        )
    )


def _receipt_debt(
    reference: ScratchReference,
    *,
    stage_removed: bool = False,
) -> BoundPublicationCleanupDebt:
    ownership = PublicationStageOwnership(
        reference._ownership,
        PublicationIdentity(11, 12),
        publication_stage_name(_TOKEN),
        PublicationIdentity(11, 21),
        PublicationIdentity(11, 22),
    )
    return bind_publication_cleanup_debt(
        reference,
        PublicationIdentity(11, 12),
        PublicationStageCleanupDebt(ownership, stage_removed),
    )


def _revision(*, size: int = 7, digest: bytes = _DIGEST) -> FileRevision:
    return FileRevision(
        FileStat(11, 30, stat.S_IFREG | 0o600, 1, 1001, 1002, size, 40, 41),
        digest,
    )


def _requests(plan: IdentityPlan) -> tuple[FilePublicationRequest, ...]:
    reference = _reference(plan)
    common = ("0" * 32, _TOKEN, "/trusted", "nested/target", reference)
    return (
        FilePublishRequest(*common, _DIGEST, Create(), CreateMetadata(1001, 1002, 0o640), plan.expected, 1.5),
        FilePublicationReconcileRequest(*common, plan.expected, 1.5),
        FilePublicationCleanupRequest(*common, _receipt_debt(reference), plan.expected, 1.5),
    )


@pytest.mark.parametrize("index", range(3), ids=["publish", "reconcile", "cleanup"])
def test_request_roundtrip_is_canonical_and_binds_reference_token_context_and_parent(
    plan: IdentityPlan,
    index: int,
) -> None:
    request = _requests(plan)[index]
    encoded = encode_file_publication_request(request)

    assert decode_file_publication_request(encoded) == request
    assert json.dumps(json.loads(encoded), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode() == encoded
    assert b"/trusted" not in encoded and b"nested/target" not in encoded


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "extra": 1},
        lambda value: {**value, "version": True},
        lambda value: {**value, "path": ""},
        lambda value: {**value, "token": "FF" * 16},
        lambda value: {**value, "operation": "unknown"},
    ],
)
def test_request_decoder_rejects_extra_malformed_and_rebound_fields(plan: IdentityPlan, mutate: object) -> None:
    value = json.loads(encode_file_publication_request(_requests(plan)[0]))
    changed = mutate(value)  # type: ignore[operator]
    encoded = json.dumps(changed, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(FilePublicationRequestError):
        decode_file_publication_request(encoded)


def test_wire_refuses_unidentified_generic_debt_and_foreign_parent(plan: IdentityPlan) -> None:
    reference = _reference(plan)
    with pytest.raises(FilePublicationWireError):
        bind_publication_cleanup_debt(
            reference,
            PublicationIdentity(11, 12),
            PublicationCleanupDebt(publication_stage_name(_TOKEN), None, None),
        )
    with pytest.raises(FilePublicationWireError):
        bind_publication_cleanup_debt(
            reference,
            PublicationIdentity(11, 99),
            PublicationCleanupDebt(publication_stage_name(_TOKEN), 11, 21),
        )


def test_request_encoder_refuses_reference_rebound_to_another_token(plan: IdentityPlan) -> None:
    request = _requests(plan)[0]
    with pytest.raises(FilePublicationRequestError):
        encode_file_publication_request(replace(request, token=bytes(reversed(_TOKEN))))


def _write(sink: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)  # type: ignore[attr-defined]
        assert written is not None and written > 0
        remaining = remaining[written:]


@dataclass
class TranscriptCarrier:
    build: object
    dispatch: Dispatch = Dispatch.SENT
    stderr: bytes = b""
    complete: bool = True
    failure: Failure | None = None
    calls: int = 0
    invocation: PreparedInvocation | None = None
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        assert io.input.sensitive and io.sensitive
        assert io.input.data.startswith(FIXED_BUNDLE.prefix)
        request = decode_file_publication_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        transcript = self.build(request)  # type: ignore[operator]
        _write(io.output.stdout, transcript)
        _write(io.output.stderr, self.stderr)
        output = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            ExitStatus(code=23) if self.dispatch is Dispatch.SENT else None,
            23,
            output,
            output,
            self.failure,
        )


def _records(request: FilePublicationRequest, kind: FileRecordKind, body: bytes) -> bytes:
    return encode_file_record(request.nonce, FileRecord(0, kind, body)) + encode_file_record(
        request.nonce,
        FileRecord(1, FileRecordKind.FINISHED, empty_file_publication_body()),
    )


def _call_publish(carrier: object, plan: IdentityPlan, reference: ScratchReference):
    return publish(
        carrier,  # type: ignore[arg-type]
        trusted_root_path="/trusted/root-canary",
        relative_path="nested/target-canary",
        token=_TOKEN,
        reference=reference,
        digest=_DIGEST,
        condition=Create(),
        create_metadata=CreateMetadata(1001, 1002, 0o640),
        plan=plan,
        deadline=Deadline.after(1),
    )


def test_complete_results_preserve_effect_and_deadline_facts(plan: IdentityPlan) -> None:
    reference = _reference(plan)

    def publish_records(request: FilePublicationRequest) -> bytes:
        assert isinstance(request, FilePublishRequest)
        return _records(
            request, FileRecordKind.RESULT, encode_file_publish_result(FilePublishResult(_revision(), True))
        )

    published = _call_publish(TranscriptCarrier(publish_records), plan, reference)
    assert published.observation.state is FilePublicationObservationState.PUBLISHED
    assert published.observation.revision == _revision()
    assert published.observation.deadline_exceeded is True

    debt = _receipt_debt(reference)

    def reconcile_records(request: FilePublicationRequest) -> bytes:
        return _records(
            request,
            FileRecordKind.RESULT,
            encode_file_publication_reconcile_result(FilePublicationReconcileResult(debt, True)),
        )

    recovered = publication_reconcile(
        TranscriptCarrier(reconcile_records),
        trusted_root_path="/trusted/root-canary",
        relative_path="nested/target-canary",
        token=_TOKEN,
        reference=reference,
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert recovered.observation.state is FilePublicationObservationState.RECOVERED
    assert recovered.observation.cleanup_debt == debt
    assert recovered.observation.deadline_exceeded is True

    def cleanup_records(request: FilePublicationRequest) -> bytes:
        return _records(
            request,
            FileRecordKind.RESULT,
            encode_file_publication_cleanup_result(FilePublicationCleanupResult(True)),
        )

    cleaned = publication_cleanup(
        TranscriptCarrier(cleanup_records),
        trusted_root_path="/trusted/root-canary",
        relative_path="nested/target-canary",
        token=_TOKEN,
        reference=reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert cleaned.observation.state is FilePublicationObservationState.CLEANED
    assert cleaned.observation.deadline_exceeded is True


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("stage_removed", True), ("record_state", "creating_or_final")],
)
def test_reconcile_rejects_impossible_recovered_cleanup_shape(
    plan: IdentityPlan,
    field: str,
    replacement: object,
) -> None:
    reference = _reference(plan)
    value = json.loads(
        encode_file_publication_reconcile_result(FilePublicationReconcileResult(_receipt_debt(reference), False))
    )
    value["cleanup"][field] = replacement
    body = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")

    def impossible_recovery(request: FilePublicationRequest) -> bytes:
        return _records(request, FileRecordKind.RESULT, body)

    result = publication_reconcile(
        TranscriptCarrier(impossible_recovery),
        trusted_root_path="/trusted",
        relative_path="target",
        token=_TOKEN,
        reference=reference,
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert result.observation.state is FilePublicationObservationState.UNCERTAIN
    assert result.observation.error is FilePublicationObservationError.CONTROL
    assert result.observation.cleanup_debt is None


def test_cleanup_failure_accepts_only_monotonic_exact_debt_progress(plan: IdentityPlan) -> None:
    reference = _reference(plan)
    original = _receipt_debt(reference)
    progressed = _receipt_debt(reference, stage_removed=True)

    def failure_records(request: FilePublicationRequest) -> bytes:
        failure = FilePublicationFailureControl(
            FilePublicationFailureCode.RECEIPT,
            receipt_kind=PublicationReceiptFailureKind.IO,
            cleanup_state=PublicationCleanupState.EXACT,
            cleanup_debt=progressed,
        )
        return _records(request, FileRecordKind.FAILED, encode_file_publication_failure(failure))

    result = publication_cleanup(
        TranscriptCarrier(failure_records),
        trusted_root_path="/trusted",
        relative_path="target",
        token=_TOKEN,
        reference=reference,
        cleanup_debt=original,
        plan=plan,
        deadline=Deadline.after(1),
    )

    assert result.observation.state is FilePublicationObservationState.REFUSED
    assert result.observation.cleanup_debt == progressed


@pytest.mark.parametrize("case", ["substituted-stage", "regressed-stage-removal"])
def test_cleanup_failure_rejects_substituted_or_regressed_debt(plan: IdentityPlan, case: str) -> None:
    reference = _reference(plan)
    original = _receipt_debt(reference)
    progressed = _receipt_debt(reference, stage_removed=True)
    assert isinstance(original._debt, PublicationStageCleanupDebt)
    substituted_ownership = replace(
        original._debt._ownership,
        _stage=PublicationIdentity(11, 999),
    )
    substituted = bind_publication_cleanup_debt(
        reference,
        PublicationIdentity(11, 12),
        PublicationStageCleanupDebt(substituted_ownership, True),
    )
    if case == "substituted-stage":
        input_debt, returned_debt = original, substituted
    else:
        input_debt, returned_debt = progressed, original

    def invalid_failure_records(request: FilePublicationRequest) -> bytes:
        failure = FilePublicationFailureControl(
            FilePublicationFailureCode.RECEIPT,
            receipt_kind=PublicationReceiptFailureKind.IO,
            cleanup_state=PublicationCleanupState.EXACT,
            cleanup_debt=returned_debt,
        )
        return _records(request, FileRecordKind.FAILED, encode_file_publication_failure(failure))

    rejected = publication_cleanup(
        TranscriptCarrier(invalid_failure_records),
        trusted_root_path="/trusted",
        relative_path="target",
        token=_TOKEN,
        reference=reference,
        cleanup_debt=input_debt,
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert rejected.observation.state is FilePublicationObservationState.UNCERTAIN
    assert rejected.observation.error is FilePublicationObservationError.CONTROL
    assert rejected.observation.cleanup_debt is None and rejected.observation.failure is None


def test_substituted_revision_and_cleanup_parent_are_uncertain_control(plan: IdentityPlan) -> None:
    reference = _reference(plan)

    def wrong_revision(request: FilePublicationRequest) -> bytes:
        bad = FilePublishResult(_revision(size=8), False)
        return _records(request, FileRecordKind.RESULT, encode_file_publish_result(bad))

    publication = _call_publish(TranscriptCarrier(wrong_revision), plan, reference)
    assert publication.observation.state is FilePublicationObservationState.UNCERTAIN
    assert publication.observation.error is FilePublicationObservationError.CONTROL

    debt = _receipt_debt(reference)

    def wrong_parent(request: FilePublicationRequest) -> bytes:
        value = {
            "cleanup": encode_publication_cleanup_debt(debt),
            "deadline_exceeded": False,
            "result": "recovered",
        }
        value["cleanup"]["parent"]["inode"] = 999  # type: ignore[index]
        body = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        return _records(request, FileRecordKind.RESULT, body)

    reconciled = publication_reconcile(
        TranscriptCarrier(wrong_parent),
        trusted_root_path="/trusted",
        relative_path="target",
        token=_TOKEN,
        reference=reference,
        plan=plan,
        deadline=Deadline.after(1),
    )
    assert reconciled.observation.state is FilePublicationObservationState.UNCERTAIN
    assert reconciled.observation.error is FilePublicationObservationError.CONTROL


@pytest.mark.parametrize("case", ["partial", "stderr", "wrong-nonce", "extra-after-terminal", "order"])
def test_partial_noisy_nonce_and_order_faults_never_expose_typed_facts(plan: IdentityPlan, case: str) -> None:
    reference = _reference(plan)

    def transcript(request: FilePublicationRequest) -> bytes:
        result = encode_file_publish_result(FilePublishResult(_revision(), False))
        complete = _records(request, FileRecordKind.RESULT, result)
        if case == "partial":
            return complete[:-3]
        if case == "wrong-nonce":
            return complete.replace(request.nonce.encode(), b"f" * 32, 1)
        if case == "extra-after-terminal":
            return complete + encode_file_record(request.nonce, FileRecord(2, FileRecordKind.FINISHED, b"{}"))
        if case == "order":
            return encode_file_record(request.nonce, FileRecord(0, FileRecordKind.FINISHED, b"{}"))
        return complete

    carrier = TranscriptCarrier(transcript, stderr=b"noise" if case == "stderr" else b"")
    result = _call_publish(carrier, plan, reference)

    assert result.observation.state is FilePublicationObservationState.UNCERTAIN
    assert result.observation.revision is None and result.observation.cleanup_debt is None


def test_oversized_manifest_refuses_before_carrier_and_safe_values_do_not_leak(plan: IdentityPlan) -> None:
    reference = _reference(plan)
    carrier = TranscriptCarrier(lambda request: b"")
    canary = "path-secret-canary"

    with pytest.raises(ValidationError) as raised:
        publish(
            carrier,
            trusted_root_path="/" + "a" * 5000,
            relative_path=canary,
            token=_TOKEN,
            reference=reference,
            digest=_DIGEST,
            condition=Create(),
            create_metadata=CreateMetadata(1001, 1002, 0o600),
            plan=plan,
            deadline=Deadline.after(1),
        )

    assert carrier.calls == 0
    assert canary not in repr(reference) and canary not in repr(raised.value)


@pytest.mark.parametrize("operation", ["publish", "reconcile", "cleanup"])
def test_public_entrypoints_do_not_retain_unencodable_paths_in_exception_chains(
    plan: IdentityPlan,
    operation: str,
) -> None:
    reference = _reference(plan)
    debt = _receipt_debt(reference)
    carrier = TranscriptCarrier(lambda request: b"")
    path = "surrogate-\ud800-secret"

    with pytest.raises(ValidationError) as raised:
        if operation == "publish":
            publish(
                carrier,
                trusted_root_path=path,
                relative_path="target",
                token=_TOKEN,
                reference=reference,
                digest=_DIGEST,
                condition=Create(),
                create_metadata=CreateMetadata(1001, 1002, 0o600),
                plan=plan,
                deadline=Deadline.after(1),
            )
        elif operation == "reconcile":
            publication_reconcile(
                carrier,
                trusted_root_path=path,
                relative_path="target",
                token=_TOKEN,
                reference=reference,
                plan=plan,
                deadline=Deadline.after(1),
            )
        else:
            publication_cleanup(
                carrier,
                trusted_root_path=path,
                relative_path="target",
                token=_TOKEN,
                reference=reference,
                cleanup_debt=debt,
                plan=plan,
                deadline=Deadline.after(1),
            )

    assert carrier.calls == 0
    assert raised.value.__context__ is None and raised.value.__cause__ is None


@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DEMOTE),
    ],
    ids=["direct", "root", "demote"],
)
@pytest.mark.parametrize("operation", ["publish", "reconcile", "cleanup"])
def test_complete_requests_fit_real_windows_ssh_and_qga_bounds(plan: IdentityPlan, operation: str) -> None:
    reference = _reference(plan)
    common = ("0" * 32, _TOKEN, "/" + "r" * 4094, "p" * 4096, reference)
    request: FilePublicationRequest
    if operation == "publish":
        request = FilePublishRequest(
            *common,
            _DIGEST,
            Create(),
            CreateMetadata(1001, 1002, 0o600),
            plan.expected,
            1.0,
        )
    elif operation == "reconcile":
        request = FilePublicationReconcileRequest(*common, plan.expected, 1.0)
    else:
        request = FilePublicationCleanupRequest(*common, _receipt_debt(reference), plan.expected, 1.0)
    manifest = encode_file_publication_request(request)
    invocation = PreparedInvocation(
        build_helper_argv(
            plan,
            runtime_path="/usr/bin/python3",
            fixed_source=FIXED_BUNDLE.bootstrap,
            nonce=request.nonce,
        )
    )
    connection = SSHConnection("host.example", "agent", Path("/keys/identity"), Path("/keys/known-hosts"))
    windows_command = subprocess.list2cmdline(build_ssh_argv(connection, invocation))
    qga_body = json.dumps(
        {"command": invocation.argv, "input-data": (FIXED_BUNDLE.prefix + manifest).decode("ascii")}
    ).encode("ascii")

    assert len(manifest) <= 32_768
    assert len(windows_command) < 32_767
    assert len(qga_body) < 65_536
    assert FIXED_BUNDLE.prefix.decode("ascii") not in windows_command


def test_aggregate_oversize_refuses_before_proxmox_wire(plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _requests(plan)[0]
    invocation = PreparedInvocation(
        build_helper_argv(
            plan,
            runtime_path="/usr/bin/python3",
            fixed_source=FIXED_BUNDLE.bootstrap,
            nonce=request.nonce,
        )
    )
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example:8006", "node-a", 101, "root@pam!token", "secret"))

    def unexpected_wire(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized provider body reached the wire")

    monkeypatch.setattr(carrier._wire, "request", unexpected_wire)
    oversized = FIXED_BUNDLE.prefix + b"x" * (65_536 - len(FIXED_BUNDLE.prefix))
    io = CarrierIO(
        input=FiniteInput(oversized, sensitive=True),
        output=SinkOutput(_NullSink(), _NullSink(), require_live=False),
        sensitive=True,
    )
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline.after(1))


class _NullSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)
