"""Core-owned state for concrete private file calls."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, cast

from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadControlFact,
    FileDownloadOutcome,
    _prepare_download,
    _prepare_download_from_binding,
    _PreparedDownload,
)
from agentworks.execution._file_download import (
    _validate_inputs as _validate_download_inputs,
)
from agentworks.execution._file_effect_gate_exchange import (
    GateControlMutationUncertain,
    GateControlObservationState,
    exchange_file_effect_gate,
)
from agentworks.execution._file_effect_gate_protocol import GateControlOperation
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
from agentworks.execution._file_json import (
    FileJsonBinding,
    FileJsonControlFact,
    FileJsonOutcome,
    JsonFileStrategy,
    _prepare_json_update,
    _PreparedJsonUpdate,
)
from agentworks.execution._file_metadata_protocol import FileMetadataOperation
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    MAX_PACKAGE_UPLOAD_MEMBERS,
    FileCallFamily,
    FileCallObligation,
    FileCallObligationCodecError,
    FileCallUncertainty,
    encode_file_call_admission,
    encode_file_call_obligation,
)
from agentworks.execution._file_operations import (
    FileInventoryBinding,
    FileMetadataBinding,
    FileRemoveBinding,
    FileStatBinding,
    OwnedFileBinding,
    OwnedFileControlFact,
    OwnedFileOutcome,
    _prepare_inventory,
    _prepare_metadata,
    _prepare_remove,
    _prepare_stat,
    _PreparedInventory,
    _PreparedMetadata,
    _PreparedRemove,
    _PreparedStat,
)
from agentworks.execution._file_upload import (
    FileUploadBinding,
    FileUploadControlFact,
    FileUploadOutcome,
    FileUploadStatus,
    _prepare_upload,
    _prepare_upload_from_binding,
    _PreparedUpload,
)
from agentworks.execution._file_upload import _validate_inputs as _validate_upload_inputs
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import Dispatch
from agentworks.operations import LifecycleObligation, _PreRegistrationClosingRefusal, release_borrow_after_custody

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.execution._file_effect_gate import FileEffectGateBinding
    from agentworks.execution._file_inventory_exchange import FileInventoryCandidateResult
    from agentworks.execution._file_metadata_exchange import FileMetadataCandidateResult
    from agentworks.execution._file_object_exchange import FileObjectCandidateResult
    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_publication import Create, Match, Replace
    from agentworks.execution._file_publication_wire import BoundPublicationCleanupDebt
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._local_download_stage import LocalDownloadStage
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution._scratch import ScratchReference
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import ByteSink, ByteSource, Carrier, Deadline
    from agentworks.execution.files import NewMetadata
    from agentworks.operations import OperationBorrow, OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileDownload:
    """Captured custody for one download with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileDownloadBinding
    outcome: FileDownloadOutcome = field(repr=False)


@dataclass(slots=True, repr=False)
class _ActiveFileCall[BindingT, PreparedT, OutcomeT]:
    carrier: Carrier
    binding: BindingT
    borrow: OperationBorrow
    prepared: PreparedT
    obligation_id: str
    obligation: LifecycleObligation | None = None
    outcome: OutcomeT | None = None


@dataclass(frozen=True, slots=True, repr=False)
class _FileCallAdmission:
    obligation_id: str
    payload: bytes


class _FileCallBinding(Protocol):
    @property
    def trusted_root_path(self) -> str: ...

    @property
    def relative_path(self) -> str: ...

    @property
    def identity_plan(self) -> IdentityPlan: ...

    @property
    def runtime_selection(self) -> RuntimeSelection: ...


@dataclass(frozen=True, slots=True, repr=False)
class _PendingDownloadSetup:
    setup: FileEffectGateSetup
    token: bytes
    operation: BorrowedFixedHelperCarrier


type _ActiveFileDownload = _ActiveFileCall[
    FileDownloadBinding, _PreparedDownload | _PendingDownloadSetup, FileDownloadOutcome
]


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileUpload:
    """Captured custody for one upload with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileUploadBinding
    outcome: FileUploadOutcome = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class PackageUploadMember:
    """One caller-held package member; source bytes never enter the ledger."""

    relative_path: str
    source: ByteSource = field(repr=False)
    size: int
    condition: Create | Replace | Match
    create_metadata: NewMetadata


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedPackageUpload:
    """The one child retained after a batch stops."""

    index: int
    carrier: Carrier = field(repr=False)
    binding: FileUploadBinding
    outcome: FileUploadOutcome | None = field(default=None, repr=False)
    checkpoint_pending: bool = False


class PackageUploadStopped(Exception):
    """A member did not finish; the caller must not infer a remaining plan."""

    def __init__(self, index: int, outcome: FileUploadOutcome) -> None:
        self.index = index
        self.outcome = outcome
        super().__init__("private package upload stopped at one member")


@dataclass(frozen=True, slots=True, repr=False)
class _PendingUploadSetup:
    setup: FileEffectGateSetup
    token: bytes
    operation: BorrowedFixedHelperCarrier
    source: ByteSource = field(repr=False)
    condition: Create | Replace | Match
    create_metadata: NewMetadata | None


type _ActiveFileUpload = _ActiveFileCall[FileUploadBinding, _PreparedUpload | _PendingUploadSetup, FileUploadOutcome]


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileJsonUpdate:
    """Captured custody for one JSON update with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileJsonBinding
    outcome: FileJsonOutcome = field(repr=False)


type _ActiveFileJsonUpdate = _ActiveFileCall[FileJsonBinding, _PreparedJsonUpdate, FileJsonOutcome]

type _ActiveFileStat = _ActiveFileCall[FileStatBinding, _PreparedStat, OwnedFileOutcome[FileObjectCandidateResult]]
type _ActiveFileInventory = _ActiveFileCall[
    FileInventoryBinding, _PreparedInventory, OwnedFileOutcome[FileInventoryCandidateResult]
]
type _ActiveFileRemove = _ActiveFileCall[
    FileRemoveBinding, _PreparedRemove, OwnedFileOutcome[FileObjectCandidateResult]
]
type _ActiveFileMetadata = _ActiveFileCall[
    FileMetadataBinding, _PreparedMetadata, OwnedFileOutcome[FileMetadataCandidateResult]
]
type _OwnedFileOutcome = (
    OwnedFileOutcome[FileObjectCandidateResult]
    | OwnedFileOutcome[FileInventoryCandidateResult]
    | OwnedFileOutcome[FileMetadataCandidateResult]
)


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedOwnedFile:
    """Captured custody for one single-file call with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: OwnedFileBinding
    outcome: _OwnedFileOutcome = field(repr=False)


class FileOperation:
    """One core file state shared by all views of an existing owner."""

    def __init__(self, owner: OperationOwner, target: ManagedTargetIdentity) -> None:
        """Bind file calls to one exact owner scope and managed target."""
        scope = owner.ownership.scope
        if (
            type(target) is not ManagedTargetIdentity
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
        ):
            raise ValidationError("File operation target must match its owner scope")
        self._owner = owner
        self._target = target
        self._active_downloads: dict[int, _ActiveFileDownload] = {}
        self._unfinished_downloads: list[UnfinishedFileDownload] = []
        self._unfinished_local_download: LocalDownloadStage | None = None
        self._active_uploads: dict[int, _ActiveFileUpload] = {}
        self._unfinished_uploads: list[UnfinishedFileUpload] = []
        self._active_package_uploads: dict[int, _ActiveFileUpload] = {}
        self._unfinished_package_uploads: list[UnfinishedPackageUpload] = []
        self._active_json_updates: dict[int, _ActiveFileJsonUpdate] = {}
        self._unfinished_json_updates: list[UnfinishedFileJsonUpdate] = []
        self._active_stats: dict[int, _ActiveFileStat] = {}
        self._active_inventories: dict[int, _ActiveFileInventory] = {}
        self._active_removals: dict[int, _ActiveFileRemove] = {}
        self._active_metadata: dict[int, _ActiveFileMetadata] = {}
        self._unfinished_owned_files: list[UnfinishedOwnedFile] = []

    @property
    def active_downloads(self) -> tuple[_ActiveFileDownload, ...]:
        return tuple(self._active_downloads.values())

    @property
    def unfinished_downloads(self) -> tuple[UnfinishedFileDownload, ...]:
        return tuple(self._unfinished_downloads)

    @property
    def unfinished_local_download(self) -> LocalDownloadStage | None:
        """The one workstation stage still held by this in-memory operation."""
        return self._unfinished_local_download

    def retain_local_download_stage(self, stage: LocalDownloadStage) -> None:
        """Keep local cleanup custody separate from persisted remote obligations."""
        if self._unfinished_local_download is not None and self._unfinished_local_download is not stage:
            raise StateError("A local download stage already requires cleanup")
        self._unfinished_local_download = stage

    def retry_local_download_cleanup(self) -> bool:
        """Retry exact known cleanup before another local download is admitted."""
        stage = self._unfinished_local_download
        if stage is None:
            return True
        if stage.cleanup_uncertain:
            return False
        try:
            stage.abort()
        except Exception:
            return False
        if stage.cleanup_uncertain:
            return False
        self._unfinished_local_download = None
        return True

    @property
    def active_uploads(self) -> tuple[_ActiveFileUpload, ...]:
        return tuple(self._active_uploads.values())

    @property
    def unfinished_uploads(self) -> tuple[UnfinishedFileUpload, ...]:
        return tuple(self._unfinished_uploads)

    @property
    def unfinished_package_uploads(self) -> tuple[UnfinishedPackageUpload, ...]:
        return tuple(self._unfinished_package_uploads)

    @property
    def active_json_updates(self) -> tuple[_ActiveFileJsonUpdate, ...]:
        return tuple(self._active_json_updates.values())

    @property
    def unfinished_json_updates(self) -> tuple[UnfinishedFileJsonUpdate, ...]:
        return tuple(self._unfinished_json_updates)

    @property
    def active_stats(self) -> tuple[_ActiveFileStat, ...]:
        return tuple(self._active_stats.values())

    @property
    def active_inventories(self) -> tuple[_ActiveFileInventory, ...]:
        return tuple(self._active_inventories.values())

    @property
    def active_removals(self) -> tuple[_ActiveFileRemove, ...]:
        return tuple(self._active_removals.values())

    @property
    def active_metadata(self) -> tuple[_ActiveFileMetadata, ...]:
        return tuple(self._active_metadata.values())

    @property
    def unfinished_owned_files(self) -> tuple[UnfinishedOwnedFile, ...]:
        return tuple(self._unfinished_owned_files)

    def download(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> FileDownloadOutcome:
        """Run and capture one concrete download under a whole-call borrow."""
        if gate_setup is not None:
            return self._download_with_gate_setup(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                sink=sink,
                max_bytes=max_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                gate_setup=gate_setup,
                effect_gate=effect_gate,
            )
        if effect_gate is not None and (
            self._target.kind is not ManagedTargetKind.VM or effect_gate.scope_name != self._target.name
        ):
            raise ValidationError("Download effect gate must match the managed target")
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_download(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                sink=sink,
                max_bytes=max_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
                effect_gate=effect_gate,
            )
            admission = self._prepare_admission(FileCallFamily.DOWNLOAD, prepared.binding, token=prepared.state.token)
            active: _ActiveFileDownload = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_downloads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_downloads)

        return self._run_prepared_download(active, prepared)

    def _download_with_gate_setup(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        gate_setup: FileEffectGateSetup,
        effect_gate: FileEffectGateBinding | None,
    ) -> FileDownloadOutcome:
        """Attach pending setup, then promote that same row to a bound download."""
        borrow = self._owner.borrow()
        try:
            binding = _validate_download_inputs(
                trusted_root_path,
                relative_path,
                sink,
                max_bytes,
                plan,
                deadline,
                runtime_selection,
                borrow,
                None,
            )
            if (
                type(gate_setup) is not FileEffectGateSetup
                or effect_gate is not None
                or self._target.kind is not ManagedTargetKind.VM
                or runtime_selection.target_os is not RuntimeTargetOS.LINUX
                or type(gate_setup.guest) is not VMGuestIdentity
                or gate_setup.path != file_effect_gate_path(self._target, plan.expected.euid, gate_setup.guest)
            ):
                raise ValidationError("Download gate setup must match the selected Linux VM and identity")
            token = secrets.token_bytes(16)
            operation = BorrowedFixedHelperCarrier(carrier, borrow)
            admission = self._prepare_admission(FileCallFamily.DOWNLOAD, binding, token=token, gate_setup=gate_setup)
            pending = _PendingDownloadSetup(gate_setup, token, operation)
            active: _ActiveFileDownload = _ActiveFileCall(carrier, binding, borrow, pending, admission.obligation_id)
        except BaseException:
            borrow.close()
            raise

        # Keep concrete custody reachable before registration. Registration can
        # commit despite a lost reply, so only its explicit pre-start refusal
        # permits _install to close and discard the borrow.
        try:
            self._active_downloads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_downloads)
        # The pending record stays attached on any possibly-effectful setup or
        # failed publication. A lost publication reply may have committed
        # either row revision; the owner remains unresolved in both cases.
        result = exchange_file_effect_gate(
            operation,
            operation=GateControlOperation.SETUP,
            path=gate_setup.path,
            guest=gate_setup.guest,
            scope_name=self._target.name,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        )
        normal = operation.settle(result.dispatch, result.carrier_completion)
        if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
            borrow.close()
            self._active_downloads.pop(id(active))
            raise StateError("File-effect gate setup was not dispatched")
        observed = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not GateControlObservationState.RESOLVED
            or observed.binding is None
        ):
            raise GateControlMutationUncertain("file-effect gate setup was not acknowledged")
        bound = replace(binding, effect_gate=observed.binding)
        self._publish_retained(active, self._obligation(FileCallFamily.DOWNLOAD, bound, token=token))
        prepared = _prepare_download_from_binding(bound, sink, deadline, token, operation)
        active.binding = bound
        active.prepared = prepared
        return self._run_prepared_download(active, prepared)

    def _run_prepared_download(self, active: _ActiveFileDownload, prepared: _PreparedDownload) -> FileDownloadOutcome:
        """Complete one already attached download through the shared custody path."""
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileDownloadControlFact):
                try:
                    self._capture(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise
        self._capture(active, outcome)
        return outcome

    def upload(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        source: ByteSource,
        size: int,
        condition: Create | Replace | Match,
        create_metadata: NewMetadata,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> FileUploadOutcome:
        """Run and capture one concrete upload under a whole-call borrow."""
        if gate_setup is not None:
            return self._upload_with_gate_setup(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                source=source,
                size=size,
                condition=condition,
                create_metadata=create_metadata,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                gate_setup=gate_setup,
                effect_gate=effect_gate,
            )
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_upload(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                source=source,
                size=size,
                condition=condition,
                create_metadata=create_metadata,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
                effect_gate=effect_gate,
            )
            self._validate_upload_gate(prepared.binding)
            admission = self._prepare_admission(FileCallFamily.UPLOAD, prepared.binding, token=prepared.state.token)
            active: _ActiveFileUpload = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_uploads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_uploads)

        return self._run_prepared_upload(active, prepared)

    def _upload_with_gate_setup(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        source: ByteSource,
        size: int,
        condition: Create | Replace | Match,
        create_metadata: NewMetadata,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        gate_setup: FileEffectGateSetup,
        effect_gate: FileEffectGateBinding | None,
    ) -> FileUploadOutcome:
        """Register pending setup and promote its row before upload effects."""
        borrow = self._owner.borrow()
        try:
            binding, canonical_condition, canonical_metadata = _validate_upload_inputs(
                trusted_root_path,
                relative_path,
                source,
                size,
                condition,
                create_metadata,
                plan,
                deadline,
                runtime_selection,
                borrow,
                None,
            )
            if (
                type(gate_setup) is not FileEffectGateSetup
                or effect_gate is not None
                or self._target.kind is not ManagedTargetKind.VM
                or runtime_selection.target_os is not RuntimeTargetOS.LINUX
                or type(gate_setup.guest) is not VMGuestIdentity
                or vm_guest_boot_id(gate_setup.guest) != self._target.boot_id
                or gate_setup.path != file_effect_gate_path(self._target, plan.expected.euid, gate_setup.guest)
            ):
                raise ValidationError("Upload gate setup must match the selected Linux VM and identity")
            token = secrets.token_bytes(16)
            operation = BorrowedFixedHelperCarrier(carrier, borrow)
            admission = self._prepare_admission(FileCallFamily.UPLOAD, binding, token=token, gate_setup=gate_setup)
            pending = _PendingUploadSetup(gate_setup, token, operation, source, canonical_condition, canonical_metadata)
            active: _ActiveFileUpload = _ActiveFileCall(carrier, binding, borrow, pending, admission.obligation_id)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_uploads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_uploads)
        result = exchange_file_effect_gate(
            operation,
            operation=GateControlOperation.SETUP,
            path=gate_setup.path,
            guest=gate_setup.guest,
            scope_name=self._target.name,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        )
        normal = operation.settle(result.dispatch, result.carrier_completion)
        if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
            borrow.close()
            self._active_uploads.pop(id(active))
            raise StateError("File-effect gate setup was not dispatched")
        observed = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not GateControlObservationState.RESOLVED
            or observed.binding is None
        ):
            raise GateControlMutationUncertain("file-effect gate setup was not acknowledged")
        bound = replace(binding, effect_gate=observed.binding)
        self._publish_retained(active, self._obligation(FileCallFamily.UPLOAD, bound, token=token))
        prepared = _prepare_upload_from_binding(
            operation,
            source=source,
            deadline=deadline,
            inputs=(bound, canonical_condition, canonical_metadata),
            token=token,
        )
        active.binding = bound
        active.prepared = prepared
        return self._run_prepared_upload(active, prepared)

    def _run_prepared_upload(self, active: _ActiveFileUpload, prepared: _PreparedUpload) -> FileUploadOutcome:
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileUploadControlFact):
                try:
                    self._capture_upload(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise
        self._capture_upload(active, outcome)
        return outcome

    def upload_package(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        members: Sequence[PackageUploadMember],
        checkpoint: Callable[[int, FileUploadOutcome], None],
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> tuple[FileUploadOutcome, ...]:
        """Upload at most 4096 members under one row and a durable caller checkpoint.

        The caller's checkpoint must durably record each confirmed member before
        returning. No later member is prepared or dispatched until it returns.
        """
        if (
            not 1 <= len(members) <= MAX_PACKAGE_UPLOAD_MEMBERS
            or any(type(member) is not PackageUploadMember for member in members)
            or not callable(checkpoint)
        ):
            raise ValidationError("Package upload requires 1 to 4096 members")
        members = tuple(members)
        borrow = self._owner.borrow()
        obligation: LifecycleObligation | None = None
        active: _ActiveFileUpload | None = None
        first_prepared: _PreparedUpload | None = None
        if gate_setup is not None:
            active, first_prepared = self._setup_package_upload(
                carrier,
                trusted_root_path=trusted_root_path,
                member=members[0],
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                effect_gate=effect_gate,
                gate_setup=gate_setup,
                borrow=borrow,
            )
            obligation = self._require_obligation(active)
            effect_gate = first_prepared.binding.effect_gate
        completed: list[FileUploadOutcome] = []
        for index, member in enumerate(members):
            try:
                prepared = (
                    first_prepared
                    if index == 0 and first_prepared is not None
                    else _prepare_upload(
                        carrier,
                        trusted_root_path=trusted_root_path,
                        relative_path=member.relative_path,
                        source=member.source,
                        size=member.size,
                        condition=member.condition,
                        create_metadata=member.create_metadata,
                        plan=plan,
                        deadline=deadline,
                        runtime_selection=runtime_selection,
                        borrow=borrow,
                        effect_gate=effect_gate,
                    )
                )
                self._validate_upload_gate(prepared.binding)
            except BaseException:
                # A previous child has already passed its application checkpoint.
                borrow.close()
                if active is not None:
                    self._active_package_uploads.pop(id(active))
                raise
            if obligation is None:
                try:
                    admission = self._prepare_admission(
                        FileCallFamily.PACKAGE_UPLOAD, prepared.binding, token=prepared.state.token, batch_index=index
                    )
                except BaseException:
                    borrow.close()
                    raise
                active = _ActiveFileCall(carrier, prepared.binding, borrow, prepared, admission.obligation_id)
                self._active_package_uploads[id(active)] = active
                self._install(active, admission, self._active_package_uploads)
                obligation = self._require_obligation(active)
            elif index != 0 or first_prepared is None:
                assert active is not None
                active.binding = prepared.binding
                active.prepared = prepared
                active.outcome = None
                expected_revision = obligation.payload_revision
                try:
                    intended_payload = encode_file_call_admission(
                        self._obligation(
                            FileCallFamily.PACKAGE_UPLOAD,
                            prepared.binding,
                            token=prepared.state.token,
                            batch_index=index,
                        )
                    )
                except FileCallObligationCodecError:
                    borrow.close()
                    self._active_package_uploads.pop(id(active))
                    raise ValidationError("Package child lifecycle recovery identity is too large or invalid") from None
                try:
                    try:
                        published = obligation.publish_payload(
                            expected_revision=expected_revision,
                            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                            payload=intended_payload,
                        )
                    except Exception:
                        # Exact repetition reconciles a commit whose reply was lost.
                        published = obligation.publish_payload(
                            expected_revision=expected_revision,
                            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                            payload=intended_payload,
                        )
                    if published.payload_revision != expected_revision + 1 or published.payload != intended_payload:
                        raise StateError("Package upload child publication did not advance exactly once")
                except BaseException:
                    # The row may name either the checkpointed predecessor or
                    # this prepared child. Keep the borrow and prepared child
                    # attached; no child may dispatch before exact confirmation.
                    raise
            try:
                outcome = prepared.run()
            except BaseException as control:
                fact = control.__cause__
                if isinstance(fact, FileUploadControlFact):
                    assert active is not None
                    try:
                        self._stop_package_upload(active, index, fact.outcome)
                    except BaseException:
                        raise control from fact
                raise
            assert active is not None
            active.outcome = outcome
            if outcome.status is not FileUploadStatus.COMPLETE or outcome.requires_owner_retention:
                self._stop_package_upload(active, index, outcome)
                raise PackageUploadStopped(index, outcome)
            try:
                checkpoint(index, outcome)
            except BaseException:
                # The checkpoint may have committed. The exact child remains
                # retained; takeover must consult application state.
                release_borrow_after_custody(borrow, retain_effect=True)
                self._unfinished_package_uploads.append(
                    UnfinishedPackageUpload(index, carrier, prepared.binding, outcome, checkpoint_pending=True)
                )
                self._active_package_uploads.pop(id(active))
                raise
            completed.append(outcome)
        borrow.close()
        assert active is not None
        self._active_package_uploads.pop(id(active))
        return tuple(completed)

    def _setup_package_upload(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        member: PackageUploadMember,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        effect_gate: FileEffectGateBinding | None,
        gate_setup: FileEffectGateSetup,
        borrow: OperationBorrow,
    ) -> tuple[_ActiveFileUpload, _PreparedUpload]:
        """Publish package index zero as setup, then bind it before any child work."""
        try:
            binding, condition, metadata = _validate_upload_inputs(
                trusted_root_path,
                member.relative_path,
                member.source,
                member.size,
                member.condition,
                member.create_metadata,
                plan,
                deadline,
                runtime_selection,
                borrow,
                None,
            )
            if (
                type(gate_setup) is not FileEffectGateSetup
                or effect_gate is not None
                or self._target.kind is not ManagedTargetKind.VM
                or runtime_selection.target_os is not RuntimeTargetOS.LINUX
                or type(gate_setup.guest) is not VMGuestIdentity
                or vm_guest_boot_id(gate_setup.guest) != self._target.boot_id
                or gate_setup.path != file_effect_gate_path(self._target, plan.expected.euid, gate_setup.guest)
            ):
                raise ValidationError("Package upload gate setup must match the selected Linux VM and identity")
            token = secrets.token_bytes(16)
            operation = BorrowedFixedHelperCarrier(carrier, borrow)
            admission = self._prepare_admission(
                FileCallFamily.PACKAGE_UPLOAD, binding, token=token, batch_index=0, gate_setup=gate_setup
            )
            pending = _PendingUploadSetup(gate_setup, token, operation, member.source, condition, metadata)
            active: _ActiveFileUpload = _ActiveFileCall(carrier, binding, borrow, pending, admission.obligation_id)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_package_uploads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_package_uploads)
        result = exchange_file_effect_gate(
            operation,
            operation=GateControlOperation.SETUP,
            path=gate_setup.path,
            guest=gate_setup.guest,
            scope_name=self._target.name,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
        )
        normal = operation.settle(result.dispatch, result.carrier_completion)
        if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
            borrow.close()
            self._active_package_uploads.pop(id(active))
            raise StateError("File-effect gate setup was not dispatched")
        observed = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not GateControlObservationState.RESOLVED
            or observed.binding is None
        ):
            raise GateControlMutationUncertain("file-effect gate setup was not acknowledged")
        bound = replace(binding, effect_gate=observed.binding)
        self._validate_upload_gate(bound)
        self._publish_retained(
            active, self._obligation(FileCallFamily.PACKAGE_UPLOAD, bound, token=token, batch_index=0)
        )
        prepared = _prepare_upload_from_binding(
            operation,
            source=member.source,
            deadline=deadline,
            inputs=(bound, condition, metadata),
            token=token,
        )
        active.binding = bound
        active.prepared = prepared
        return active, prepared

    def _validate_upload_gate(self, binding: FileUploadBinding) -> None:
        """Match a caller-supplied gate to this selected managed VM."""
        effect_gate = binding.effect_gate
        if effect_gate is None:
            return
        if (
            self._target.kind is not ManagedTargetKind.VM
            or binding.runtime_selection.target_os is not RuntimeTargetOS.LINUX
            or vm_guest_boot_id(effect_gate.guest) != self._target.boot_id
            or effect_gate.scope_name != self._target.name
            or effect_gate.path
            != file_effect_gate_path(self._target, binding.identity_plan.expected.euid, effect_gate.guest)
        ):
            raise ValidationError("Upload effect gate must match the selected Linux VM and identity")

    def _stop_package_upload(self, active: _ActiveFileUpload, index: int, outcome: FileUploadOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.PACKAGE_UPLOAD,
                    active.binding,
                    token=outcome.token,
                    batch_index=index,
                    scratch_reference=outcome.reference,
                    scratch_cleanup_debt=outcome.scratch_cleanup_debt,
                    publication_cleanup_debt=outcome.publication_cleanup_debt,
                    uncertainty=self._upload_uncertainty(outcome),
                ),
            )
            self._unfinished_package_uploads.append(
                UnfinishedPackageUpload(index, active.carrier, active.binding, outcome)
            )
        release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_package_uploads.pop(id(active))

    def update_json(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        source: bytes,
        strategy: JsonFileStrategy,
        create: bool,
        create_metadata: NewMetadata,
        max_bytes: int,
        max_depth: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileJsonOutcome:
        """Run and capture one JSON update under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_json_update(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                source=source,
                strategy=strategy,
                create=create,
                create_metadata=create_metadata,
                max_bytes=max_bytes,
                max_depth=max_depth,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            admission = self._prepare_admission(FileCallFamily.JSON_UPDATE, prepared.binding)
            active: _ActiveFileJsonUpdate = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_json_updates[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_json_updates)
        prepared.set_child_upload_callback(lambda child, attempt: self._publish_json_child(active, child, attempt))

        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileJsonControlFact):
                try:
                    self._capture_json_update(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise

        self._capture_json_update(active, outcome)
        return outcome

    def stat(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileObjectCandidateResult]:
        """Run and capture one object observation under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_stat(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            admission = self._prepare_admission(FileCallFamily.STAT, prepared.binding)
            active: _ActiveFileStat = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_stats[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_stats)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.STAT)
                    self._active_stats.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.STAT)
        self._active_stats.pop(id(active))
        return outcome

    def list_directory(
        self,
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
    ) -> OwnedFileOutcome[FileInventoryCandidateResult]:
        """Run and capture one directory inventory under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_inventory(
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
            )
            admission = self._prepare_admission(FileCallFamily.INVENTORY, prepared.binding)
            active: _ActiveFileInventory = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_inventories[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_inventories)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.INVENTORY)
                    self._active_inventories.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.INVENTORY)
        self._active_inventories.pop(id(active))
        return outcome

    def remove(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        expected_kind: FileKind,
        expected_revision: FileRevision,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileObjectCandidateResult]:
        """Run and capture one conditional removal under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_remove(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                expected_kind=expected_kind,
                expected_revision=expected_revision,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            admission = self._prepare_admission(FileCallFamily.REMOVE, prepared.binding)
            active: _ActiveFileRemove = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_removals[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_removals)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.REMOVE)
                    self._active_removals.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.REMOVE)
        self._active_removals.pop(id(active))
        return outcome

    def set_metadata(
        self,
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
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one metadata convergence under one borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_metadata(
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
            )
            admission = self._prepare_admission(FileCallFamily.SET_METADATA, prepared.binding)
            active: _ActiveFileMetadata = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_metadata[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_metadata)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.SET_METADATA)
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.SET_METADATA)
        self._active_metadata.pop(id(active))
        return outcome

    def ensure_directory(
        self,
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
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one directory convergence under one borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_metadata(
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
            )
            admission = self._prepare_admission(FileCallFamily.ENSURE_DIRECTORY, prepared.binding)
            active: _ActiveFileMetadata = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_metadata[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, admission, self._active_metadata)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.ENSURE_DIRECTORY)
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.ENSURE_DIRECTORY)
        self._active_metadata.pop(id(active))
        return outcome

    def _install[BindingT: _FileCallBinding, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        admission: _FileCallAdmission,
        active_records: dict[int, _ActiveFileCall[BindingT, PreparedT, OutcomeT]],
    ) -> None:
        try:
            active.obligation = active.borrow.install_dispatch_obligation(
                admission.obligation_id,
                "file-call",
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=admission.payload,
            )
        except _PreRegistrationClosingRefusal:
            active.borrow.close()
            active_records.pop(id(active))
            raise

    def _prepare_admission(
        self,
        family: FileCallFamily,
        binding: _FileCallBinding,
        *,
        token: bytes | None = None,
        batch_index: int | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> _FileCallAdmission:
        try:
            payload = encode_file_call_admission(
                self._obligation(family, binding, token=token, batch_index=batch_index, gate_setup=gate_setup)
            )
        except FileCallObligationCodecError:
            raise ValidationError("File call lifecycle recovery identity is too large or invalid") from None
        return _FileCallAdmission(secrets.token_hex(16), payload)

    def _publish_json_child(
        self,
        active: _ActiveFileJsonUpdate,
        child: _PreparedUpload,
        attempt: int,
    ) -> None:
        obligation = self._require_obligation(active)
        payload = encode_file_call_obligation(
            self._obligation(
                FileCallFamily.JSON_UPDATE,
                active.binding,
                token=child.state.token,
                attempt=attempt,
            )
        )
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=payload,
        )

    def _obligation(
        self,
        family: FileCallFamily,
        binding: _FileCallBinding,
        *,
        token: bytes | None = None,
        attempt: int | None = None,
        batch_index: int | None = None,
        uncertainty: frozenset[FileCallUncertainty] = frozenset(),
        scratch_reference: ScratchReference | None = None,
        scratch_cleanup_debt: ScratchCleanupDebt | None = None,
        publication_cleanup_debt: BoundPublicationCleanupDebt | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> FileCallObligation:
        return FileCallObligation(
            family=family,
            target=self._target,
            root=binding.trusted_root_path,
            relative_path=binding.relative_path,
            identity_plan=binding.identity_plan,
            runtime_selection=binding.runtime_selection,
            token=token,
            attempt=attempt,
            batch_index=batch_index,
            scratch_reference=scratch_reference,
            scratch_cleanup_debt=scratch_cleanup_debt,
            publication_cleanup_debt=publication_cleanup_debt,
            effect_gate=(
                binding.effect_gate
                if (
                    (family is FileCallFamily.DOWNLOAD and isinstance(binding, FileDownloadBinding))
                    or (
                        family in {FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD}
                        and isinstance(binding, FileUploadBinding)
                    )
                )
                else None
            ),
            gate_setup=gate_setup,
            uncertainty=uncertainty,
        )

    @staticmethod
    def _require_obligation[BindingT, PreparedT, OutcomeT](
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
    ) -> LifecycleObligation:
        obligation = active.obligation
        if obligation is None:
            raise AssertionError("attached file call has no lifecycle obligation")
        return obligation

    def _publish_retained[BindingT, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        recovery: FileCallObligation,
    ) -> None:
        obligation = self._require_obligation(active)
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(recovery),
        )

    @staticmethod
    def _uncertainty(
        *,
        pending_remote_effects: bool,
        coordination_uncertain: bool,
    ) -> frozenset[FileCallUncertainty]:
        result: set[FileCallUncertainty] = set()
        if pending_remote_effects:
            result.add(FileCallUncertainty.PENDING_REMOTE_EFFECT)
        if coordination_uncertain:
            result.add(FileCallUncertainty.COORDINATION_UNCERTAINTY)
        return frozenset(result)

    def _upload_uncertainty(self, outcome: FileUploadOutcome) -> frozenset[FileCallUncertainty]:
        result = set(
            self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
        )
        if outcome.stage_ownership_uncertain or outcome.scratch_cleanup_debt is not None:
            result.add(FileCallUncertainty.SCRATCH_OWNERSHIP)
        if outcome.publication_ownership_uncertain or outcome.publication_cleanup_debt is not None:
            result.add(FileCallUncertainty.PUBLICATION_OWNERSHIP)
        return frozenset(result)

    def _capture_owned[BindingT: _FileCallBinding, PreparedT, ResultT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OwnedFileOutcome[ResultT]],
        outcome: OwnedFileOutcome[ResultT],
        family: FileCallFamily,
    ) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._publish_retained(
                active,
                self._obligation(
                    family,
                    active.binding,
                    uncertainty=self._uncertainty(
                        pending_remote_effects=outcome.pending_remote_effects,
                        coordination_uncertain=outcome.coordination_uncertain,
                    ),
                ),
            )
            self._unfinished_owned_files.append(
                UnfinishedOwnedFile(
                    active.carrier,
                    cast("OwnedFileBinding", active.binding),
                    cast("_OwnedFileOutcome", outcome),
                )
            )
        release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)

    def _capture(self, active: _ActiveFileDownload, outcome: FileDownloadOutcome) -> None:
        active.outcome = outcome
        prepared = active.prepared
        assert isinstance(prepared, _PreparedDownload)
        prepared.release_sink()
        if outcome.requires_owner_retention:
            uncertainty = self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
            if outcome.snapshot_ownership_uncertain or outcome.cleanup_debt is not None:
                uncertainty = uncertainty | frozenset({FileCallUncertainty.SCRATCH_OWNERSHIP})
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.DOWNLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.ready._reference if outcome.ready is not None else None,
                    scratch_cleanup_debt=outcome.cleanup_debt,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished(UnfinishedFileDownload(active.carrier, active.binding, outcome))
        release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_downloads.pop(id(active))

    def _retain_unfinished(self, download: UnfinishedFileDownload) -> None:
        self._unfinished_downloads.append(download)

    def _capture_upload(self, active: _ActiveFileUpload, outcome: FileUploadOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            uncertainty = self._upload_uncertainty(outcome)
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.UPLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.reference,
                    scratch_cleanup_debt=outcome.scratch_cleanup_debt,
                    publication_cleanup_debt=outcome.publication_cleanup_debt,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished_upload(UnfinishedFileUpload(active.carrier, active.binding, outcome))
        release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_uploads.pop(id(active))

    def _retain_unfinished_upload(self, upload: UnfinishedFileUpload) -> None:
        self._unfinished_uploads.append(upload)

    def _capture_json_update(self, active: _ActiveFileJsonUpdate, outcome: FileJsonOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            upload = outcome.upload_outcome
            uncertainty = self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
            if upload is not None:
                uncertainty = uncertainty | self._upload_uncertainty(upload)
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.JSON_UPDATE,
                    active.binding,
                    token=upload.token if upload is not None else None,
                    attempt=outcome.publication_attempts if upload is not None else None,
                    scratch_reference=upload.reference if upload is not None else None,
                    scratch_cleanup_debt=upload.scratch_cleanup_debt if upload is not None else None,
                    publication_cleanup_debt=upload.publication_cleanup_debt if upload is not None else None,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished_json_update(UnfinishedFileJsonUpdate(active.carrier, active.binding, outcome))
        release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_json_updates.pop(id(active))

    def _retain_unfinished_json_update(self, update: UnfinishedFileJsonUpdate) -> None:
        self._unfinished_json_updates.append(update)
