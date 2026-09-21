"""Host-neutral boundary and reducer checks for directory inventory."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import replace

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_inventory import FileInventoryEntry, encode_inventory
from agentworks.execution._file_inventory_exchange import (
    FileInventoryObservationError,
    FileInventoryObservationState,
    _FileInventoryCollector,
    list_directory,
)
from agentworks.execution._file_inventory_protocol import (
    MAX_DEPTH,
    MAX_ENCODED_BYTES,
    MAX_ENTRIES,
    FileInventoryControlError,
    FileInventoryRequest,
    FileInventoryRequestError,
    FileInventoryResultControl,
    decode_file_inventory_request,
    encode_file_inventory_request,
    encode_file_inventory_result,
    parse_file_inventory_entries,
    parse_file_inventory_failure,
    parse_file_inventory_result,
)
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Provenance,
    Retention,
    SinkOutput,
)
from tests.execution.files._runtime_support import runtime_nonce, runtime_ready_record, runtime_selection

pytestmark = pytest.mark.windows

_NONCE = "1" * 32


def _identity() -> IdentityExpectation:
    return IdentityExpectation(1000, 1000, (1000,))


def _request(**changes: object) -> FileInventoryRequest:
    values: dict[str, object] = {
        "nonce": _NONCE,
        "root_path": "/srv/workspace",
        "relative_path": "target",
        "max_entries": 32,
        "max_depth": 3,
        "max_encoded_bytes": 65_536,
        "identity": _identity(),
        "remaining_seconds": 2.5,
    }
    values.update(changes)
    return FileInventoryRequest(**values)  # type: ignore[arg-type]


def _entry(path: str, *, mode: int = stat.S_IFREG | 0o600, links: int = 1) -> FileInventoryEntry:
    observed = FileStat(1, len(path) + 1, mode, links, 1000, 1000, 7, 8, 9)
    return FileInventoryEntry(path, FileRevision(observed))


def _json(value: object) -> bytes:
    return json.dumps(value, allow_nan=True, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _exception_details(error: BaseException) -> str:
    details: list[str] = []
    current: BaseException | None = error
    while current is not None:
        details.extend((repr(current), repr(current.args), repr(current.__dict__)))
        current = current.__cause__ or current.__context__
    return " ".join(details)


def test_request_round_trips_exact_closed_limits_and_unbounded_duration() -> None:
    request = _request(
        max_entries=MAX_ENTRIES,
        max_depth=MAX_DEPTH,
        max_encoded_bytes=MAX_ENCODED_BYTES,
        remaining_seconds=None,
    )
    encoded = encode_file_inventory_request(request)

    assert encoded.isascii()
    assert decode_file_inventory_request(encoded) == request
    assert b'"operation":"list"' in encoded
    assert b'"target"' not in encoded


@pytest.mark.parametrize(
    ("field", "supplied"),
    [
        ("version", True),
        ("operation", "stat"),
        ("max_entries", 0),
        ("max_entries", MAX_ENTRIES + 1),
        ("max_depth", 0),
        ("max_depth", MAX_DEPTH + 1),
        ("max_encoded_bytes", 0),
        ("max_encoded_bytes", MAX_ENCODED_BYTES + 1),
        ("remaining_seconds", 1),
        ("remaining_seconds", True),
        ("remaining_seconds", float("nan")),
        ("remaining_seconds", float("inf")),
        ("path", "Li4="),
        ("root", "cmVsYXRpdmU="),
    ],
)
def test_request_rejects_invalid_fields(field: str, supplied: object) -> None:
    value = json.loads(encode_file_inventory_request(_request()))
    value[field] = supplied
    with pytest.raises(FileInventoryRequestError):
        decode_file_inventory_request(_json(value))


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":1}',
        b'{ "version": 1 }',
        b"[]",
        b"\xff",
        b"{" + b'"x":' + b'"a"' * 40_000 + b"}",
    ],
    ids=["duplicate-key", "noncanonical", "non-object", "invalid-encoding", "oversized"],
)
def test_request_rejects_duplicate_noncanonical_invalid_and_oversized_json(data: bytes) -> None:
    with pytest.raises(FileInventoryRequestError):
        decode_file_inventory_request(data)


def test_request_decoder_does_not_retain_sensitive_path_or_document() -> None:
    canary = "inventory-sensitive-canary"
    value = json.loads(encode_file_inventory_request(_request()))
    value["operation"] = canary

    with pytest.raises(FileInventoryRequestError) as raised:
        decode_file_inventory_request(_json(value))

    assert canary not in _exception_details(raised.value)


def test_request_decoder_does_not_retain_invalid_utf8_path_bytes() -> None:
    canary = b"inventory-path-canary-\xff"
    value = json.loads(encode_file_inventory_request(_request()))
    value["path"] = base64.b64encode(canary).decode("ascii")

    with pytest.raises(FileInventoryRequestError) as raised:
        decode_file_inventory_request(_json(value))

    assert "inventory-path-canary" not in _exception_details(raised.value)


def test_result_and_failure_controls_are_canonical_and_closed() -> None:
    result = FileInventoryResultControl(2, hashlib.sha256(b"[]").digest())
    assert parse_file_inventory_result(encode_file_inventory_result(result), 2) == result

    for body in (b'{"length":2}', b'{"digest":"0","length":2}', b'{"code":"unknown"}', b'{ "code":"io"}'):
        with pytest.raises(FileInventoryControlError):
            if b"code" in body:
                parse_file_inventory_failure(body)
            else:
                parse_file_inventory_result(body, 2)


def test_inventory_entries_round_trip_sorted_utf8_supported_metadata() -> None:
    entries = (
        _entry("alpha"),
        _entry("nested", mode=stat.S_IFDIR | 0o750, links=2),
        _entry("nested/snowman-☃", mode=stat.S_IFSOCK | 0o600),
    )
    encoded = encode_inventory(entries)

    assert (
        parse_file_inventory_entries(
            encoded,
            max_entries=3,
            max_depth=2,
            max_encoded_bytes=len(encoded),
        )
        == entries
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda records: records.reverse(),
        lambda records: records.append(dict(records[0])),
        lambda records: records[0].update(extra=1),
        lambda records: records[0].update(relative_path="nested/too/deep"),
        lambda records: records[0].update(relative_path="../escape"),
        lambda records: records[0].update(relative_path="x" * 256),
        lambda records: records[0].update(mode=stat.S_IFIFO | 0o600),
        lambda records: records[0].update(link_count=2),
        lambda records: records[0].update(uid=-1),
        lambda records: records[0].update(size=True),
    ],
)
def test_inventory_entries_reject_schema_order_depth_kind_and_numeric_violations(mutate) -> None:
    records = json.loads(encode_inventory((_entry("alpha"), _entry("beta"))))
    mutate(records)
    encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    with pytest.raises(FileInventoryControlError):
        parse_file_inventory_entries(encoded, max_entries=2, max_depth=2, max_encoded_bytes=len(encoded))


def test_collector_releases_entries_only_after_complete_verified_transcript() -> None:
    encoded = encode_inventory((_entry("alpha"),))
    collector = _FileInventoryCollector(max_entries=1, max_depth=1, max_encoded_bytes=len(encoded))
    collector.accept(FileRecord(0, FileRecordKind.DATA, encoded))
    collector.accept(
        FileRecord(
            1,
            FileRecordKind.RESULT,
            encode_file_inventory_result(FileInventoryResultControl(len(encoded), hashlib.sha256(encoded).digest())),
        )
    )

    incomplete = collector.finish(None, streams_complete=True, stderr_noise=False)
    assert incomplete.state is FileInventoryObservationState.INCOMPLETE
    assert incomplete.entries is None


def test_collector_clears_typed_entries_on_noise_or_post_terminal_data() -> None:
    encoded = encode_inventory((_entry("alpha"),))
    for post_terminal, stderr_noise in ((False, True), (True, False)):
        collector = _FileInventoryCollector(max_entries=1, max_depth=1, max_encoded_bytes=len(encoded))
        collector.accept(FileRecord(0, FileRecordKind.DATA, encoded))
        collector.accept(
            FileRecord(
                1,
                FileRecordKind.RESULT,
                encode_file_inventory_result(
                    FileInventoryResultControl(len(encoded), hashlib.sha256(encoded).digest())
                ),
            )
        )
        collector.accept(FileRecord(2, FileRecordKind.FINISHED, b"{}"))
        if post_terminal:
            collector.accept(FileRecord(3, FileRecordKind.DATA, b"reflected-canary"))
        result = collector.finish(None, streams_complete=True, stderr_noise=stderr_noise)
        assert result.state is FileInventoryObservationState.INVALID
        assert result.entries is None
        assert result.error in {FileInventoryObservationError.STDERR, FileInventoryObservationError.POST_TERMINAL}


class _ScriptedCarrier:
    def __init__(self, records: tuple[FileRecord, ...], *, stderr: bytes = b"") -> None:
        self.records = records
        self.stderr = stderr
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.output, SinkOutput)
        nonce = runtime_nonce(invocation)
        stream = b"".join(encode_file_record(nonce, record) for record in self.records)
        stream = runtime_ready_record(invocation) + stream
        assert io.output.stdout.try_write(memoryview(stream)) == len(stream)
        assert io.output.stderr.try_write(memoryview(self.stderr)) == len(self.stderr)
        delivered = CapturedOutput(complete=True, provenance=Provenance.CARRIER_STDOUT, retention=Retention.DELIVERED)
        diagnostics = replace(delivered, provenance=Provenance.MIXED_STDERR)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=delivered, stderr=diagnostics)


class _TruncatingCompleteCarrier:
    """Claim complete delivery while cutting a valid transcript's terminator."""

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        assert isinstance(io.output, SinkOutput)
        nonce = runtime_nonce(invocation)
        encoded = encode_inventory((_entry("alpha"),))
        records = (
            FileRecord(0, FileRecordKind.DATA, encoded),
            FileRecord(
                1,
                FileRecordKind.RESULT,
                encode_file_inventory_result(
                    FileInventoryResultControl(len(encoded), hashlib.sha256(encoded).digest())
                ),
            ),
            FileRecord(2, FileRecordKind.FINISHED, b"{}"),
        )
        stream = (
            runtime_ready_record(invocation) + b"".join(encode_file_record(nonce, record) for record in records)[:-1]
        )
        assert io.output.stdout.try_write(memoryview(stream)) == len(stream)
        delivered = CapturedOutput(complete=True, provenance=Provenance.CARRIER_STDOUT, retention=Retention.DELIVERED)
        diagnostics = replace(delivered, provenance=Provenance.MIXED_STDERR)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=delivered, stderr=diagnostics)


def test_exchange_uses_sensitive_finite_input_and_releases_verified_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = encode_inventory((_entry("alpha"),))
    carrier = _ScriptedCarrier(
        (
            FileRecord(0, FileRecordKind.DATA, encoded),
            FileRecord(
                1,
                FileRecordKind.RESULT,
                encode_file_inventory_result(
                    FileInventoryResultControl(len(encoded), hashlib.sha256(encoded).digest())
                ),
            ),
            FileRecord(2, FileRecordKind.FINISHED, b"{}"),
        )
    )
    monkeypatch.setattr("agentworks.execution._file_inventory_exchange.secrets.token_hex", lambda _size: _NONCE)
    plan = IdentityPlan(_identity(), IdentityMode.DIRECT)

    result = list_directory(
        carrier,
        trusted_root_path="/srv/workspace",
        relative_path="target",
        max_entries=1,
        max_depth=1,
        max_encoded_bytes=len(encoded),
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FileInventoryObservationState.PRESENT
    assert result.observation.entries == (_entry("alpha"),)
    assert carrier.io is not None and carrier.io.sensitive
    assert carrier.invocation is not None and "/srv/workspace" not in " ".join(carrier.invocation.argv)
    assert "target" not in carrier.invocation.argv


def test_exchange_refuses_invalid_host_limits_before_dispatch() -> None:
    carrier = _ScriptedCarrier(())
    with pytest.raises(ValidationError):
        list_directory(
            carrier,
            trusted_root_path="/srv/workspace",
            relative_path="target",
            max_entries=0,
            max_depth=1,
            max_encoded_bytes=2,
            plan=IdentityPlan(_identity(), IdentityMode.DIRECT),
            deadline=Deadline.after(10),
            runtime_selection=runtime_selection(),
        )
    assert carrier.calls == 0


def test_exchange_host_path_error_does_not_retain_invalid_text() -> None:
    canary = "host-path-canary-\ud800"
    carrier = _ScriptedCarrier(())
    with pytest.raises(ValidationError) as raised:
        list_directory(
            carrier,
            trusted_root_path="/srv/workspace",
            relative_path=canary,
            max_entries=1,
            max_depth=1,
            max_encoded_bytes=2,
            plan=IdentityPlan(_identity(), IdentityMode.DIRECT),
            deadline=Deadline.after(10),
            runtime_selection=runtime_selection(),
        )

    assert "host-path-canary" not in _exception_details(raised.value)
    assert carrier.calls == 0


def test_exchange_rejects_truncated_transcript_despite_complete_carrier_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentworks.execution._file_inventory_exchange.secrets.token_hex", lambda _size: _NONCE)
    result = list_directory(
        _TruncatingCompleteCarrier(),
        trusted_root_path="/srv/workspace",
        relative_path="target",
        max_entries=1,
        max_depth=1,
        max_encoded_bytes=4096,
        plan=IdentityPlan(_identity(), IdentityMode.DIRECT),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FileInventoryObservationState.INCOMPLETE
    assert result.observation.entries is None


def test_host_modules_import_without_posix_identity_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getresuid", None, raising=False)
    monkeypatch.setattr(os, "getresgid", None, raising=False)
    request = _request()
    assert decode_file_inventory_request(encode_file_inventory_request(request)) == request


def test_record_reader_discards_truncated_path_payload() -> None:
    collector = _FileInventoryCollector(max_entries=1, max_depth=1, max_encoded_bytes=1024)
    reader = FileRecordReader(_NONCE, collector.accept)
    framed = encode_file_record(_NONCE, FileRecord(0, FileRecordKind.DATA, b'[{"relative_path":"canary"}]'))
    reader.try_write(memoryview(framed[:-1]))
    reader.finish()
    observation = collector.finish(reader.error, streams_complete=False, stderr_noise=False)

    assert observation.state is FileInventoryObservationState.INCOMPLETE
    assert observation.entries is None
    assert "canary" not in repr(observation)
