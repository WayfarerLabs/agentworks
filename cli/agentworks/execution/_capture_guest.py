"""Publish bounded process prefixes as immutable workload-owned scratch."""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

from ._capture_control import CaptureCreation, CaptureStreamFact, encode_capture_fact
from ._evidence_wire import FrameKind
from ._inline_control import StreamEnd, StreamName, StreamRetention
from ._inline_guest import _main, _read_exact
from ._inline_request import (
    MAX_PREPARED_MANIFEST_BYTES,
    REQUEST_HEADER_BYTES,
    REQUEST_MARKER,
    InlineManifest,
    ManifestError,
    ManifestErrorCode,
    decode_capture_manifest,
)
from ._scratch import ScratchTransferError, begin_scratch, verify_scratch, write_scratch_chunk
from ._scratch_receipt import ScratchOperation, ScratchReceiptContext
from ._scratch_root import ScratchRootError, open_scratch_root

if TYPE_CHECKING:
    from ._inline_guest_io import FrameWriter
    from ._process import ProcessResult


def read_request() -> InlineManifest:
    header = _read_exact(REQUEST_HEADER_BYTES)
    token = header[len(REQUEST_MARKER) :]
    if not header.startswith(REQUEST_MARKER) or any(c not in b"0123456789abcdef" for c in token):
        raise ManifestError(ManifestErrorCode.INVALID)
    length = int(token, 16)
    if length > MAX_PREPARED_MANIFEST_BYTES:
        raise ManifestError(ManifestErrorCode.OVERSIZED)
    manifest = decode_capture_manifest(_read_exact(length))
    if os.read(0, 1):
        raise ManifestError(ManifestErrorCode.INVALID)
    return manifest


def emit_unentered(emitter: FrameWriter) -> None:
    for stream in StreamName:
        end = StreamEnd(stream, 0, hashlib.sha256(b"").hexdigest(), False, False, StreamRetention.CAPTURED)
        emitter.emit(FrameKind.STREAM_END, encode_capture_fact(CaptureStreamFact(end, CaptureCreation.NOT_ENTERED)))


def publish(emitter: FrameWriter, manifest: InlineManifest, result: ProcessResult) -> bool:
    """Publish known-length prefixes once, marking possible before each creation."""
    assert manifest.capture_tokens is not None
    failed = False
    for index, (stream, output) in enumerate(((StreamName.STDOUT, result.stdout), (StreamName.STDERR, result.stderr))):
        retained = output.data[: manifest.capture_limit]
        digest = hashlib.sha256(retained).digest()
        truncated = len(output.data) > manifest.capture_limit
        end = StreamEnd(
            stream, len(retained), digest.hex(), output.complete and not truncated, truncated, StreamRetention.CAPTURED
        )
        creation = CaptureCreation.NOT_ENTERED
        ready = None
        parent = None
        try:
            parent = open_scratch_root(expires_at=None)
            creation = CaptureCreation.POSSIBLE
            reference = begin_scratch(
                parent,
                len(retained),
                manifest.capture_tokens[index],
                ScratchReceiptContext(ScratchOperation.SNAPSHOT, manifest.identity),
            )
            for offset in range(0, len(retained), 16_384):
                block = retained[offset : offset + 16_384]
                write_scratch_chunk(parent, reference, offset, block, hashlib.sha256(block).digest())
            ready = verify_scratch(parent, reference, digest)
        except (ScratchRootError, ScratchTransferError, OSError):
            failed = True
        finally:
            if parent is not None:
                try:
                    os.close(parent)
                except OSError:
                    failed = True
        emitter.emit(FrameKind.STREAM_END, encode_capture_fact(CaptureStreamFact(end, creation, ready)))
    return failed


def main(nonce: str) -> int:
    return _main(nonce, prepared_input=True, capture_mode=True)
