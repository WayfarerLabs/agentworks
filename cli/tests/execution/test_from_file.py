"""The ``FromFile`` input: a workstation file copied to the payload's stdin, opened before dispatch.

The refusals run over a fake ``_execute`` on every CI host. The delivery
test runs a real request on the L lane, so every byte goes through the pump's
input thread, the launcher and the payload.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.access import Admin, VMAccess
from agentworks.execution.requests import Command, FromFile
from agentworks.execution.results import Completed, Result
from tests.vm_access_support import account_shell, local_vm_access

if TYPE_CHECKING:
    from agentworks.execution.pump import Sink


def access(tmp_path: Path) -> VMAccess:
    return VMAccess(vm_name="box", host="100.64.0.9", user="agentworks", identity=Admin(), identity_file=tmp_path / "k")


def recording_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    dispatched: list[object] = []

    def execute(self: VMAccess, request: object, *, stdin: object, on_stdout: Sink, on_stderr: Sink) -> Result:
        dispatched.append(request)
        return Result(Completed(0))

    monkeypatch.setattr(VMAccess, "_execute", execute)
    return dispatched


def test_a_file_that_cannot_be_opened_raises_with_nothing_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dispatched = recording_dispatch(monkeypatch)

    with pytest.raises(FileNotFoundError):
        access(tmp_path).run(Command(("cat",), stdin=FromFile(tmp_path / "missing")))

    assert dispatched == []


def test_a_path_that_is_not_a_path_is_refused_with_nothing_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatched = recording_dispatch(monkeypatch)
    source = tmp_path / "archive"
    source.write_bytes(b"content")

    with pytest.raises(ValidationError):
        access(tmp_path).run(Command(("cat",), stdin=FromFile(str(source))))  # type: ignore[arg-type]

    assert dispatched == []


def test_stream_refuses_a_request_with_file_input(tmp_path: Path) -> None:
    source = tmp_path / "archive"
    source.write_bytes(b"content")

    with pytest.raises(ValidationError):
        access(tmp_path).stream(
            Command(("cat",), stdin=FromFile(source)), stdin=None, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer
        )


@pytest.mark.skipif(sys.platform != "linux", reason="the L lane runs agw-launch, a Linux guest program, here")
def test_every_byte_of_the_file_reaches_the_payload_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Several pump chunks long, with every byte value, CR LF and NUL included.
    content = bytes(range(256)) * 1024 + b"\r\n\x00tail"
    source = tmp_path / "archive"
    source.write_bytes(content)
    lane = local_vm_access(monkeypatch, home=tmp_path, shell=account_shell(tmp_path))

    result = lane.run(Command(("cat",), stdin=FromFile(source), deadline=60))

    assert result.completion == Completed(0)
    assert result.stdout == content
