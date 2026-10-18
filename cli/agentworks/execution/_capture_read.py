"""Retrieve immutable capture ranges without confusing remote facts with local bytes."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ._capture_control import RetrievedCapture
from ._file_snapshot_exchange import FileSnapshotObservationState, snapshot_chunk
from ._inline_control import StreamName

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._capture_cleanup import CaptureCleanup
    from ._capture_control import CaptureStreamFact
    from ._file_snapshot_exchange import FileSnapshotCandidateResult
    from ._fixed_helper_operation import BoundHelperCarrier
    from ._runtime_prerequisite import HelperClosureExpectation
    from .carrier import Deadline


def _read(cleanup: CaptureCleanup, fact: CaptureStreamFact, deadline: Deadline) -> RetrievedCapture:
    if fact.ready is None:
        return RetrievedCapture(b"")
    data = bytearray()
    while len(data) < fact.end.count and not deadline.expired:
        offset, length = len(data), min(262_144, fact.end.count - len(data))

        def action(
            carrier: BoundHelperCarrier,
            prepare: Callable[[HelperClosureExpectation], BoundHelperCarrier],
            offset: int = offset,
            length: int = length,
        ) -> FileSnapshotCandidateResult:
            assert fact.ready is not None
            return snapshot_chunk(
                carrier,
                token=fact.ready._reference._ownership._token,
                ready=fact.ready,
                offset=offset,
                length=length,
                prepare_delivery=prepare,
                **cleanup.arguments(deadline),
            )

        result = cleanup.call(action, deadline)
        observation = result.observation
        if (
            cleanup.helper_pending
            or not cleanup.accepted(result, FileSnapshotObservationState.CHUNK)
            or observation is None
        ):
            break
        chunk = observation.chunk
        if (
            chunk is None
            or chunk.offset != offset
            or chunk.length != length
            or len(chunk.data) != length
            or hashlib.sha256(chunk.data).digest() != chunk.chunk_digest
        ):
            break
        data.extend(chunk.data)
    verified = len(data) == fact.end.count and hashlib.sha256(data).digest() == fact.ready._digest
    return RetrievedCapture(bytes(data), verified)


def retrieve_capture(
    cleanup: CaptureCleanup, facts: tuple[CaptureStreamFact, ...], deadline: Deadline
) -> tuple[RetrievedCapture, RetrievedCapture]:
    streams = {fact.end.stream: fact for fact in facts}
    results = []
    for stream in StreamName:
        fact = streams.get(stream)
        results.append(
            RetrievedCapture(b"") if fact is None or cleanup.helper_pending else _read(cleanup, fact, deadline)
        )
    return results[0], results[1]
