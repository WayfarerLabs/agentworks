"""Concrete retained custody for one ordinary call's workstation originals."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import AgentworksError, PartialMutationError

from ._file_result import _details, _diagnostic, _raise_reason, _raise_uncertain
from ._local_file_io import LocalDestination, LocalFileLimitError, LocalFileUnsupportedError
from .files import Change, FileFailureReason, FileOperationPhase

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Never

    from agentworks.operations import OperationBorrow

    from ._file_operation import FileOperation
    from ._file_upload import FileUploadOutcome
    from .carrier import Deadline


class LocalResource(Protocol):
    @property
    def settled(self) -> bool: ...

    def close(self, deadline: Deadline) -> None: ...


@dataclass(slots=True, repr=False)
class LocalFileCall:
    """Keep exact originals until captured remote bookkeeping and reduction settle."""

    borrow: OperationBorrow
    resource: LocalResource | None = None
    reduction_complete: bool = False
    remote_requires_owner_retention: bool = False
    changed: bool = False
    mutation_uncertain: bool = False
    phase: FileOperationPhase = FileOperationPhase.OBSERVATION
    upload_outcome: FileUploadOutcome | None = None


class LocalFileControlFact(Exception):
    """Value-free effect facts captured before reporting interrupted local work."""

    def __init__(self, call: LocalFileCall) -> None:
        self.changed = call.changed
        self.mutation_uncertain = call.mutation_uncertain
        self.cleanup_unsettled = call.resource is not None and not call.resource.settled
        self.phase = call.phase
        self.upload_outcome = call.upload_outcome
        super().__init__("Local file call stopped with retained custody")


def _capture_local_effect(call: LocalFileCall) -> None:
    resource = call.resource
    if isinstance(resource, LocalDestination):
        call.changed = call.changed or resource.changed
        call.mutation_uncertain = resource.mutation_uncertain


def _raise_control(error: BaseException, call: LocalFileCall) -> Never:
    cause = error.__cause__
    try:
        fact = LocalFileControlFact(call)
        fact.__cause__ = cause
    except BaseException:
        raise error from cause
    raise error from fact


def local_failure(
    error: Exception,
    *,
    entity_kind: str,
    entity_name: str,
    changed: bool = False,
    uncertain: bool = False,
    phase: FileOperationPhase = FileOperationPhase.OBSERVATION,
) -> Never:
    """Reduce host exceptions without copying workstation paths or error text."""
    reason = (
        FileFailureReason.DEADLINE
        if isinstance(error, TimeoutError)
        else FileFailureReason.CONFLICT
        if isinstance(error, (FileExistsError, FileNotFoundError))
        else FileFailureReason.LIMIT
        if isinstance(error, LocalFileLimitError)
        else FileFailureReason.UNSUPPORTED
        if isinstance(error, LocalFileUnsupportedError)
        else FileFailureReason.IO
    )
    if uncertain:
        _raise_uncertain(
            phase,
            reason,
            entity_kind=entity_kind,
            entity_name=entity_name,
            dispatch=None,
            effect=Change.CHANGED if changed else None,
        )
    if changed:
        message, hint = _diagnostic(phase, reason, effect=Change.CHANGED)
        raise PartialMutationError(
            message,
            completed_steps=(),
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint=hint,
            details=_details(phase, reason, effect=Change.CHANGED),
        ) from None
    _raise_reason(phase, reason, entity_kind=entity_kind, entity_name=entity_name)


def owned_local_call[T](
    operation: FileOperation,
    deadline: Deadline,
    action: Callable[[LocalFileCall], T],
    *,
    entity_kind: str,
    entity_name: str,
) -> T:
    """Retain before construction, reduce once, then close actual originals."""
    call = operation.begin_local_file()
    try:
        try:
            result = action(call)
        except OSError as error:
            resource = call.resource
            changed = call.changed or (isinstance(resource, LocalDestination) and resource.changed)
            uncertain = isinstance(resource, LocalDestination) and resource.mutation_uncertain
            local_failure(
                error,
                entity_kind=entity_kind,
                entity_name=entity_name,
                changed=changed,
                uncertain=uncertain,
                phase=call.phase,
            )
        call.reduction_complete = True
    except BaseException as error:
        # Retain actual originals before any error reporting or secondary cleanup.
        # A control interrupting reduction cannot certify that reduction completed.
        _capture_local_effect(call)
        if isinstance(error, AgentworksError):
            call.reduction_complete = True
        with suppress(BaseException):
            operation.finish_local_file(call, deadline)
        if not isinstance(error, Exception):
            _raise_control(error, call)
        raise
    call.phase = FileOperationPhase.CLEANUP
    try:
        operation.finish_local_file(call, deadline)
    except Exception as error:
        _capture_local_effect(call)
        resource = call.resource
        local_failure(
            error,
            entity_kind=entity_kind,
            entity_name=entity_name,
            changed=call.changed or (isinstance(resource, LocalDestination) and resource.changed),
            phase=FileOperationPhase.CLEANUP,
        )
    except BaseException as error:
        _capture_local_effect(call)
        _raise_control(error, call)
    return result
