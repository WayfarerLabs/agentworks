"""Core-owned state for concrete private file calls."""

from __future__ import annotations

import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, cast

from agentworks.db.operations import OperationResourceKind, OperationScope
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
    _validate_binding as _validate_download_binding,
)
from agentworks.execution._file_download import (
    _validate_sink as _validate_download_sink,
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
from agentworks.execution._file_local_custody import LocalFileCall
from agentworks.execution._file_metadata_protocol import FileMetadataOperation
from agentworks.execution._file_obligation import (
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
    _prepare_upload,
    _prepare_upload_from_binding,
    _PreparedUpload,
)
from agentworks.execution._file_upload import _validate_inputs as _validate_upload_inputs
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import Dispatch
from agentworks.operations import LifecycleObligation, _is_pre_registration_refusal, release_borrow_after_custody

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.execution._file_effect_gate import FileEffectGateBinding
    from agentworks.execution._file_inventory_exchange import FileInventoryCandidateResult
    from agentworks.execution._file_local_download import FileLocalDownloadOutcome
    from agentworks.execution._file_metadata_exchange import FileMetadataCandidateResult
    from agentworks.execution._file_object_exchange import FileObjectCandidateResult
    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_publication import Create, Match, Replace
    from agentworks.execution._file_publication_wire import BoundPublicationCleanupDebt
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._local_download_stage import LocalDownloadStage
    from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimeSelection
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
    retained_payload: _RetainedFilePayload | None = None
    retained_payload_confirmed: bool = False
    capture_ready: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class _RetainedFilePayload:
    """Exact unfinished-custody publication, held across an interrupted reply."""

    expected_revision: int
    payload_version: int
    payload: bytes


@dataclass(frozen=True, slots=True, repr=False)
class _FileCallAdmission:
    obligation_id: str
    payload: bytes
    payload_version: int
    gate_setup: bool


class FileGateSetupControlFact(Exception):
    """Value-free prerequisite facts for interrupted gate admission, before file effects."""

    def __init__(self, runtime: RuntimePrerequisiteObservation | None = None) -> None:
        self.runtime_state = runtime.state if runtime is not None else None
        super().__init__("File gate admission stopped with retained coordination custody")


@contextmanager
def _gate_admission() -> Iterator[None]:
    """Attach coordination facts without replacing the original escaping control."""
    try:
        yield
    except BaseException as control:
        if isinstance(control.__cause__, FileGateSetupControlFact):
            raise
        cause = control.__cause__
        try:
            fact = FileGateSetupControlFact()
            fact.__cause__ = cause
        except BaseException:
            raise control from cause
        raise control from fact


class _FileCallBinding(Protocol):
    @property
    def bootstrap(self) -> _NumericGuestBootstrap | None: ...

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


@dataclass(slots=True, repr=False)
class _LocalDownloadCall:
    """One local download's serial borrow and finalization custody."""

    borrow: OperationBorrow
    remote_requires_owner_retention: bool = False
    active: _ActiveFileDownload | None = None
    dispatched: bool = False
    setup_started: bool = False
    stage: LocalDownloadStage | None = None
    local_outcome: FileLocalDownloadOutcome | None = None
    reduction_complete: bool = False


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

    def __init__(
        self,
        owner: OperationOwner,
        target: ManagedTargetIdentity | OperationScope,
        *,
        bootstrap: _NumericGuestBootstrap | None = None,
        guest: VMGuestIdentity | None = None,
    ) -> None:
        """Bind file calls to one managed VM or the exact ordinary host scope."""
        scope = owner.ownership.scope
        if type(target) is OperationScope:
            if target.resource_kind is not OperationResourceKind.PLATFORM_HOST or target != scope:
                raise ValidationError("File operation target must match its owner scope")
            if bootstrap is not None or guest is not None:
                raise ValidationError("Ordinary host file operations cannot carry guest identity")
        elif (
            type(target) is not ManagedTargetIdentity
            or target.kind is not ManagedTargetKind.VM
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
        ):
            raise ValidationError("File operation target must match its owner scope")
        if bootstrap is not None and (
            not isinstance(target, ManagedTargetIdentity) or vm_guest_boot_id(bootstrap.guest) != target.boot_id
        ):
            raise ValidationError("Numeric file bootstrap must match the selected VM boot")
        if guest is not None and (
            type(guest) is not VMGuestIdentity
            or not isinstance(target, ManagedTargetIdentity)
            or vm_guest_boot_id(guest) != target.boot_id
            or (bootstrap is not None and guest != bootstrap.guest)
        ):
            raise ValidationError("File guest identity must match the selected VM and bootstrap")
        self._owner = owner
        self._target = target
        self._bootstrap = bootstrap
        self._guest = bootstrap.guest if bootstrap is not None else guest
        self._active_downloads: dict[int, _ActiveFileDownload] = {}
        self._unfinished_downloads: list[UnfinishedFileDownload] = []
        self._unfinished_local_download: LocalDownloadStage | None = None
        self._local_download_call: _LocalDownloadCall | None = None
        self._local_file_call: LocalFileCall | None = None
        self._active_uploads: dict[int, _ActiveFileUpload] = {}
        self._unfinished_uploads: list[UnfinishedFileUpload] = []
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
    def has_unfinished_local_file_call(self) -> bool:
        """Whether a workstation original or its finalization still has custody."""
        return self._local_file_call is not None

    def begin_local_file(self) -> LocalFileCall:
        """Acquire serial admission before constructing or touching local originals."""
        borrow = self._owner.borrow()
        try:
            if self._local_file_call is not None:
                raise StateError("A local file call still holds core custody")
            call = LocalFileCall(borrow)
            self._local_file_call = call
            return call
        except BaseException:
            borrow.close()
            raise

    def finish_local_file(self, call: LocalFileCall, deadline: Deadline) -> None:
        """Close exact originals and release only after completed typed reduction."""
        if self._local_file_call is not call:
            raise StateError("Local file call is not active")
        try:
            resource = call.resource
            if resource is not None:
                resource.close(deadline)
                if not resource.settled:
                    raise StateError("Local file cleanup remains uncertain")
            if not call.reduction_complete or any(
                active.borrow is call.borrow for active in self._active_uploads.values()
            ):
                raise StateError("Local file capture or reduction remains incomplete")
            if not call.borrow.closed:
                release_borrow_after_custody(call.borrow, retain_effect=call.remote_requires_owner_retention)
            self._local_file_call = None
        except BaseException:
            self._owner.stop_admission()
            raise

    def settle_local_file_bookkeeping(self, deadline: Deadline) -> None:
        """Retry only retained original closure and captured bookkeeping."""
        call = self._local_file_call
        first: BaseException | None = None
        if call is not None and call.resource is not None and not call.resource.settled:
            try:
                call.resource.close(deadline)
            except BaseException as error:
                first = error
        try:
            self.settle_upload_bookkeeping()
        except BaseException as error:
            if first is None:
                first = error
        if first is not None:
            raise first
        if call is not None:
            self.finish_local_file(call, deadline)

    @property
    def unfinished_downloads(self) -> tuple[UnfinishedFileDownload, ...]:
        return tuple(self._unfinished_downloads)

    @property
    def retained_local_download_stage(self) -> LocalDownloadStage | None:
        """The one workstation stage still held by this in-memory operation."""
        return self._unfinished_local_download

    @property
    def has_unfinished_local_download_call(self) -> bool:
        """Whether whole-call finalization custody is still retained."""
        return self._local_download_call is not None

    def begin_local_download(self) -> _LocalDownloadCall:
        """Acquire whole-call serial admission before workstation effects."""
        borrow = self._owner.borrow()
        try:
            if self._local_download_call is not None:
                raise StateError("A local download call still holds core custody")
            call = _LocalDownloadCall(borrow)
        except BaseException:
            borrow.close()
            raise
        self._local_download_call = call
        return call

    def finish_local_download(self, call: _LocalDownloadCall) -> None:
        """Release only after local custody and public reduction have settled."""
        if self._local_download_call is not call:
            raise StateError("Local download call is not active")
        if any(active.borrow is call.borrow for active in self._active_downloads.values()):
            self._owner.stop_admission()
            raise StateError("Local download remote custody is still active")
        if call.active is not None and (call.local_outcome is None or not call.reduction_complete):
            self._owner.stop_admission()
            raise StateError("Download local capture remains incomplete")
        self._settle_download_call(call)

    def capture_local_download(self, call: _LocalDownloadCall, outcome: FileLocalDownloadOutcome) -> None:
        """Retain actual publication facts before exception-fact allocation or reduction."""
        call.local_outcome = outcome
        active = call.active
        if active is not None and active.obligation is not None and not call.dispatched and not call.setup_started:
            # Without gate setup, confirmed admission followed by constructor
            # refusal has no remote effect. SETUP custody survives that refusal.
            self._active_downloads.pop(id(active), None)

    def mark_download_reduction_complete(self, call: _LocalDownloadCall) -> None:
        """Record completion of the reduction phase before finalizing custody."""
        call.reduction_complete = True

    def settle_download_bookkeeping(self) -> None:
        """Retry captured debt publication and closure without file effects."""
        call = self._local_download_call
        try:
            for active in tuple(self._active_downloads.values()):
                if active.outcome is not None:
                    self._settle_download_capture(active, local_call=call if call and call.active is active else None)
            if call is not None:
                if call.local_outcome is None or not call.reduction_complete:
                    raise StateError("Download bookkeeping capture remains incomplete")
                self.finish_local_download(call)
        except BaseException:
            self._owner.stop_admission()
            raise

    def _settle_download_call(self, call: _LocalDownloadCall) -> None:
        try:
            if not call.borrow.closed:
                release_borrow_after_custody(call.borrow, retain_effect=call.remote_requires_owner_retention)
            self._local_download_call = None
        except BaseException:
            self._owner.stop_admission()
            raise

    def retain_local_download_stage(self, stage: LocalDownloadStage) -> None:
        """Keep local cleanup custody separate from persisted remote obligations."""
        if self._unfinished_local_download is not None and self._unfinished_local_download is not stage:
            raise StateError("A local download stage already requires cleanup")
        self._unfinished_local_download = stage

    def retry_local_download_cleanup(self, call: _LocalDownloadCall) -> bool:
        """Retry exact known cleanup before another local download is admitted."""
        if self._local_download_call is not call:
            raise StateError("Local download cleanup requires its active serial borrow")
        stage = self._unfinished_local_download
        if stage is None:
            return True
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

    def prepare_local_download(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        local_call: _LocalDownloadCall,
    ) -> None:
        """Admit the original remote child before constructing its workstation stage."""
        if self._local_download_call is not local_call or local_call.active is not None:
            raise StateError("Local download requires its unused active serial borrow")
        setup = self._select_gate_setup(plan, runtime_selection, None, None)
        if setup is not None:
            self._prepare_gated_download(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                sink=None,
                max_bytes=max_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                gate_setup=setup,
                effect_gate=None,
                local_call=local_call,
            )
            return
        binding = _validate_download_binding(
            trusted_root_path,
            relative_path,
            max_bytes,
            plan,
            deadline,
            runtime_selection,
            local_call.borrow,
            None,
            bootstrap=self._bootstrap,
        )
        token = secrets.token_bytes(16)
        operation = BorrowedFixedHelperCarrier(carrier, local_call.borrow)
        prepared = _prepare_download_from_binding(binding, None, deadline, token, operation)
        admission = self._prepare_admission(
            FileCallFamily.DOWNLOAD,
            binding,
            token=token,
        )
        active: _ActiveFileDownload = _ActiveFileCall(
            carrier,
            binding,
            local_call.borrow,
            prepared,
            admission.obligation_id,
        )
        self._active_downloads[id(active)] = active
        local_call.active = active
        try:
            self._install(active, admission, self._active_downloads, close_borrow_on_refusal=False)
        except BaseException as control:
            if _is_pre_registration_refusal(control):
                local_call.active = None
            raise

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
        local_call: _LocalDownloadCall | None = None,
    ) -> FileDownloadOutcome:
        """Run and capture one concrete download under a whole-call borrow."""
        if local_call is not None and (
            self._local_download_call is not local_call or gate_setup is not None or effect_gate is not None
        ):
            raise StateError("Local download requires its active serial borrow")
        if local_call is not None and local_call.active is not None:
            active = local_call.active
            prepared = active.prepared
            assert isinstance(prepared, _PreparedDownload)
            if local_call.dispatched:
                raise StateError("Local download child was already dispatched")
            prepared.attach_sink(sink)
            local_call.dispatched = True
            return self._run_prepared_download(active, prepared, local_call=local_call)
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
        if gate_setup is not None:
            active = self._prepare_gated_download(
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
                local_call=local_call,
            )
            prepared = active.prepared
            assert isinstance(prepared, _PreparedDownload)
            if local_call is not None:
                local_call.dispatched = True
            return self._run_prepared_download(active, prepared, local_call=local_call)
        if effect_gate is not None and (
            not isinstance(self._target, ManagedTargetIdentity)
            or self._target.kind is not ManagedTargetKind.VM
            or effect_gate.scope_name != self._target.name
        ):
            raise ValidationError("Download effect gate must match the managed target")
        borrow = self._owner.borrow() if local_call is None else local_call.borrow
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
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(
                FileCallFamily.DOWNLOAD,
                prepared.binding,
                token=prepared.state.token,
            )
            active = _ActiveFileCall(carrier, prepared.binding, borrow, prepared, admission.obligation_id)
        except BaseException:
            if local_call is None:
                borrow.close()
            raise

        try:
            self._active_downloads[id(active)] = active
        except BaseException:
            if local_call is None:
                borrow.close()
            raise
        self._install(active, admission, self._active_downloads, close_borrow_on_refusal=local_call is None)
        if local_call is not None:
            local_call.active = active
            local_call.dispatched = True
        return self._run_prepared_download(active, prepared, local_call=local_call)

    def _prepare_gated_download(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink | None,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        gate_setup: FileEffectGateSetup,
        effect_gate: FileEffectGateBinding | None,
        local_call: _LocalDownloadCall | None = None,
    ) -> _ActiveFileDownload:
        """Attach pending setup, then promote that same row to a bound download."""
        borrow = self._owner.borrow() if local_call is None else local_call.borrow
        try:
            if sink is not None:
                _validate_download_sink(sink)
            binding = _validate_download_binding(
                trusted_root_path,
                relative_path,
                max_bytes,
                plan,
                deadline,
                runtime_selection,
                borrow,
                None,
                bootstrap=self._bootstrap,
            )
            if (
                type(gate_setup) is not FileEffectGateSetup
                or effect_gate is not None
                or not isinstance(self._target, ManagedTargetIdentity)
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
            if local_call is None:
                borrow.close()
            raise

        # Keep concrete custody reachable before registration. Registration can
        # commit despite a lost reply, so only its explicit pre-start refusal
        # permits _install to close and discard the borrow.
        try:
            self._active_downloads[id(active)] = active
        except BaseException:
            if local_call is None:
                borrow.close()
            raise
        if local_call is not None:
            local_call.active = active
        with _gate_admission():
            try:
                self._install(active, admission, self._active_downloads, close_borrow_on_refusal=local_call is None)
            except BaseException as control:
                if local_call is not None and _is_pre_registration_refusal(control):
                    local_call.active = None
                raise
            if local_call is not None:
                local_call.setup_started = True
            self._bind_download_gate(active, sink, deadline, local_call=local_call)
        return active

    def _bind_download_gate(
        self,
        active: _ActiveFileDownload,
        sink: ByteSink | None,
        deadline: Deadline,
        *,
        local_call: _LocalDownloadCall | None,
    ) -> None:
        pending = active.prepared
        assert isinstance(pending, _PendingDownloadSetup)
        gate_setup, token, operation = pending.setup, pending.token, pending.operation
        binding = active.binding
        # The pending record stays attached on any possibly-effectful setup or
        # failed publication. A lost publication reply may have committed
        # either row revision; the owner remains unresolved in both cases.
        result = exchange_file_effect_gate(
            operation,
            operation=GateControlOperation.SETUP,
            path=gate_setup.path,
            guest=gate_setup.guest,
            scope_name=self._owner.ownership.scope.resource_name,
            plan=binding.identity_plan,
            deadline=deadline,
            runtime_selection=binding.runtime_selection,
            bootstrap=self._bootstrap,
        )
        normal = operation.settle(result.dispatch, result.carrier_completion)
        if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
            if local_call is None:
                active.borrow.close()
            else:
                local_call.active = None
            self._active_downloads.pop(id(active))
            raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact(
                result.runtime_prerequisite
            )
        observed = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not GateControlObservationState.RESOLVED
            or observed.binding is None
        ):
            raise GateControlMutationUncertain(
                "file-effect gate setup was not acknowledged"
            ) from FileGateSetupControlFact(result.runtime_prerequisite)
        bound = replace(binding, effect_gate=observed.binding)
        self._validate_effect_gate(bound)
        self._publish_retained(active, self._obligation(FileCallFamily.DOWNLOAD, bound, token=token))
        prepared = _prepare_download_from_binding(bound, sink, deadline, token, operation)
        active.binding = bound
        active.prepared = prepared

    def _run_prepared_download(
        self, active: _ActiveFileDownload, prepared: _PreparedDownload, *, local_call: _LocalDownloadCall | None = None
    ) -> FileDownloadOutcome:
        """Complete one already attached download through the shared custody path."""
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileDownloadControlFact):
                active.outcome = fact.outcome
                try:
                    self._capture(active, fact.outcome, local_call=local_call)
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        self._capture(active, outcome, local_call=local_call)
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
        local_call: LocalFileCall | None = None,
    ) -> FileUploadOutcome:
        """Run and capture one concrete upload under a whole-call borrow."""
        if local_call is not None and self._local_file_call is not local_call:
            raise StateError("Owned local upload requires its active serial borrow")
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
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
                local_call=local_call,
            )
        borrow = self._owner.borrow() if local_call is None else local_call.borrow
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
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(FileCallFamily.UPLOAD, prepared.binding, token=prepared.state.token)
            active: _ActiveFileUpload = _ActiveFileCall(
                carrier, prepared.binding, borrow, prepared, admission.obligation_id
            )
        except BaseException:
            if local_call is None:
                borrow.close()
            raise

        try:
            self._active_uploads[id(active)] = active
        except BaseException:
            if local_call is None:
                borrow.close()
            self._active_uploads.pop(id(active), None)
            raise
        self._install(active, admission, self._active_uploads, close_borrow_on_refusal=local_call is None)

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
        local_call: LocalFileCall | None,
    ) -> FileUploadOutcome:
        """Register pending setup and promote its row before upload effects."""
        borrow = self._owner.borrow() if local_call is None else local_call.borrow
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
                bootstrap=self._bootstrap,
            )
            if (
                type(gate_setup) is not FileEffectGateSetup
                or effect_gate is not None
                or not isinstance(self._target, ManagedTargetIdentity)
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
            if local_call is None:
                borrow.close()
            raise
        try:
            self._active_uploads[id(active)] = active
        except BaseException:
            if local_call is None:
                borrow.close()
            self._active_uploads.pop(id(active), None)
            raise
        with _gate_admission():
            self._install(active, admission, self._active_uploads, close_borrow_on_refusal=local_call is None)
            prepared = self._bind_upload_gate(active, deadline, local_call=local_call)
        return self._run_prepared_upload(active, prepared)

    def _bind_upload_gate(
        self, active: _ActiveFileUpload, deadline: Deadline, *, local_call: LocalFileCall | None
    ) -> _PreparedUpload:
        pending = active.prepared
        assert isinstance(pending, _PendingUploadSetup)
        gate_setup, token, operation = pending.setup, pending.token, pending.operation
        binding = active.binding
        result = exchange_file_effect_gate(
            operation,
            operation=GateControlOperation.SETUP,
            path=gate_setup.path,
            guest=gate_setup.guest,
            scope_name=self._owner.ownership.scope.resource_name,
            plan=binding.identity_plan,
            deadline=deadline,
            runtime_selection=binding.runtime_selection,
            bootstrap=self._bootstrap,
        )
        normal = operation.settle(result.dispatch, result.carrier_completion)
        if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
            if local_call is None:
                active.borrow.close()
            self._active_uploads.pop(id(active))
            raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact(
                result.runtime_prerequisite
            )
        observed = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not GateControlObservationState.RESOLVED
            or observed.binding is None
        ):
            raise GateControlMutationUncertain(
                "file-effect gate setup was not acknowledged"
            ) from FileGateSetupControlFact(result.runtime_prerequisite)
        bound = replace(binding, effect_gate=observed.binding)
        self._validate_effect_gate(bound)
        self._publish_retained(active, self._obligation(FileCallFamily.UPLOAD, bound, token=token))
        prepared = _prepare_upload_from_binding(
            operation,
            source=pending.source,
            deadline=deadline,
            inputs=(bound, pending.condition, pending.create_metadata),
            token=token,
        )
        active.binding = bound
        active.prepared = prepared
        return prepared

    def _select_gate_setup(
        self,
        plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
        effect_gate: FileEffectGateBinding | None,
        setup: FileEffectGateSetup | None,
    ) -> FileEffectGateSetup | None:
        """Derive ordinary admission only from constructor-bound verified VM facts."""
        guest = self._guest
        target = self._target
        if isinstance(target, OperationScope):
            if effect_gate is not None or setup is not None:
                raise ValidationError("Ordinary host file operations cannot carry a guest effect gate")
            return None
        if effect_gate is not None:
            if setup is not None or (guest is not None and effect_gate.guest != guest):
                raise ValidationError("File gate must match the selected verified guest")
            return None
        if setup is not None:
            if (
                type(setup) is not FileEffectGateSetup
                or (guest is not None and setup.guest != guest)
                or vm_guest_boot_id(setup.guest) != target.boot_id
            ):
                raise ValidationError("File gate setup must match the selected verified guest")
            return setup
        if guest is None:
            return None
        if runtime_selection.target_os is not RuntimeTargetOS.LINUX:
            raise ValidationError("Verified VM file gates require the selected Linux runtime")
        return FileEffectGateSetup.for_target(target, plan.expected.euid, guest)

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

    def _validate_effect_gate(
        self,
        binding: FileDownloadBinding | FileUploadBinding | FileJsonBinding | FileRemoveBinding | FileMetadataBinding,
    ) -> None:
        """Match a caller-supplied gate to this selected managed VM."""
        effect_gate = binding.effect_gate
        if effect_gate is None:
            return
        if (
            not isinstance(self._target, ManagedTargetIdentity)
            or self._target.kind is not ManagedTargetKind.VM
            or binding.runtime_selection.target_os is not RuntimeTargetOS.LINUX
            or effect_gate.euid != binding.identity_plan.expected.euid
            or effect_gate.proposed_generation is not None
            or (binding.bootstrap is not None and effect_gate.guest != binding.bootstrap.guest)
            or (self._guest is not None and effect_gate.guest != self._guest)
            or vm_guest_boot_id(effect_gate.guest) != self._target.boot_id
            or effect_gate.scope_name != self._target.name
            or effect_gate.path
            != file_effect_gate_path(self._target, binding.identity_plan.expected.euid, effect_gate.guest)
        ):
            raise ValidationError("File effect gate must match the selected Linux VM and identity")

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
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> FileJsonOutcome:
        """Run and capture one JSON update under a whole-call borrow."""
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
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
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(FileCallFamily.JSON_UPDATE, prepared.binding, gate_setup=gate_setup)
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
        if gate_setup is not None:
            gate = self._setup_mutation_gate(active, FileCallFamily.JSON_UPDATE, gate_setup, deadline)
            if gate is None:
                borrow.close()
                self._active_json_updates.pop(id(active))
                raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact()
            prepared.binding = replace(prepared.binding, effect_gate=gate)
            prepared.state.binding = prepared.binding
            active.binding = prepared.binding
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
                bootstrap=self._bootstrap,
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
        return self._run_owned(active, FileCallFamily.STAT, self._active_stats, prepared.run)

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
                bootstrap=self._bootstrap,
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
        return self._run_owned(active, FileCallFamily.INVENTORY, self._active_inventories, prepared.run)

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
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> OwnedFileOutcome[FileObjectCandidateResult]:
        """Run and capture one conditional removal under a whole-call borrow."""
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
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
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(FileCallFamily.REMOVE, prepared.binding, gate_setup=gate_setup)
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
        if gate_setup is not None:
            gate = self._setup_mutation_gate(active, FileCallFamily.REMOVE, gate_setup, deadline)
            if gate is None:
                borrow.close()
                self._active_removals.pop(id(active))
                raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact()
            prepared.binding = replace(prepared.binding, effect_gate=gate)
            prepared.state.binding = prepared.binding
            active.binding = prepared.binding
        return self._run_owned(active, FileCallFamily.REMOVE, self._active_removals, prepared.run)

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
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one metadata convergence under one borrow."""
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
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
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(FileCallFamily.SET_METADATA, prepared.binding, gate_setup=gate_setup)
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
        if gate_setup is not None:
            gate = self._setup_mutation_gate(active, FileCallFamily.SET_METADATA, gate_setup, deadline)
            if gate is None:
                borrow.close()
                self._active_metadata.pop(id(active))
                raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact()
            prepared.binding = replace(prepared.binding, effect_gate=gate)
            prepared.state.binding = prepared.binding
            active.binding = prepared.binding
        return self._run_owned(active, FileCallFamily.SET_METADATA, self._active_metadata, prepared.run)

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
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
        _operation: FileMetadataOperation = FileMetadataOperation.ENSURE_DIRECTORY,
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one directory convergence under one borrow."""
        gate_setup = self._select_gate_setup(plan, runtime_selection, effect_gate, gate_setup)
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_metadata(
                carrier,
                operation=_operation,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                trusted_owner=trusted_owner,
                trusted_group=trusted_group,
                mode=mode,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
                bootstrap=self._bootstrap,
                effect_gate=effect_gate,
            )
            self._validate_effect_gate(prepared.binding)
            admission = self._prepare_admission(
                FileCallFamily.ENSURE_DIRECTORY, prepared.binding, gate_setup=gate_setup
            )
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
        if gate_setup is not None:
            gate = self._setup_mutation_gate(active, FileCallFamily.ENSURE_DIRECTORY, gate_setup, deadline)
            if gate is None:
                borrow.close()
                self._active_metadata.pop(id(active))
                raise StateError("File-effect gate setup was not dispatched") from FileGateSetupControlFact()
            prepared.binding = replace(prepared.binding, effect_gate=gate)
            prepared.state.binding = prepared.binding
            active.binding = prepared.binding
        return self._run_owned(active, FileCallFamily.ENSURE_DIRECTORY, self._active_metadata, prepared.run)

    def create_directory(
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
        effect_gate: FileEffectGateBinding | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Create one absent directory through ordinary metadata admission."""
        return self.ensure_directory(
            carrier,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            trusted_owner=trusted_owner,
            trusted_group=trusted_group,
            mode=mode,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            _operation=FileMetadataOperation.CREATE_DIRECTORY,
            effect_gate=effect_gate,
            gate_setup=gate_setup,
        )

    def _setup_mutation_gate(
        self,
        active: _ActiveFileJsonUpdate | _ActiveFileRemove | _ActiveFileMetadata,
        family: FileCallFamily,
        setup: FileEffectGateSetup,
        deadline: Deadline,
    ) -> FileEffectGateBinding | None:
        """Bind setup before mutation, or return None for settled, undispatched setup."""
        with _gate_admission():
            binding = active.binding
            operation = active.prepared.state.operation
            result = exchange_file_effect_gate(
                operation,
                operation=GateControlOperation.SETUP,
                path=setup.path,
                guest=setup.guest,
                scope_name=self._owner.ownership.scope.resource_name,
                plan=binding.identity_plan,
                deadline=deadline,
                runtime_selection=binding.runtime_selection,
                bootstrap=binding.bootstrap,
            )
            normal = operation.settle(result.dispatch, result.carrier_completion)
            if result.dispatch is Dispatch.NOT_SENT and not operation.requires_owner_retention:
                return None
            observed = result.observation
            if (
                not normal
                or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
                or observed is None
                or observed.state is not GateControlObservationState.RESOLVED
                or observed.binding is None
            ):
                raise GateControlMutationUncertain(
                    "file-effect gate setup was not acknowledged"
                ) from FileGateSetupControlFact(result.runtime_prerequisite)
            bound = replace(binding, effect_gate=observed.binding)
            self._validate_effect_gate(bound)
            record = self._obligation(family, bound)
            obligation = active.obligation
            assert obligation is not None
            obligation.publish_payload(
                expected_revision=obligation.payload_revision,
                payload_version=record.payload_version,
                payload=encode_file_call_obligation(record),
            )
            return observed.binding

    def _install[BindingT: _FileCallBinding, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        admission: _FileCallAdmission,
        active_records: dict[int, _ActiveFileCall[BindingT, PreparedT, OutcomeT]],
        *,
        close_borrow_on_refusal: bool = True,
    ) -> None:
        with _gate_admission() if admission.gate_setup else nullcontext():
            try:
                active.obligation = active.borrow.install_dispatch_obligation(
                    admission.obligation_id,
                    "file-call",
                    payload_version=admission.payload_version,
                    payload=admission.payload,
                )
            except BaseException as control:
                if not _is_pre_registration_refusal(control):
                    raise
                if close_borrow_on_refusal:
                    active.borrow.close()
                active_records.pop(id(active))
                raise

    def _prepare_admission(
        self,
        family: FileCallFamily,
        binding: _FileCallBinding,
        *,
        token: bytes | None = None,
        gate_setup: FileEffectGateSetup | None = None,
    ) -> _FileCallAdmission:
        try:
            record = self._obligation(
                family,
                binding,
                token=token,
                gate_setup=gate_setup,
            )
            payload = encode_file_call_admission(record)
        except FileCallObligationCodecError:
            raise ValidationError("File call lifecycle recovery identity is too large or invalid") from None
        return _FileCallAdmission(secrets.token_hex(16), payload, record.payload_version, gate_setup is not None)

    def _publish_json_child(
        self,
        active: _ActiveFileJsonUpdate,
        child: _PreparedUpload,
        attempt: int,
    ) -> None:
        obligation = self._require_obligation(active)
        record = self._obligation(
            FileCallFamily.JSON_UPDATE,
            active.binding,
            token=child.state.token,
            attempt=attempt,
        )
        payload = encode_file_call_obligation(record)
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=record.payload_version,
            payload=payload,
        )

    def _obligation(
        self,
        family: FileCallFamily,
        binding: _FileCallBinding,
        *,
        token: bytes | None = None,
        attempt: int | None = None,
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
            scratch_reference=scratch_reference,
            scratch_cleanup_debt=scratch_cleanup_debt,
            publication_cleanup_debt=publication_cleanup_debt,
            effect_gate=(
                binding.effect_gate
                if (
                    isinstance(
                        binding,
                        (
                            FileDownloadBinding,
                            FileUploadBinding,
                            FileJsonBinding,
                            FileRemoveBinding,
                            FileMetadataBinding,
                        ),
                    )
                )
                else None
            ),
            gate_setup=gate_setup,
            uncertainty=uncertainty,
            bootstrap=binding.bootstrap,
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
            payload_version=recovery.payload_version,
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

    def _run_owned[BindingT: _FileCallBinding, PreparedT, ResultT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OwnedFileOutcome[ResultT]],
        family: FileCallFamily,
        active_records: dict[int, _ActiveFileCall[BindingT, PreparedT, OwnedFileOutcome[ResultT]]],
        run: Callable[[], OwnedFileOutcome[ResultT]],
    ) -> OwnedFileOutcome[ResultT]:
        try:
            outcome = run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, family)
                    active_records.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, family)
        active_records.pop(id(active))
        return outcome

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

    def _capture(
        self, active: _ActiveFileDownload, outcome: FileDownloadOutcome, *, local_call: _LocalDownloadCall | None = None
    ) -> None:
        active.outcome = outcome
        if local_call is not None:
            local_call.active = active
            local_call.remote_requires_owner_retention = outcome.requires_owner_retention
        try:
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
                record = self._obligation(
                    FileCallFamily.DOWNLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.ready._reference if outcome.ready is not None else None,
                    scratch_cleanup_debt=outcome.cleanup_debt,
                    uncertainty=uncertainty,
                )
                obligation = self._require_obligation(active)
                active.retained_payload = _RetainedFilePayload(
                    obligation.payload_revision, record.payload_version, encode_file_call_obligation(record)
                )
            active.capture_ready = True
            self._settle_download_capture(active, local_call=local_call)
        except BaseException:
            self._owner.stop_admission()
            raise

    def _settle_download_capture(self, active: _ActiveFileDownload, *, local_call: _LocalDownloadCall | None) -> None:
        outcome = active.outcome
        assert outcome is not None
        if not active.capture_ready:
            raise StateError("Download bookkeeping capture remains incomplete")
        self._publish_captured_debt(active)
        if outcome.requires_owner_retention:
            self._retain_unfinished(UnfinishedFileDownload(active.carrier, active.binding, outcome))
        if local_call is None and not active.borrow.closed:
            release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_downloads.pop(id(active))

    def _retain_unfinished(self, download: UnfinishedFileDownload) -> None:
        if not any(held.outcome is download.outcome for held in self._unfinished_downloads):
            self._unfinished_downloads.append(download)

    def _capture_upload(self, active: _ActiveFileUpload, outcome: FileUploadOutcome) -> None:
        active.outcome = outcome
        local_call = self._local_file_call
        if local_call is not None and local_call.borrow is active.borrow:
            local_call.upload_outcome = outcome
            local_call.remote_requires_owner_retention = outcome.requires_owner_retention
            local_call.changed = outcome.publication_confirmed
        try:
            if outcome.requires_owner_retention:
                record = self._obligation(
                    FileCallFamily.UPLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.reference,
                    scratch_cleanup_debt=outcome.scratch_cleanup_debt,
                    publication_cleanup_debt=outcome.publication_cleanup_debt,
                    uncertainty=self._upload_uncertainty(outcome),
                )
                obligation = self._require_obligation(active)
                active.retained_payload = _RetainedFilePayload(
                    obligation.payload_revision, record.payload_version, encode_file_call_obligation(record)
                )
            active.capture_ready = True
            self._settle_upload_bookkeeping(active)
        except BaseException:
            self._owner.stop_admission()
            raise

    def settle_upload_bookkeeping(self) -> None:
        """Retry captured debt publication and closure without helper or source replay."""
        try:
            for active in tuple(self._active_uploads.values()):
                if active.outcome is not None:
                    self._settle_upload_bookkeeping(active)
        except BaseException:
            self._owner.stop_admission()
            raise

    def _publish_captured_debt[BindingT, PreparedT, OutcomeT](
        self, active: _ActiveFileCall[BindingT, PreparedT, OutcomeT]
    ) -> None:
        publication = active.retained_payload
        if publication is not None and not active.retained_payload_confirmed:
            obligation = self._require_obligation(active)
            try:
                row = obligation.publish_payload(
                    expected_revision=publication.expected_revision,
                    payload_version=publication.payload_version,
                    payload=publication.payload,
                )
            except Exception:
                # One bounded retry reconciles a lost reply with the same CAS.
                # Control flow leaves the exact intent in custody for teardown.
                row = obligation.publish_payload(
                    expected_revision=publication.expected_revision,
                    payload_version=publication.payload_version,
                    payload=publication.payload,
                )
            if (
                row.payload_revision != publication.expected_revision + 1
                or row.payload_version != publication.payload_version
                or row.payload != publication.payload
            ):
                raise StateError("File debt publication revision is not the intended revision")
            active.retained_payload_confirmed = True

    def _settle_upload_bookkeeping(self, active: _ActiveFileUpload) -> None:
        outcome = active.outcome
        assert outcome is not None
        if not active.capture_ready:
            raise StateError("Upload bookkeeping capture remains incomplete")
        self._publish_captured_debt(active)
        if outcome.requires_owner_retention:
            self._retain_unfinished_upload(UnfinishedFileUpload(active.carrier, active.binding, outcome))
        local_call = self._local_file_call
        if (local_call is None or local_call.borrow is not active.borrow) and not active.borrow.closed:
            release_borrow_after_custody(active.borrow, retain_effect=outcome.requires_owner_retention)
        self._active_uploads.pop(id(active))

    def _retain_unfinished_upload(self, upload: UnfinishedFileUpload) -> None:
        if not any(held.outcome is upload.outcome for held in self._unfinished_uploads):
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
