"""Separate fixed prepared admission without widening small inline requests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.execution import _inline_guest as guest
from agentworks.execution._direct_input_preparation import prepare_direct_input
from agentworks.execution._inline import prepare_inline_candidate
from agentworks.execution._inline_bundle import PREPARED_PROGRAM
from agentworks.execution._inline_request import (
    MAX_MANIFEST_BYTES,
    MAX_PREPARED_MANIFEST_BYTES,
    REQUEST_HEADER_BYTES,
    REQUEST_MARKER,
    InlineManifest,
    ManifestError,
    ManifestErrorCode,
    decode_manifest,
    decode_prepared_manifest,
    encode_prepared_request_envelope,
)
from agentworks.execution.carrier import Deadline, FiniteInput
from agentworks.execution.models import Command, Script, Shell

from .test_direct_input_operation import PLAN
from .test_execution_operation import RecordingCarrier
from .test_prepared_helper_stage import BOOTSTRAP, RUNTIME


def manifest() -> InlineManifest:
    prepared = prepare_inline_candidate(Command(("/bin/true",)), plan=PLAN, runtime_selection=RUNTIME)
    assert isinstance(prepared.io.input, FiniteInput)
    return decode_manifest(prepared.io.input.data[REQUEST_HEADER_BYTES:])


def test_prepared_entry_admits_sixteen_mib_source_and_input_separately():
    source = "#" + "x" * (16 * 1024 * 1024 - 2) + "\n"
    stdin = bytes(range(256)) * (16 * 1024 * 1024 // 256)
    preparation = prepare_direct_input(
        RecordingCarrier(),
        Script(source, Shell.SH),
        plan=PLAN,
        bootstrap=BOOTSTRAP,
        deadline=Deadline.after(30),
        runtime_selection=RUNTIME,
        stdin=stdin,
        env=None,
        cwd=None,
        capture_limit=4096,
        sensitive=False,
    )
    try:
        data = preparation.data[len(PREPARED_PROGRAM.prefix) + REQUEST_HEADER_BYTES :]
        assert MAX_MANIFEST_BYTES < len(data) <= MAX_PREPARED_MANIFEST_BYTES
        request = decode_prepared_manifest(data)
        assert request.source == source.encode() and request.stdin == stdin and request.capture_limit == 4096
        with pytest.raises(ManifestError) as caught:
            decode_manifest(data)
        assert caught.value.code is ManifestErrorCode.OVERSIZED
    finally:
        preparation.clear()
        preparation.candidate.release()


def test_host_prepared_encoding_refuses_actual_sixty_four_mib_bound():
    request = replace(manifest(), stdin=b"x" * (MAX_PREPARED_MANIFEST_BYTES * 3 // 4))
    with pytest.raises(ManifestError) as caught:
        encode_prepared_request_envelope(request)
    assert caught.value.code is ManifestErrorCode.OVERSIZED


@pytest.mark.parametrize("prepared,bound", [(False, MAX_MANIFEST_BYTES), (True, MAX_PREPARED_MANIFEST_BYTES)])
def test_guest_envelope_refuses_oversize_before_reading_body(monkeypatch, prepared, bound):
    calls = []

    def header(length):
        calls.append(length)
        assert length == REQUEST_HEADER_BYTES
        return REQUEST_MARKER + format(bound + 1, "08x").encode("ascii")

    monkeypatch.setattr(guest, "_read_exact", header)
    with pytest.raises(ManifestError) as caught:
        guest._read_request(prepared_input=prepared)
    assert caught.value.code is ManifestErrorCode.OVERSIZED and calls == [REQUEST_HEADER_BYTES]


def test_guest_prepared_decoder_applies_large_schema_and_finite_input_admission(monkeypatch):
    original = manifest()
    request = replace(original, stdin=b"x" * 40_000)
    envelope = encode_prepared_request_envelope(request)
    chunks = iter((envelope[:REQUEST_HEADER_BYTES], envelope[REQUEST_HEADER_BYTES:]))
    monkeypatch.setattr(guest, "_read_exact", lambda length: next(chunks))
    monkeypatch.setattr(guest, "os", SimpleNamespace(read=lambda fd, length: b""))
    assert guest._read_request(prepared_input=True) == request
    live = encode_prepared_request_envelope(replace(original, stdin=None))
    chunks = iter((live[:REQUEST_HEADER_BYTES], live[REQUEST_HEADER_BYTES:]))
    with pytest.raises(ManifestError):
        guest._read_request(prepared_input=True)
