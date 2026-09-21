"""Shared fixtures for the private owned-download composition."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind, OperationScope
from agentworks.execution._file_download import FileDownloadOutcome, download_file
from agentworks.execution.carrier import CarrierIO, CarrierReport, ChannelFeatures, Deadline, SinkOutput
from agentworks.operations import OperationOwner
from tests.execution.files._file_snapshot_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import ByteSink, Carrier


class BytesSink:
    def __init__(self, *, writes: tuple[int | None, ...] = ()) -> None:
        self.data = bytearray()
        self.writes = writes
        self.calls = 0
        self.closed = False

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        selected = self.writes[self.calls - 1] if self.calls <= len(self.writes) else len(data)
        if selected is None:
            return None
        amount = min(selected, len(data))
        self.data.extend(data[:amount])
        return amount

    def close(self) -> None:
        self.closed = True


class _DiscardSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class LostCallStdoutCarrier:
    """Run one call while discarding its otherwise-valid stdout."""

    def __init__(self, lost_call: int) -> None:
        self._carrier = LocalCarrier()
        self._lost_call = lost_call
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls != self._lost_call:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
        assert isinstance(io.output, SinkOutput)
        hidden = CarrierIO(
            input=io.input,
            output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
            sensitive=io.sensitive,
        )
        return self._carrier.execute(invocation, io=hidden, deadline=deadline)


def owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "download-vm"),
        "file-download",
    )


def download(
    operation_owner: OperationOwner,
    root: Path,
    sink: ByteSink,
    max_bytes: int,
    plan: IdentityPlan,
    *,
    carrier: Carrier | None = None,
    deadline: Deadline | None = None,
    selected_runtime: RuntimeSelection | None = None,
    relative_path: str = "source",
) -> FileDownloadOutcome:
    return download_file(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path=relative_path,
        sink=sink,
        max_bytes=max_bytes,
        plan=plan,
        deadline=deadline or Deadline.after(30),
        runtime_selection=selected_runtime or runtime_selection(sys.executable),
        owner=operation_owner,
    )
