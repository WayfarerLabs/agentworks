"""Owned composition for private single-file helper exchanges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import ValidationError
from agentworks.execution._account import (
    AccountObservationState,
    FileOwnershipResolutionResult,
    resolve_file_ownership,
)
from agentworks.execution._account_protocol import (
    FileOwnershipRequest,
    FileOwnershipRequestError,
    encode_file_ownership_request,
)
from agentworks.execution._file_inventory_exchange import (
    FileInventoryCandidateResult,
)
from agentworks.execution._file_inventory_exchange import (
    list_directory as exchange_list_directory,
)
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    ensure_file_directory,
    set_file_metadata,
)
from agentworks.execution._file_metadata_protocol import (
    FileMetadataFailureCode,
    FileMetadataOperation,
    FileMetadataRequest,
    FileMetadataRequestError,
    encode_file_metadata_request,
)
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
)
from agentworks.execution._file_object_exchange import (
    remove_file as exchange_remove_file,
)
from agentworks.execution._file_object_exchange import (
    stat_file as exchange_stat_file,
)
from agentworks.execution._file_read import (
    FileReadCandidateResult,
)
from agentworks.execution._file_read import (
    read_file as exchange_read_file,
)
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline, Dispatch, ExitStatus
    from agentworks.operations import OperationOwner

_VALIDATION_NONCE = "0" * 32


class _CandidateResult(Protocol):
    @property
    def dispatch(self) -> Dispatch: ...

    @property
    def carrier_completion(self) -> ExitStatus | None: ...


class _MetadataExchange(Protocol):
    def __call__(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        uid: int,
        gid: int,
        mode: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileMetadataCandidateResult: ...


@dataclass(frozen=True, slots=True, repr=False)
class OwnedFileOutcome[T]:
    """Returned exchange facts and independent operation-ownership state."""

    result: T | None = None
    ownership_result: FileOwnershipResolutionResult | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class OwnedFileControlFact[T](Exception):
    """Safe bounded owned-file state attached to escaping control flow."""

    def __init__(self, outcome: OwnedFileOutcome[T]) -> None:
        self.outcome = outcome
        super().__init__("private file operation stopped with retained operation state")


@dataclass(slots=True, repr=False)
class _State[T: _CandidateResult]:
    operation: BorrowedFixedHelperCarrier
    deadline: Deadline
    result: T | None = None
    ownership_result: FileOwnershipResolutionResult | None = None
    deadline_exceeded: bool = False

    def note_deadline(self) -> None:
        self.deadline_exceeded = self.deadline_exceeded or self.deadline.expired

    def finish(self) -> OwnedFileOutcome[T]:
        return OwnedFileOutcome(
            self.result,
            self.ownership_result,
            self.deadline_exceeded,
            self.operation.pending_remote_effects,
            self.operation.coordination_uncertain,
            self.operation.requires_owner_retention,
        )


def read_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileReadCandidateResult]:
    """Run one bounded read while borrowing core operation ownership."""
    return _run_owned(
        carrier,
        deadline,
        owner,
        lambda operation: exchange_read_file(
            operation,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        ),
    )


def stat_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileObjectCandidateResult]:
    """Run one object observation while borrowing core operation ownership."""
    return _run_owned(
        carrier,
        deadline,
        owner,
        lambda operation: exchange_stat_file(
            operation,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        ),
    )


def list_directory(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_entries: int,
    max_depth: int,
    max_encoded_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileInventoryCandidateResult]:
    """Run one bounded inventory while borrowing core operation ownership."""
    return _run_owned(
        carrier,
        deadline,
        owner,
        lambda operation: exchange_list_directory(
            operation,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            max_entries=max_entries,
            max_depth=max_depth,
            max_encoded_bytes=max_encoded_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        ),
    )


def remove_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    expected_kind: FileKind,
    expected_revision: FileRevision,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileObjectCandidateResult]:
    """Run one conditional removal while borrowing core operation ownership."""
    return _run_owned(
        carrier,
        deadline,
        owner,
        lambda operation: exchange_remove_file(
            operation,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            expected_kind=expected_kind,
            expected_revision=expected_revision,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        ),
    )


def set_metadata(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    trusted_owner: str,
    trusted_group: str,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileMetadataCandidateResult]:
    """Resolve names and converge one object's metadata under one borrow."""
    _validate_metadata_inputs(
        FileMetadataOperation.SET_METADATA,
        trusted_root_path,
        relative_path,
        trusted_owner,
        trusted_group,
        mode,
        plan,
        deadline,
    )
    return _run_metadata(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        trusted_owner=trusted_owner,
        trusted_group=trusted_group,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        owner=owner,
        exchange=set_file_metadata,
    )


def ensure_directory(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    trusted_owner: str,
    trusted_group: str,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
) -> OwnedFileOutcome[FileMetadataCandidateResult]:
    """Resolve names and create or converge one directory under one borrow."""
    _validate_metadata_inputs(
        FileMetadataOperation.ENSURE_DIRECTORY,
        trusted_root_path,
        relative_path,
        trusted_owner,
        trusted_group,
        mode,
        plan,
        deadline,
    )
    return _run_metadata(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        trusted_owner=trusted_owner,
        trusted_group=trusted_group,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        owner=owner,
        exchange=ensure_file_directory,
    )


def _run_owned[T: _CandidateResult](
    carrier: Carrier,
    deadline: Deadline,
    owner: OperationOwner,
    exchange: Callable[[BorrowedFixedHelperCarrier], T],
) -> OwnedFileOutcome[T]:
    if deadline.expired:
        return OwnedFileOutcome(deadline_exceeded=True)
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    state = _State[T](operation, deadline)
    try:
        try:
            state.result = exchange(operation)
            operation.settle(state.result.dispatch, state.result.carrier_completion)
            state.note_deadline()
            return state.finish()
        except BaseException as control:
            state.note_deadline()
            raise control from OwnedFileControlFact(state.finish())
    finally:
        borrow.close()


def _run_metadata(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    trusted_owner: str,
    trusted_group: str,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
    exchange: _MetadataExchange,
) -> OwnedFileOutcome[FileMetadataCandidateResult]:
    if deadline.expired:
        return OwnedFileOutcome(deadline_exceeded=True)
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    state = _State[FileMetadataCandidateResult](operation, deadline)
    try:
        try:
            ownership_result = resolve_file_ownership(
                operation,
                trusted_owner,
                trusted_group,
                deadline,
                runtime_selection,
            )
            state.ownership_result = ownership_result
            normal = operation.settle(ownership_result.dispatch, ownership_result.carrier_completion)
            state.note_deadline()
            observation = ownership_result.observation
            if (
                not normal
                or state.deadline_exceeded
                or ownership_result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
                or observation is None
                or observation.state is not AccountObservationState.RESOLVED
                or observation.ownership is None
            ):
                return state.finish()
            state.result = exchange(
                operation,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                uid=observation.ownership.uid,
                gid=observation.ownership.gid,
                mode=mode,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
            )
            operation.settle(state.result.dispatch, state.result.carrier_completion)
            state.note_deadline()
            return state.finish()
        except BaseException as control:
            state.note_deadline()
            raise control from OwnedFileControlFact(state.finish())
    finally:
        borrow.close()


def _validate_metadata_inputs(
    operation: FileMetadataOperation,
    trusted_root_path: str,
    relative_path: str,
    trusted_owner: str,
    trusted_group: str,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
) -> None:
    ownership_failed = False
    try:
        encode_file_ownership_request(FileOwnershipRequest(_VALIDATION_NONCE, trusted_owner, trusted_group))
    except FileOwnershipRequestError:
        ownership_failed = True
    if ownership_failed:
        raise ValidationError("File metadata requires valid UTF-8 owner and group names")
    request_failure: FileMetadataFailureCode | None = None
    try:
        encode_file_metadata_request(
            FileMetadataRequest(
                _VALIDATION_NONCE,
                operation,
                trusted_root_path,
                relative_path,
                0,
                0,
                mode,
                deadline.remaining(),
                plan.expected,
            )
        )
    except FileMetadataRequestError as error:
        request_failure = error.failure
    if request_failure is FileMetadataFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File metadata request exceeds the 32768-byte manifest bound")
    if request_failure is not None:
        raise ValidationError("File metadata request contains an invalid field")
