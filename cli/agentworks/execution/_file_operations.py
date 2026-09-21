"""Owned composition for private single-file helper exchanges."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Never, Protocol

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
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import Carrier, Deadline, Dispatch, ExitStatus
    from agentworks.operations import OperationBorrow

_VALIDATION_NONCE = "0" * 32


class _CandidateResult(Protocol):
    @property
    def dispatch(self) -> Dispatch: ...

    @property
    def carrier_completion(self) -> ExitStatus | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class FileStatBinding:
    trusted_root_path: str
    relative_path: str
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileInventoryBinding:
    trusted_root_path: str
    relative_path: str
    max_entries: int
    max_depth: int
    max_encoded_bytes: int
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileRemoveBinding:
    trusted_root_path: str
    relative_path: str
    expected_kind: FileKind
    expected_revision: FileRevision
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileMetadataBinding:
    operation: FileMetadataOperation
    trusted_root_path: str
    relative_path: str
    trusted_owner: str
    trusted_group: str
    mode: int
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


type OwnedFileBinding = FileStatBinding | FileInventoryBinding | FileRemoveBinding | FileMetadataBinding


@dataclass(frozen=True, slots=True, repr=False)
class OwnedFileOutcome[T]:
    """Returned exchange facts and independent operation-ownership state."""

    binding: OwnedFileBinding = field(repr=False)
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
    binding: OwnedFileBinding
    operation: BorrowedFixedHelperCarrier
    deadline: Deadline
    result: T | None = None
    ownership_result: FileOwnershipResolutionResult | None = None
    deadline_exceeded: bool = False
    control_outcome: OwnedFileOutcome[T] | None = None

    def note_deadline(self) -> None:
        self.deadline_exceeded = self.deadline_exceeded or self.deadline.expired

    def finish(self) -> OwnedFileOutcome[T]:
        return OwnedFileOutcome(
            self.binding,
            self.result,
            self.ownership_result,
            self.deadline_exceeded,
            self.operation.pending_remote_effects,
            self.operation.coordination_uncertain,
            self.operation.requires_owner_retention,
        )

    def run_exchange(self, exchange: Callable[[], T]) -> OwnedFileOutcome[T]:
        if self.deadline.expired:
            self.deadline_exceeded = True
            return self.finish()
        try:
            self.result = exchange()
            self.operation.settle(self.result.dispatch, self.result.carrier_completion)
            self.note_deadline()
            return self.finish()
        except BaseException as control:
            self.raise_control(control)

    def raise_control(self, control: BaseException) -> Never:
        self.note_deadline()
        try:
            outcome = self.finish()
            fact = OwnedFileControlFact(outcome)
            self.control_outcome = outcome
        except BaseException:
            raise control from None
        raise control from fact


@dataclass(slots=True, repr=False)
class _PreparedStat:
    binding: FileStatBinding
    state: _State[FileObjectCandidateResult]

    def run(self) -> OwnedFileOutcome[FileObjectCandidateResult]:
        binding = self.binding
        return self.state.run_exchange(
            lambda: exchange_stat_file(
                self.state.operation,
                trusted_root_path=binding.trusted_root_path,
                relative_path=binding.relative_path,
                plan=binding.identity_plan,
                deadline=self.state.deadline,
                runtime_selection=binding.runtime_selection,
            )
        )


@dataclass(slots=True, repr=False)
class _PreparedInventory:
    binding: FileInventoryBinding
    state: _State[FileInventoryCandidateResult]

    def run(self) -> OwnedFileOutcome[FileInventoryCandidateResult]:
        binding = self.binding
        return self.state.run_exchange(
            lambda: exchange_list_directory(
                self.state.operation,
                trusted_root_path=binding.trusted_root_path,
                relative_path=binding.relative_path,
                max_entries=binding.max_entries,
                max_depth=binding.max_depth,
                max_encoded_bytes=binding.max_encoded_bytes,
                plan=binding.identity_plan,
                deadline=self.state.deadline,
                runtime_selection=binding.runtime_selection,
            )
        )


@dataclass(slots=True, repr=False)
class _PreparedRemove:
    binding: FileRemoveBinding
    state: _State[FileObjectCandidateResult]

    def run(self) -> OwnedFileOutcome[FileObjectCandidateResult]:
        binding = self.binding
        return self.state.run_exchange(
            lambda: exchange_remove_file(
                self.state.operation,
                trusted_root_path=binding.trusted_root_path,
                relative_path=binding.relative_path,
                expected_kind=binding.expected_kind,
                expected_revision=binding.expected_revision,
                plan=binding.identity_plan,
                deadline=self.state.deadline,
                runtime_selection=binding.runtime_selection,
            )
        )


@dataclass(slots=True, repr=False)
class _PreparedMetadata:
    binding: FileMetadataBinding
    state: _State[FileMetadataCandidateResult]

    def run(self) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        if self.state.deadline.expired:
            self.state.deadline_exceeded = True
            return self.state.finish()
        try:
            return self._run()
        except BaseException as control:
            self.state.raise_control(control)

    def _run(self) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        binding = self.binding
        ownership_result = resolve_file_ownership(
            self.state.operation,
            binding.trusted_owner,
            binding.trusted_group,
            self.state.deadline,
            binding.runtime_selection,
        )
        self.state.ownership_result = ownership_result
        normal = self.state.operation.settle(ownership_result.dispatch, ownership_result.carrier_completion)
        self.state.note_deadline()
        observation = ownership_result.observation
        if (
            not normal
            or self.state.deadline_exceeded
            or ownership_result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observation is None
            or observation.state is not AccountObservationState.RESOLVED
            or observation.ownership is None
        ):
            return self.state.finish()
        exchange = (
            set_file_metadata if binding.operation is FileMetadataOperation.SET_METADATA else ensure_file_directory
        )
        self.state.result = exchange(
            self.state.operation,
            trusted_root_path=binding.trusted_root_path,
            relative_path=binding.relative_path,
            uid=observation.ownership.uid,
            gid=observation.ownership.gid,
            mode=binding.mode,
            plan=binding.identity_plan,
            deadline=self.state.deadline,
            runtime_selection=binding.runtime_selection,
        )
        self.state.operation.settle(self.state.result.dispatch, self.state.result.carrier_completion)
        self.state.note_deadline()
        return self.state.finish()


def stat_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> OwnedFileOutcome[FileObjectCandidateResult]:
    """Run one object observation under the caller's active operation borrow."""
    return _prepare_stat(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_stat(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> _PreparedStat:
    binding = FileStatBinding(trusted_root_path, relative_path, plan, runtime_selection)
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    return _PreparedStat(binding, _State(binding, operation, deadline))


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
    borrow: OperationBorrow,
) -> OwnedFileOutcome[FileInventoryCandidateResult]:
    """Run one bounded inventory under the caller's active operation borrow."""
    return _prepare_inventory(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        max_entries=max_entries,
        max_depth=max_depth,
        max_encoded_bytes=max_encoded_bytes,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_inventory(
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
    borrow: OperationBorrow,
) -> _PreparedInventory:
    binding = FileInventoryBinding(
        trusted_root_path,
        relative_path,
        max_entries,
        max_depth,
        max_encoded_bytes,
        plan,
        runtime_selection,
    )
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    return _PreparedInventory(binding, _State(binding, operation, deadline))


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
    borrow: OperationBorrow,
) -> OwnedFileOutcome[FileObjectCandidateResult]:
    """Run one conditional removal under the caller's active operation borrow."""
    return _prepare_remove(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        expected_kind=expected_kind,
        expected_revision=expected_revision,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_remove(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    expected_kind: FileKind,
    expected_revision: FileRevision,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> _PreparedRemove:
    binding = FileRemoveBinding(
        trusted_root_path,
        relative_path,
        expected_kind,
        expected_revision,
        plan,
        runtime_selection,
    )
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    return _PreparedRemove(binding, _State(binding, operation, deadline))


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
    borrow: OperationBorrow,
) -> OwnedFileOutcome[FileMetadataCandidateResult]:
    """Resolve names and converge one object's metadata under one borrow."""
    return _prepare_metadata(
        carrier,
        operation=FileMetadataOperation.SET_METADATA,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        trusted_owner=trusted_owner,
        trusted_group=trusted_group,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


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
    borrow: OperationBorrow,
) -> OwnedFileOutcome[FileMetadataCandidateResult]:
    """Resolve names and create or converge one directory under one borrow."""
    return _prepare_metadata(
        carrier,
        operation=FileMetadataOperation.ENSURE_DIRECTORY,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        trusted_owner=trusted_owner,
        trusted_group=trusted_group,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_metadata(
    carrier: Carrier,
    *,
    operation: FileMetadataOperation,
    trusted_root_path: str,
    relative_path: str,
    trusted_owner: str,
    trusted_group: str,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> _PreparedMetadata:
    _validate_metadata_inputs(
        operation,
        trusted_root_path,
        relative_path,
        trusted_owner,
        trusted_group,
        mode,
        plan,
        deadline,
    )
    binding = FileMetadataBinding(
        operation,
        trusted_root_path,
        relative_path,
        trusted_owner,
        trusted_group,
        mode,
        plan,
        runtime_selection,
    )
    borrowed = BorrowedFixedHelperCarrier(carrier, borrow)
    return _PreparedMetadata(binding, _State(binding, borrowed, deadline))


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
