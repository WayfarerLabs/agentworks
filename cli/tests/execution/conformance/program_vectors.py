"""The vectors for the request vocabulary slice 5 adds, F1 to F5 and F8 in the files LLD.

They join :mod:`vectors`' list, so the same lanes run them: finite stdin and
the operation log (F1, F2), core-owned Python programs and their startup (F3,
F4), an interactive script (F5), the run sinks, and the environment of an
elevated request (F8). Each asserts observable behavior only.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from agentworks.execution.errors import CommandFailed
from agentworks.execution.oplog import format_record
from agentworks.execution.requests import Bytes, CaptureOutput, Command, DiscardOutput, NoInput
from agentworks.execution.results import Completed, Unknown, UnknownReason

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result
    from tests.execution.conformance.lanes import Lane

REQUEST_DEADLINE = 60


def python(source: str, *, stdin: bytes | None = None, log_streams: bool = True) -> Command:
    return Command(
        ("python3", "-c", source),
        stdin=NoInput() if stdin is None else Bytes(stdin),
        log_streams=log_streams,
        deadline=REQUEST_DEADLINE,
    )


# -- F1: stdin never reaches a record or an error -----------------------------------------


def stdin_reaches_no_record_and_no_error(lane: Lane) -> None:
    secret = f"agw-f1-{uuid.uuid4().hex}"
    records: list[str] = []

    def log(request: RunRequest, result: Result) -> None:
        records.append(format_record(request, result, label="F1"))

    access = lane.access_with_log(log)
    reads = "import sys; sys.stdin.buffer.read()"
    for log_streams in (True, False):
        request = python(reads, stdin=secret.encode(), log_streams=log_streams)

        result = access.run(request)

        assert result.completion == Completed(0)
        assert secret not in repr(request) + repr(result)
    echoes = (
        "import sys; data = sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data); sys.exit(3)"
    )
    withheld = python(echoes, stdin=secret.encode(), log_streams=False)

    with pytest.raises(CommandFailed) as raised:
        access.run(withheld, check=True)

    error = raised.value
    assert error.result.stdout == error.result.stderr == secret.encode()  # the payload did echo it
    assert all(secret not in text for text in (str(error), repr(error), error.hint or ""))
    assert len(records) == 3
    assert all(secret not in record for record in records)

    with pytest.raises(CommandFailed):
        access.run(python(echoes, stdin=secret.encode()), check=True)
    assert secret in records[-1]  # the streams are logged unless the request withholds them


# -- The run sinks ----------------------------------------------------------------------


def run_sinks_capture_to_their_limit_or_discard(lane: Lane) -> None:
    writes = "import sys; sys.stdout.write('out' * 4); sys.stderr.write('err')"
    request = python(writes)

    discarded = lane.access.run(request, stdout=DiscardOutput(), stderr=DiscardOutput())
    kept = lane.access.run(request, stdout=CaptureOutput(12), stderr=DiscardOutput())
    over = lane.access.run(request, stdout=CaptureOutput(11))

    assert (discarded.completion, discarded.stdout, discarded.stderr) == (Completed(0), b"", b"")
    assert (kept.completion, kept.stdout, kept.stderr) == (Completed(0), b"out" * 4, b"")
    assert over.completion == Unknown(UnknownReason.OUTPUT_LIMIT)


# (name, lanes, check), which ``vectors.build`` turns into its ``Vector`` entries.
ENTRIES: list[tuple[str, str, Callable[..., None]]] = [
    ("F1-stdin-reaches-no-record-or-error", "LSV", stdin_reaches_no_record_and_no_error),
    ("run-sinks", "LS", run_sinks_capture_to_their_limit_or_discard),
]
