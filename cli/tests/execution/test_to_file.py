"""The ``ToFile`` sink: a new binary file, kept only for ``Completed(0)`` (I20).

The pump is replaced by a fake ``_execute`` that hands the sinks fixed
chunks, so this runs on every CI host, Windows included, where binary mode is
what keeps CR LF from being translated.
"""

from __future__ import annotations

import stat
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.access import Admin, VMAccess
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, ToFile
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.execution.pump import Sink
    from agentworks.execution.results import Completion

pytestmark = pytest.mark.windows

CHUNKS = (b"first\r\nline\n", b"\x00\r\x1a binary\n")


def access(tmp_path: Path) -> VMAccess:
    return VMAccess(vm_name="box", host="100.64.0.9", user="agentworks", identity=Admin(), identity_file=tmp_path / "k")


def pump_delivers(monkeypatch: pytest.MonkeyPatch, completion: Completion) -> list[object]:
    """Make ``run`` deliver :data:`CHUNKS` on stdout and end as ``completion``; return the dispatches."""
    dispatched: list[object] = []

    def execute(self: VMAccess, request: object, *, stdin: object, on_stdout: Sink, on_stderr: Sink) -> Result:
        dispatched.append(request)
        for chunk in CHUNKS:
            on_stdout(chunk)
        return Result(completion)

    monkeypatch.setattr(VMAccess, "_execute", execute)
    return dispatched


def test_an_existing_path_is_refused_with_nothing_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dispatched = pump_delivers(monkeypatch, Completed(0))
    target = tmp_path / "archive"
    target.write_bytes(b"already here")

    with pytest.raises(ValidationError):
        access(tmp_path).run(Command(("cat",)), stdout=ToFile(target))

    assert dispatched == []
    assert target.read_bytes() == b"already here"


def test_completed_0_keeps_every_byte_unchanged_and_reports_the_running_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pump_delivers(monkeypatch, Completed(0))
    target = tmp_path / "archive"
    progress: list[int] = []

    result = access(tmp_path).run(Command(("cat",)), stdout=ToFile(target, on_progress=progress.append))

    assert target.read_bytes() == b"".join(CHUNKS)
    assert progress == [len(CHUNKS[0]), len(CHUNKS[0]) + len(CHUNKS[1])]
    assert result.stdout == b""
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "completion", [Completed(1), Unknown(UnknownReason.CONNECTION), Unknown(UnknownReason.TRUNCATED)]
)
def test_any_other_completion_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, completion: Completion
) -> None:
    pump_delivers(monkeypatch, completion)
    target = tmp_path / "archive"

    result = access(tmp_path).run(Command(("cat",)), stdout=ToFile(target))

    assert result.completion == completion
    assert not target.exists()


@pytest.mark.parametrize(
    ("completion", "error"), [(Completed(1), CommandFailed), (Unknown(UnknownReason.CONNECTION), ExecutionIncomplete)]
)
def test_a_checked_failure_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, completion: Completion, error: type[Exception]
) -> None:
    pump_delivers(monkeypatch, completion)
    target = tmp_path / "archive"

    with pytest.raises(error):
        access(tmp_path).run(Command(("cat",)), stdout=ToFile(target), check=True)

    assert not target.exists()


def test_an_interrupt_raised_by_the_progress_callback_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pump_delivers(monkeypatch, Completed(0))
    target = tmp_path / "archive"

    def interrupt(_written: int) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        access(tmp_path).run(Command(("cat",)), stdout=ToFile(target, on_progress=interrupt))

    assert not target.exists()


@pytest.mark.parametrize(
    "sink",
    [ToFile("archive"), ToFile(None), ToFile(object())],  # type: ignore[arg-type]
    ids=["str-path", "none-path", "object-path"],
)
def test_a_tofile_that_is_not_a_path_is_refused_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sink: ToFile
) -> None:
    dispatched = pump_delivers(monkeypatch, Completed(0))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        access(tmp_path).run(Command(("cat",)), stdout=sink)

    assert dispatched == []
    assert list(tmp_path.iterdir()) == []


def test_a_progress_callback_that_is_not_callable_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dispatched = pump_delivers(monkeypatch, Completed(0))
    progress: Callable[[int], None] = "not callable"  # type: ignore[assignment]

    with pytest.raises(ValidationError):
        access(tmp_path).run(Command(("cat",)), stdout=ToFile(tmp_path / "archive", on_progress=progress))

    assert dispatched == []
    assert not (tmp_path / "archive").exists()
