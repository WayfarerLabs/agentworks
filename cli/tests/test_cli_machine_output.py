"""Behavior checks for the CLI process-stream machine-output adapter."""

from __future__ import annotations

import io
import sys

import pytest

from agentworks.cli._machine_output import write_json_stdout
from agentworks.machine_output import MachineOutputCommand, encode_json_envelope


class _BinaryStdoutProxy:
    """Capture byte writes while exposing an unrelated text buffer."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.output = io.BytesIO()

    def write(self, data: bytes) -> int:
        return self.output.write(data)


def test_stdout_writer_prefers_a_direct_binary_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _BinaryStdoutProxy()
    monkeypatch.setattr(sys, "stdout", stream)

    write_json_stdout(MachineOutputCommand.DOCTOR, {"count": 3})

    assert stream.output.getvalue() == encode_json_envelope(MachineOutputCommand.DOCTOR, {"count": 3})
    assert stream.buffer.getvalue() == ""


def test_stdout_writer_rejects_text_stdout_without_a_binary_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)

    with pytest.raises(RuntimeError):
        write_json_stdout(MachineOutputCommand.DOCTOR, {"count": 3})

    assert stream.getvalue() == ""
