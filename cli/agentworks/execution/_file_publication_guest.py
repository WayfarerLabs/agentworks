"""Destination entry point for one private publication operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_paths import ConfinedOpenError, open_linux_confined, open_linux_root
from ._file_publication import (
    FilePublicationError,
    PublicationCleanupDebt,
    PublicationFailureKind,
    PublicationPhase,
    ScratchFileSource,
    publish_file,
    retry_publication_cleanup,
)
from ._file_publication_protocol import (
    MAX_REQUEST_BYTES,
    FilePublicationCleanupRequest,
    FilePublicationCleanupResult,
    FilePublicationFailureCode,
    FilePublicationFailureControl,
    FilePublicationReconcileRequest,
    FilePublicationReconcileResult,
    FilePublicationRequest,
    FilePublicationRequestError,
    FilePublishRequest,
    FilePublishResult,
    PublicationCleanupState,
    decode_file_publication_request,
    empty_file_publication_body,
    encode_file_publication_cleanup_result,
    encode_file_publication_failure,
    encode_file_publication_reconcile_result,
    encode_file_publish_result,
)
from ._file_publication_wire import (
    BoundPublicationCleanupDebt,
    FilePublicationWireError,
    bind_publication_cleanup_debt,
)
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._publication_receipt import (
    PublicationReceiptError,
    PublicationReceiptFailureKind,
    PublicationStageCleanupDebt,
    PublicationStageHistoricalOwnership,
    PublicationStageOwnershipUncertainty,
    _Identity,
    cleanup_publication_stage,
    reconcile_publication_stage,
)
from ._scratch import ScratchTransferError, verify_scratch

_OperationResult = FilePublishResult | FilePublicationReconcileResult | FilePublicationCleanupResult


class _SafeFailure(Exception):
    def __init__(self, failure: FilePublicationFailureControl) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


def _read_request() -> FilePublicationRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_publication_request(bytes(data))


def _expires_at(remaining_seconds: float | None) -> float | None:
    if remaining_seconds is None:
        return None
    return min(sys.float_info.max, time.monotonic() + remaining_seconds)


def _expired(expires_at: float | None) -> bool:
    return expires_at is not None and time.monotonic() >= expires_at


def _open_parent(request: FilePublicationRequest) -> tuple[int, int | None, str]:
    try:
        root_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FilePublicationFailureControl(FilePublicationFailureCode.ROOT_REFUSED)) from None
    if root_fd is None:
        raise _SafeFailure(FilePublicationFailureControl(FilePublicationFailureCode.ROOT_REFUSED))
    parent_fd: int | None = None
    try:
        components = request.relative_path.split("/")
        if len(components) > 1:
            flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                parent_fd = open_linux_confined(root_fd, "/".join(components[:-1]), flags)
            except ConfinedOpenError:
                raise _SafeFailure(FilePublicationFailureControl(FilePublicationFailureCode.PARENT_REFUSED)) from None
            if parent_fd is None:
                raise _SafeFailure(FilePublicationFailureControl(FilePublicationFailureCode.PARENT_REFUSED))
        return root_fd, parent_fd, components[-1]
    except BaseException:
        with suppress(OSError):
            os.close(root_fd)
        raise


def _parent_identity(parent_fd: int) -> _Identity:
    try:
        observed = os.fstat(parent_fd)
    except OSError:
        raise _SafeFailure(FilePublicationFailureControl(FilePublicationFailureCode.PARENT_REFUSED)) from None
    return _Identity(observed.st_dev, observed.st_ino)


def _bind_cleanup(
    request: FilePublicationRequest,
    parent_fd: int,
    debt: PublicationCleanupDebt | PublicationStageCleanupDebt,
) -> tuple[PublicationCleanupState, BoundPublicationCleanupDebt | None]:
    try:
        return (
            PublicationCleanupState.EXACT,
            bind_publication_cleanup_debt(request.reference, _parent_identity(parent_fd), debt),
        )
    except FilePublicationWireError:
        return PublicationCleanupState.OWNERSHIP_UNCERTAIN, None


def _publication_failure(
    request: FilePublicationRequest,
    parent_fd: int,
    error: FilePublicationError,
) -> FilePublicationFailureControl:
    state = PublicationCleanupState.NONE
    debt = None
    if error.cleanup_debt is not None:
        state, debt = _bind_cleanup(request, parent_fd, error.cleanup_debt)
    return FilePublicationFailureControl(
        FilePublicationFailureCode.PUBLICATION,
        publication_kind=error.kind,
        publication_phase=error.phase,
        cleanup_state=state,
        cleanup_debt=debt,
    )


def _receipt_failure(
    request: FilePublicationRequest,
    parent_fd: int,
    error: PublicationReceiptError,
) -> FilePublicationFailureControl:
    state = PublicationCleanupState.NONE
    debt = None
    if error.cleanup_debt is not None:
        state, debt = _bind_cleanup(request, parent_fd, error.cleanup_debt)
    return FilePublicationFailureControl(
        FilePublicationFailureCode.RECEIPT,
        receipt_kind=error.kind,
        cleanup_state=state,
        cleanup_debt=debt,
    )


def _operate_publish(request: FilePublishRequest, parent_fd: int, leaf: str, expires_at: float | None) -> FilePublishResult:
    try:
        ready = verify_scratch(parent_fd, request.reference, request.digest, expires_at=expires_at)
    except ScratchTransferError as error:
        raise _SafeFailure(
            FilePublicationFailureControl(
                FilePublicationFailureCode.SCRATCH,
                scratch_kind=error.kind,
                scratch_phase=error.phase,
            )
        ) from None
    try:
        revision = publish_file(
            parent_fd,
            leaf,
            ScratchFileSource(parent_fd, ready),
            condition=request.condition,
            create_metadata=request.create_metadata,
            expires_at=expires_at,
        )
    except FilePublicationError as error:
        raise _SafeFailure(_publication_failure(request, parent_fd, error)) from None
    return FilePublishResult(revision, False)


def _operate_reconcile(
    request: FilePublicationReconcileRequest,
    parent_fd: int,
    expires_at: float | None,
) -> FilePublicationReconcileResult:
    try:
        result = reconcile_publication_stage(
            parent_fd,
            request.reference,
            parent_fd,
            expires_at=expires_at,
        )
    except PublicationReceiptError as error:
        if error.kind is PublicationReceiptFailureKind.DEADLINE and error.cleanup_debt is not None:
            state, debt = _bind_cleanup(request, parent_fd, error.cleanup_debt)
            if state is PublicationCleanupState.EXACT:
                assert debt is not None
                return FilePublicationReconcileResult(debt, True)
        raise _SafeFailure(_receipt_failure(request, parent_fd, error)) from None
    if isinstance(result, PublicationStageOwnershipUncertainty):
        return FilePublicationReconcileResult(None, False)
    assert isinstance(result, PublicationStageHistoricalOwnership)
    state, debt = _bind_cleanup(request, parent_fd, PublicationStageCleanupDebt(result._ownership, False))
    if state is not PublicationCleanupState.EXACT or debt is None:
        return FilePublicationReconcileResult(None, False)
    return FilePublicationReconcileResult(debt, False)


def _operate_cleanup(
    request: FilePublicationCleanupRequest,
    parent_fd: int,
    expires_at: float | None,
) -> FilePublicationCleanupResult:
    if _expired(expires_at):
        raise _SafeFailure(
            FilePublicationFailureControl(
                FilePublicationFailureCode.PUBLICATION,
                publication_kind=PublicationFailureKind.DEADLINE,
                publication_phase=PublicationPhase.CLEANUP,
                cleanup_state=PublicationCleanupState.EXACT,
                cleanup_debt=request.cleanup_debt,
            )
        )
    if _parent_identity(parent_fd) != request.cleanup_debt._publication_parent:
        raise _SafeFailure(
            FilePublicationFailureControl(
                FilePublicationFailureCode.PUBLICATION,
                publication_kind=PublicationFailureKind.CONFLICT,
                publication_phase=PublicationPhase.CLEANUP,
                cleanup_state=PublicationCleanupState.EXACT,
                cleanup_debt=request.cleanup_debt,
            )
        )
    debt = request.cleanup_debt._debt
    if isinstance(debt, PublicationCleanupDebt):
        if not retry_publication_cleanup(parent_fd, debt):
            raise _SafeFailure(
                FilePublicationFailureControl(
                    FilePublicationFailureCode.PUBLICATION,
                    publication_kind=PublicationFailureKind.CONFLICT,
                    publication_phase=PublicationPhase.CLEANUP,
                    cleanup_state=PublicationCleanupState.EXACT,
                    cleanup_debt=request.cleanup_debt,
                )
            )
    else:
        try:
            cleanup_publication_stage(parent_fd, parent_fd, debt)
        except PublicationReceiptError as error:
            raise _SafeFailure(_receipt_failure(request, parent_fd, error)) from None
    return FilePublicationCleanupResult(False)


def _operate(request: FilePublicationRequest, expires_at: float | None) -> _OperationResult:
    root_fd, parent_fd, leaf = _open_parent(request)
    selected_fd = root_fd if parent_fd is None else parent_fd
    try:
        if isinstance(request, FilePublishRequest):
            return _operate_publish(request, selected_fd, leaf, expires_at)
        if isinstance(request, FilePublicationReconcileRequest):
            return _operate_reconcile(request, selected_fd, expires_at)
        return _operate_cleanup(request, selected_fd, expires_at)
    finally:
        try:
            if parent_fd is not None:
                with suppress(OSError):
                    os.close(parent_fd)
        finally:
            with suppress(OSError):
                os.close(root_fd)


def _finish_failure(writer: FileRecordWriter, failure: FilePublicationFailureControl) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_publication_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_publication_body())
    return 0


def _finish_result(writer: FileRecordWriter, result: _OperationResult) -> int:
    if isinstance(result, FilePublishResult):
        body = encode_file_publish_result(result)
    elif isinstance(result, FilePublicationReconcileResult):
        body = encode_file_publication_reconcile_result(result)
    else:
        body = encode_file_publication_cleanup_result(result)
    writer.write(FileRecordKind.RESULT, body)
    writer.write(FileRecordKind.FINISHED, empty_file_publication_body())
    return 0


def main(nonce: str) -> int:
    """Execute one request after nonce, runtime, identity, and expiry checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FilePublicationRequestError as error:
        return _finish_failure(writer, FilePublicationFailureControl(error.failure))
    if request.nonce != nonce:
        return _finish_failure(writer, FilePublicationFailureControl(FilePublicationFailureCode.NONCE_MISMATCH))
    if sys.platform != "linux":
        return _finish_failure(writer, FilePublicationFailureControl(FilePublicationFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FilePublicationFailureControl(FilePublicationFailureCode.IDENTITY_MISMATCH))
    expires_at = _expires_at(request.remaining_seconds)
    if _expired(expires_at):
        return _finish_failure(writer, FilePublicationFailureControl(FilePublicationFailureCode.DEADLINE))
    try:
        result = _operate(request, expires_at)
    except _SafeFailure as error:
        return _finish_failure(writer, error.failure)
    deadline_exceeded = _expired(expires_at)
    if isinstance(result, FilePublishResult):
        result = FilePublishResult(result.revision, deadline_exceeded)
    elif isinstance(result, FilePublicationReconcileResult):
        result = FilePublicationReconcileResult(result.cleanup_debt, result.deadline_exceeded or deadline_exceeded)
    else:
        result = FilePublicationCleanupResult(deadline_exceeded)
    return _finish_result(writer, result)
