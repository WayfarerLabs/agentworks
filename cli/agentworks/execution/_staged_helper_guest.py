"""Verify one prepared stdin object before entering its fixed helper bootstrap."""

from __future__ import annotations

import os
import sys
import time

from ._helper_identity import matches_current_identity
from ._scratch import ScratchTransferError, borrow_ready_scratch, verify_scratch
from ._scratch_root import ScratchRootError, open_scratch_root
from ._staged_helper_protocol import MAX_ENVELOPE_BYTES, StagedHelperError, StagedHelperRequest, decode_request


def _check_expiry(expires_at: float) -> None:
    if time.monotonic() >= expires_at:
        raise StagedHelperError("staged helper budget elapsed")


def _read_request() -> StagedHelperRequest:
    data = bytearray()
    while len(data) <= MAX_ENVELOPE_BYTES:
        chunk = os.read(0, MAX_ENVELOPE_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_request(bytes(data))


def _load(request: StagedHelperRequest, bootstrap: str, parent_fd: int, expires_at: float) -> int:
    """Consume an interior request under its caller's already admitted boundary."""
    original_stdin = os.dup(0)
    original_argv = sys.argv
    try:
        try:
            _check_expiry(expires_at)
            ready = verify_scratch(parent_fd, request.reference, request.sha256, expires_at=expires_at)
            with borrow_ready_scratch(parent_fd, ready, expires_at=expires_at) as descriptor:
                os.dup2(descriptor, 0)
            _check_expiry(expires_at)
        except (StagedHelperError, ScratchTransferError, OSError):
            return 125
        sys.argv = ["agentworks-fixed-helper", request.nonce]
        try:
            exec(compile(bootstrap, "<agentworks-fixed-staged-helper>", "exec"), {"__name__": "__main__"})
        except SystemExit as result:
            if type(result.code) is int:
                return result.code
            raise
        raise StagedHelperError("fixed helper did not return an exit status")
    finally:
        try:
            os.dup2(original_stdin, 0)
        finally:
            os.close(original_stdin)
            sys.argv = original_argv


def main(nonce: str, bootstrap: str) -> int:
    """The core-derived entry supplies bootstrap source, never envelope data."""
    try:
        request = _read_request()
        expires_at = time.monotonic() + request.remaining_ns / 1_000_000_000
        if request.nonce != nonce or request.identity.euid != 0 or not matches_current_identity(request.identity):
            raise StagedHelperError("staged helper identity mismatch")
        _check_expiry(expires_at)
        parent_fd = open_scratch_root(expires_at=expires_at)
    except (StagedHelperError, ScratchRootError, ScratchTransferError, OSError):
        return 125
    try:
        return _load(request, bootstrap, parent_fd, expires_at)
    finally:
        os.close(parent_fd)
