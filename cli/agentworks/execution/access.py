"""Bound file-operation view over one composition-owned operation."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from agentworks.errors import StateError, ValidationError

from . import _file_memory_read
from ._diagnostic_values import validate_logical_entity_value
from ._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from ._execution_result import check_owned_inline_result, reduce_owned_inline_result
from ._file_local_download import FileLocalDownloadControlFact, FileLocalDownloadOutcome, download_to_local_file
from ._file_operation import FileOperation
from ._file_paths import normalized_relative_path, normalized_root
from ._file_result import (
    _raise_reason,
    reduce_file_inventory,
    reduce_file_metadata,
    reduce_file_remove,
    reduce_file_stat,
)
from ._file_result_transfer import (
    reduce_file_json,
    reduce_file_local_download,
    reduce_file_memory_read,
    reduce_file_upload,
)
from ._helper_launcher import IdentityPlan
from ._json import serialize_json_source, validate_json_object
from ._managed_bound_run import ManagedDeadlineExpired
from ._managed_job_protocol import StreamDisposition, WorkloadWaitFact
from ._managed_job_store import FactName, Stream
from ._managed_observation_exchange import ManagedObservationState
from ._managed_result import _fact
from ._managed_runs import ManagedOutputMode
from ._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from .carrier import Deadline, Dispatch, Failure, Retention
from .diagnostics import ExecutionPhase, check_execution_result
from .files import (
    Change,
    Create,
    DirectoryEntry,
    DirectoryLimit,
    FileFailureReason,
    FileKind,
    FileMetadata,
    FileOperationPhase,
    JsonObject,
    JsonStrategy,
    MutationResult,
    NewMetadata,
    ReadResult,
    Replace,
    Revision,
    UploadSource,
    WriteCondition,
    _file_revision_from_revision,
    _private_file_kind,
    _private_json_strategy,
    _private_write_condition,
)
from .jobs import JobDisposal, JobOutput, JobStatus, JobStop, JobStream
from .models import Command, Input, JobRef, Lifetime, Output, Script
from .profiles import Protection
from .result import ApplicationState, ExecutionFailure, ExecutionOutput, ExecutionResult, ExitCode, Signal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Never

    from ._managed_resource_start import ResourceStartAcknowledgement
    from .carrier import Carrier

_DEFAULT_JSON_MAX_BYTES = 64 * 1_024
_DEFAULT_JSON_MAX_DEPTH = 64
_MAX_UPLOAD_SIZE = (1 << 63) - 1
_MAX_DOWNLOAD_SIZE = (1 << 63) - 1
_DEFAULT_LOCAL_CONDITION = Create()
_DEFAULT_INPUT = Input.eof()
_DEFAULT_OUTPUT = Output.capture()

type _JsonFileStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]


class ExecutionAccess:
    """Private bound execution view over one composition-owned operation."""

    def __init__(
        self,
        operation: ExecutionOperation,
        carrier: Carrier,
        *,
        runtime_selection: RuntimeSelection,
        ordinary_plan: IdentityPlan,
        elevated_plan: IdentityPlan | None,
        entity_kind: str,
        entity_name: str,
        deadline: Callable[[], Deadline],
    ) -> None:
        if type(operation) is not ExecutionOperation:
            raise ValidationError("Execution access requires an acquired execution operation")
        if type(runtime_selection) is not RuntimeSelection:
            raise ValidationError("Execution access requires an explicit runtime selection")
        if type(ordinary_plan) is not IdentityPlan or (
            elevated_plan is not None and type(elevated_plan) is not IdentityPlan
        ):
            raise ValidationError("Execution access requires bound identity plans")
        validate_logical_entity_value(entity_kind, "kind", subject="Execution access")
        validate_logical_entity_value(entity_name, "name", subject="Execution access")
        if not callable(deadline):
            raise ValidationError("Execution access requires a composition-owned deadline policy")
        self._operation = operation
        self._carrier = carrier
        self._runtime_selection = runtime_selection
        self._ordinary_plan = ordinary_plan
        self._elevated_plan = elevated_plan
        self._entity_kind = entity_kind
        self._entity_name = entity_name
        self._deadline = deadline

    def _job_deadline(self, deadline: Deadline | None) -> Deadline:
        self._operation.require_job_binding(self._carrier, self._runtime_selection)
        selected = self._deadline()
        if type(selected) is not Deadline or selected.expires_at is None or selected.expired:
            raise ValidationError("Managed access requires a live finite composition deadline")
        if deadline is not None:
            if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
                raise ValidationError("Managed access requires a live finite deadline")
            if deadline.expires_at < selected.expires_at:
                selected = deadline
        return selected

    @staticmethod
    def _raise_job_control(control: BaseException, job: JobRef) -> Never:
        if type(job) is not JobRef:
            raise control
        fact = ManagedExecutionControlFact(job)
        fact.__cause__ = control.__cause__
        raise control from fact

    def observe(self, job: JobRef, *, deadline: Deadline | None = None) -> JobStatus:
        """Read application status and positive workload closure without control."""
        selected = self._job_deadline(deadline)
        try:
            outcome = self._operation.observe_job(job, self._carrier, selected)
        except BaseException as control:
            self._raise_job_control(control, job)
        candidate = outcome.candidate
        observation = None if candidate is None else candidate.observation
        facts = (
            dict(observation.facts)
            if observation is not None and observation.state is ManagedObservationState.OBSERVED
            else {}
        )
        wait = _fact(facts.get(FactName.WAIT), WorkloadWaitFact)
        status = None
        if isinstance(wait, WorkloadWaitFact):
            status = ExitCode(wait.exit_code) if wait.exit_code is not None else Signal(wait.signal)  # type: ignore[arg-type]
        expired = selected.expired or (candidate is not None and candidate.carrier_failure is Failure.DEADLINE)
        settled = not outcome.requires_owner_retention
        valid = observation is not None and observation.state is ManagedObservationState.OBSERVED and settled
        return JobStatus(
            job,
            ApplicationState.COMPLETED if status is not None else ApplicationState.UNKNOWN,
            status,
            ExecutionFailure.DEADLINE
            if expired
            else None
            if valid and status is not None
            else ExecutionFailure.OBSERVATION,
            valid and outcome.terminal_proved,
            expired,
        )

    def read_output(
        self,
        job: JobRef,
        *,
        stream: JobStream,
        cursor: int = 0,
        max_bytes: int = 4096,
        deadline: Deadline | None = None,
    ) -> JobOutput:
        """Slice only verified immutable closed output using a byte offset."""
        if type(stream) is not JobStream or type(cursor) is not int or cursor < 0:
            raise ValidationError("Managed output requires a stream and nonnegative byte cursor")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValidationError("Managed output requires a positive byte bound")
        selected = self._job_deadline(deadline)
        try:
            outcome = self._operation.read_job(job, self._carrier, selected, Stream(stream.value))
        except BaseException as control:
            self._raise_job_control(control, job)
        candidate = outcome.attempt.candidate
        expired = selected.expired or (candidate is not None and candidate.carrier_failure is Failure.DEADLINE)
        if not outcome.accepted or outcome.attempt.requires_owner_retention:
            return JobOutput(
                job,
                stream,
                cursor=cursor,
                next_cursor=cursor,
                failure=ExecutionFailure.DEADLINE if expired else ExecutionFailure.OBSERVATION,
                deadline_exceeded=expired,
            )
        data = outcome.output or b""
        if cursor > len(data):
            raise ValidationError("Managed output cursor exceeds the verified retained prefix")
        chunk = data[cursor : cursor + max_bytes]
        disposition = outcome.disposition
        retention = (
            Retention.DISCARDED
            if disposition is StreamDisposition.DISCARDED
            else Retention.SUPPRESSED
            if disposition is StreamDisposition.SUPPRESSED
            else Retention.CAPTURED
        )
        capture_complete = disposition is StreamDisposition.COMPLETE_CAPTURE
        return JobOutput(
            job,
            stream,
            chunk,
            cursor,
            cursor + len(chunk),
            cursor + len(chunk) == len(data),
            capture_complete,
            retention,
            ExecutionFailure.DEADLINE
            if expired
            else ExecutionFailure.OUTPUT_LIMIT
            if disposition is StreamDisposition.TRUNCATED_CAPTURE
            else None,
            expired,
        )

    def wait(self, job: JobRef, *, deadline: Deadline | None = None, check: bool = False) -> ExecutionResult:
        """Wait within an observation budget without stopping or extending the job."""
        if type(check) is not bool:
            raise ValidationError("Managed wait check must be boolean")
        selected = self._job_deadline(deadline)
        return self._wait_managed(job, selected, check=check)

    def _wait_managed(
        self,
        job: JobRef,
        selected: Deadline,
        *,
        check: bool,
        acknowledgement: ResourceStartAcknowledgement | None = None,
    ) -> ExecutionResult:
        """Compose managed observation under an already selected finite budget."""
        try:
            outcome = self._operation.wait_job(job, self._carrier, selected)
        except ManagedDeadlineExpired as control:
            if acknowledgement is None or acknowledgement.reference != job or control.__cause__ is not None:
                self._raise_job_control(control, job)
            retention = {
                ManagedOutputMode.CAPTURE: Retention.CAPTURED,
                ManagedOutputMode.DISCARD: Retention.DISCARDED,
                ManagedOutputMode.SENSITIVITY_SUPPRESSED: Retention.SUPPRESSED,
            }[acknowledgement.output_policy.mode]
            missing = ExecutionOutput(retention=retention)
            result = ExecutionResult(
                Dispatch.UNKNOWN,
                ApplicationState.UNKNOWN,
                stdout=missing,
                stderr=missing,
                failure=ExecutionFailure.DEADLINE,
                deadline_exceeded=True,
                job=job,
            )
        except BaseException as control:
            self._raise_job_control(control, job)
        else:
            result = replace(outcome.result, job=job)
        if not check:
            return result
        return check_execution_result(
            result,
            entity_kind=self._entity_kind,
            entity_name=self._entity_name,
            phase=ExecutionPhase.OBSERVATION
            if result.failure in {ExecutionFailure.DEADLINE, ExecutionFailure.OBSERVATION}
            else None,
        )

    def stop(self, job: JobRef, *, deadline: Deadline | None = None) -> JobStop:
        """Request permanent stop and report separate positive workload closure."""
        selected = self._job_deadline(deadline)
        try:
            return self._operation.stop_job(job, selected)
        except BaseException as control:
            self._raise_job_control(control, job)

    def dispose(self, job: JobRef, *, deadline: Deadline | None = None) -> JobDisposal:
        """Dispose only positively terminal artifacts; active work keeps its keeper."""
        selected = self._job_deadline(deadline)
        try:
            return self._operation.dispose_job(job, self._carrier, selected)
        except BaseException as control:
            self._raise_job_control(control, job)

    def start(
        self,
        request: Command | Script,
        *,
        profile: Protection,
        lifetime: Lifetime,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: Input = _DEFAULT_INPUT,
        output: Output = _DEFAULT_OUTPUT,
        sensitive: bool = False,
        deadline: Deadline | None = None,
    ) -> JobRef:
        """Start one exact managed job under operation or resource custody."""
        if profile is not Protection.MANAGED or type(lifetime) is not Lifetime:
            raise ValidationError("Managed start requires explicit MANAGED and lifetime choices")
        if self._runtime_selection.target_os is not RuntimeTargetOS.LINUX:
            raise StateError("Managed execution is unavailable on this runtime")
        if type(request) not in {Command, Script} or type(stdin) is not Input or type(output) is not Output:
            raise ValidationError("Managed start requires finite invocation, input and output values")
        if type(sudo) is not bool or type(sensitive) is not bool:
            raise ValidationError("Managed execution flags must be booleans")
        if sudo and self._elevated_plan is None:
            raise StateError("Execution elevation is unavailable for this bound access")
        selected = self._deadline()
        if type(selected) is not Deadline or selected.expires_at is None or selected.expired:
            raise ValidationError("Managed start requires a live finite composition deadline")
        if deadline is not None:
            if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
                raise ValidationError("Managed start requires a live finite deadline")
            if deadline.expires_at < selected.expires_at:
                selected = deadline
        plan = self._elevated_plan if sudo else self._ordinary_plan
        assert plan is not None
        if lifetime is Lifetime.INDEPENDENT:
            return self._operation.start_resource_managed(
                self._carrier,
                request,
                plan=plan,
                runtime_selection=self._runtime_selection,
                deadline=selected,
                input=stdin,
                output=output,
                env=env,
                cwd=cwd,
                sensitive=sensitive or stdin.is_sensitive,
            ).reference
        return self._operation.start_managed(
            self._carrier,
            request,
            plan=plan,
            runtime_selection=self._runtime_selection,
            deadline=selected,
            input=stdin,
            output=output,
            env=env,
            cwd=cwd,
            sensitive=sensitive or stdin.is_sensitive,
        )

    def run(
        self,
        request: Command | Script,
        *,
        profile: Protection,
        lifetime: Lifetime = Lifetime.OPERATION,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: Input = _DEFAULT_INPUT,
        output: Output = _DEFAULT_OUTPUT,
        sensitive: bool = False,
        deadline: Deadline | None = None,
        check: bool = False,
    ) -> ExecutionResult:
        """Run DIRECT inline or launch then observe exact MANAGED work."""
        if type(profile) is not Protection or type(lifetime) is not Lifetime:
            raise ValidationError("Foreground execution requires explicit profile and lifetime values")
        if lifetime is Lifetime.INDEPENDENT and profile is Protection.DIRECT:
            raise StateError("INDEPENDENT execution lifetime is unavailable")
        if self._runtime_selection.target_os is not RuntimeTargetOS.LINUX:
            raise StateError("Foreground execution is unavailable on this runtime")
        if type(request) not in {Command, Script}:
            raise ValidationError("Foreground execution requires a command or script")
        if type(stdin) is not Input or type(output) is not Output:
            raise ValidationError("Foreground execution requires finite input and bounded output")
        if type(sudo) is not bool or type(sensitive) is not bool or type(check) is not bool:
            raise ValidationError("Foreground execution flags must be booleans")
        if sudo and self._elevated_plan is None:
            raise StateError("Execution elevation is unavailable for this bound access")
        composition_deadline = self._deadline()
        if type(composition_deadline) is not Deadline or composition_deadline.expired:
            raise ValidationError("Foreground execution requires a live deadline")
        if deadline is not None and (type(deadline) is not Deadline or deadline.expired):
            raise ValidationError("Foreground execution requires a live deadline")
        selected_deadline = composition_deadline
        if deadline is not None and (
            composition_deadline.expires_at is None
            or (deadline.expires_at is not None and deadline.expires_at < composition_deadline.expires_at)
        ):
            selected_deadline = deadline
        if profile is Protection.MANAGED and selected_deadline.expires_at is None:
            raise ValidationError("Managed execution requires a finite deadline")
        plan = self._elevated_plan if sudo else self._ordinary_plan
        assert plan is not None
        effective_sensitive = sensitive or stdin.is_sensitive

        if profile is Protection.MANAGED:
            if lifetime is Lifetime.INDEPENDENT:
                acknowledgement = self._operation.start_resource_managed(
                    self._carrier,
                    request,
                    plan=plan,
                    runtime_selection=self._runtime_selection,
                    deadline=selected_deadline,
                    input=stdin,
                    output=output,
                    env=env,
                    cwd=cwd,
                    sensitive=effective_sensitive,
                )
                return self._wait_managed(
                    acknowledgement.reference,
                    selected_deadline,
                    check=check,
                    acknowledgement=acknowledgement,
                )
            job = self._operation.start_managed(
                self._carrier,
                request,
                plan=plan,
                runtime_selection=self._runtime_selection,
                deadline=selected_deadline,
                input=stdin,
                output=output,
                env=env,
                cwd=cwd,
                sensitive=effective_sensitive,
            )
            return self._wait_managed(job, selected_deadline, check=check)

        outcome = self._operation.run_inline(
            self._carrier,
            request,
            plan=plan,
            deadline=selected_deadline,
            runtime_selection=self._runtime_selection,
            stdin=stdin.data,
            env=env,
            cwd=cwd,
            capture_limit=output.max_bytes,
            sensitive=effective_sensitive,
        )
        if check:
            return check_owned_inline_result(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)
        return reduce_owned_inline_result(outcome)


class _BytesSource:
    """A private exact finite source for byte-value publication."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, limit: int, /) -> bytes:
        if self._offset == len(self._data):
            return b""
        end = min(self._offset + limit, len(self._data))
        result = self._data[self._offset : end]
        self._offset = end
        return result


class _UploadSourceAdapter:
    """Adapt the public source shape to the private upload carrier shape."""

    def __init__(self, source: UploadSource) -> None:
        self._source = source

    def try_read(self, limit: int) -> bytes | None:
        return self._source.read(limit)


class FileAccess:
    """One non-production, bound view of the supplied file-operation custody."""

    def __init__(
        self,
        operation: FileOperation,
        carrier: Carrier,
        *,
        trusted_root: PurePosixPath,
        runtime_selection: RuntimeSelection,
        ordinary_plan: IdentityPlan,
        elevated_plan: IdentityPlan | None,
        entity_kind: str,
        entity_name: str,
        deadline: Callable[[], Deadline],
    ) -> None:
        if type(operation) is not FileOperation:
            raise ValidationError("File access requires an acquired file operation")
        if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
            raise ValidationError("File access requires one trusted normalized root")
        if type(runtime_selection) is not RuntimeSelection:
            raise ValidationError("File access requires an explicit runtime selection")
        if type(ordinary_plan) is not IdentityPlan or (
            elevated_plan is not None and type(elevated_plan) is not IdentityPlan
        ):
            raise ValidationError("File access requires bound identity plans")
        validate_logical_entity_value(entity_kind, "kind", subject="File access")
        validate_logical_entity_value(entity_name, "name", subject="File access")
        if not callable(deadline):
            raise ValidationError("File access requires a composition-owned deadline policy")

        self._operation = operation
        self._carrier = carrier
        self._trusted_root = trusted_root
        self._runtime_selection = runtime_selection
        self._ordinary_plan = ordinary_plan
        self._elevated_plan = elevated_plan
        self._entity_kind = entity_kind
        self._entity_name = entity_name
        self._deadline = deadline

    def read_file(self, path: PurePosixPath, *, max_bytes: int, sudo: bool = False) -> ReadResult | None:
        """Read one bounded regular-file snapshot."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValidationError("File read requires a positive byte bound")
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = _file_memory_read.read_file(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
            operation=self._operation,
        )
        return reduce_file_memory_read(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def stat(self, path: PurePosixPath, *, sudo: bool = False) -> FileMetadata | None:
        """Observe one supported filesystem object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = self._operation.stat(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_stat(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def list_directory(
        self,
        path: PurePosixPath,
        *,
        limit: DirectoryLimit,
        sudo: bool = False,
    ) -> tuple[DirectoryEntry, ...]:
        """Inventory one directory within the caller's explicit limits."""
        if type(limit) is not DirectoryLimit:
            raise ValidationError("File inventory requires exact directory limits")
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = self._operation.list_directory(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            max_entries=limit.max_entries,
            max_depth=limit.max_depth,
            max_encoded_bytes=limit.max_encoded_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_inventory(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def write_file(
        self,
        path: PurePosixPath,
        data: bytes,
        *,
        condition: WriteCondition,
        create_metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Publish one exact finite byte value under an explicit condition."""
        if type(data) is not bytes or type(create_metadata) is not NewMetadata:
            raise ValidationError("File publication requires exact bytes and creation metadata")
        return self.upload(
            path,
            _BytesSource(data),
            size=len(data),
            condition=condition,
            create_metadata=create_metadata,
            sudo=sudo,
        )

    def upload(
        self,
        path: PurePosixPath,
        source: UploadSource,
        *,
        size: int,
        condition: WriteCondition,
        create_metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Publish one exact finite value from a caller-owned source."""
        root, leaf, plan, deadline = self._request(path, sudo)
        source = _validate_upload_source(source)
        if type(size) is not int or not 0 <= size <= _MAX_UPLOAD_SIZE:
            raise ValidationError("File upload size must be a nonnegative bounded integer")
        if type(create_metadata) is not NewMetadata:
            raise ValidationError("File upload requires exact creation metadata")
        private_condition = _private_write_condition(condition)
        outcome = self._operation.upload(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            source=_UploadSourceAdapter(source),
            size=size,
            condition=private_condition,
            create_metadata=create_metadata,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_upload(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def download(
        self,
        path: PurePosixPath,
        destination: Path,
        *,
        local_condition: Create | Replace = _DEFAULT_LOCAL_CONDITION,
        max_bytes: int | None = None,
        sudo: bool = False,
    ) -> FileMetadata:
        """Publish one verified remote snapshot to a local regular file."""
        if not isinstance(destination, Path):
            raise ValidationError("File download requires a concrete local path")
        if type(local_condition) not in (Create, Replace):
            raise ValidationError("File download requires Create or Replace")
        if max_bytes is not None and (type(max_bytes) is not int or not 0 < max_bytes <= _MAX_DOWNLOAD_SIZE):
            raise ValidationError("File download requires a positive representable byte bound")
        root, leaf, plan, deadline = self._request(path, sudo)
        if deadline.expired:
            return reduce_file_local_download(
                FileLocalDownloadOutcome(None, deadline_exceeded=True),
                entity_kind=self._entity_kind,
                entity_name=self._entity_name,
            )
        call = self._operation.begin_local_download()
        outcome: FileLocalDownloadOutcome | None = None
        try:
            stage = self._operation.retained_local_download_stage
            cleaned = True
            if stage is not None:
                try:
                    cleaned = self._operation.retry_local_download_cleanup(call)
                except BaseException as control:
                    raise control from FileLocalDownloadControlFact(self._retained_local_outcome())
            if not cleaned:
                outcome = self._retained_local_outcome()
                result = reduce_file_local_download(
                    outcome,
                    entity_kind=self._entity_kind,
                    entity_name=self._entity_name,
                )
            else:
                try:
                    outcome = download_to_local_file(
                        self._carrier,
                        trusted_root_path=root,
                        relative_path=leaf,
                        destination=destination,
                        max_bytes=_MAX_DOWNLOAD_SIZE if max_bytes is None else max_bytes,
                        plan=plan,
                        deadline=deadline,
                        runtime_selection=self._runtime_selection,
                        operation=self._operation,
                        condition=local_condition,
                        local_call=call,
                    )
                except BaseException as control:
                    fact = control.__cause__
                    if not isinstance(fact, FileLocalDownloadControlFact):
                        raise
                    outcome = fact.outcome
                    if not isinstance(control, Exception):
                        raise
                    result = reduce_file_local_download(
                        outcome,
                        entity_kind=self._entity_kind,
                        entity_name=self._entity_name,
                        failure=control,
                    )
                else:
                    result = reduce_file_local_download(
                        outcome, entity_kind=self._entity_kind, entity_name=self._entity_name
                    )
        except BaseException:
            # Core keeps the call and its borrow on failed finalization.
            with suppress(BaseException):
                self._operation.finish_local_download(call)
            raise
        try:
            self._operation.finish_local_download(call)
        except Exception:
            _raise_reason(
                FileOperationPhase.CLEANUP,
                FileFailureReason.COORDINATION,
                entity_kind=self._entity_kind,
                entity_name=self._entity_name,
                effect=Change.CHANGED if outcome is not None and outcome.published else None,
            )
        except BaseException as control:
            raise control from FileLocalDownloadControlFact(outcome or FileLocalDownloadOutcome(None))
        return result

    def _retained_local_outcome(self) -> FileLocalDownloadOutcome:
        stage = self._operation.retained_local_download_stage
        assert stage is not None
        return FileLocalDownloadOutcome(
            None,
            published=stage.published,
            publication_uncertain=stage.publication_uncertain,
            cleanup_uncertain=stage.cleanup_uncertain,
            cleanup_failed=True,
            unfinished_stage=stage,
        )

    def update_json(
        self,
        path: PurePosixPath,
        document: JsonObject,
        *,
        strategy: JsonStrategy,
        create: bool,
        create_metadata: NewMetadata,
        max_bytes: int = _DEFAULT_JSON_MAX_BYTES,
        max_depth: int = _DEFAULT_JSON_MAX_DEPTH,
        sudo: bool = False,
    ) -> MutationResult:
        """Validate and apply one bounded JSON-object update."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(document) is not dict or type(create) is not bool or type(create_metadata) is not NewMetadata:
            raise ValidationError("JSON update requires an object, creation choice, and creation metadata")
        source = _json_source(document, max_bytes=max_bytes, max_depth=max_depth)
        outcome = self._operation.update_json(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            source=source,
            strategy=cast("_JsonFileStrategy", _private_json_strategy(strategy)),
            create=create,
            create_metadata=create_metadata,
            max_bytes=max_bytes,
            max_depth=max_depth,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_json(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def ensure_directory(
        self,
        path: PurePosixPath,
        *,
        metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Create or converge exactly one directory and its metadata."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(metadata) is not NewMetadata:
            raise ValidationError("Directory creation requires exact metadata")
        outcome = self._operation.ensure_directory(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            trusted_owner=metadata.owner,
            trusted_group=metadata.group,
            mode=metadata.mode,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_metadata(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def set_metadata(
        self,
        path: PurePosixPath,
        *,
        owner: str,
        group: str,
        mode: int,
        sudo: bool = False,
    ) -> MutationResult:
        """Converge ownership and mode for one existing supported object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        metadata = NewMetadata(owner, group, mode)
        outcome = self._operation.set_metadata(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            trusted_owner=metadata.owner,
            trusted_group=metadata.group,
            mode=metadata.mode,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_metadata(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def remove(
        self,
        path: PurePosixPath,
        *,
        expected_kind: FileKind,
        expected: Revision,
        sudo: bool = False,
    ) -> MutationResult:
        """Conditionally remove one observed supported object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(expected) is not Revision:
            raise ValidationError("File removal requires an exact revision")
        outcome = self._operation.remove(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            expected_kind=_private_file_kind(expected_kind),
            expected_revision=_file_revision_from_revision(expected),
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_remove(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def _request(self, path: PurePosixPath, sudo: bool) -> tuple[str, str, IdentityPlan, Deadline]:
        plan = self._select_plan(sudo)
        root, leaf = self._decompose(path)
        deadline = self._deadline()
        if type(deadline) is not Deadline:
            raise ValidationError("File access deadline policy must return one deadline")
        return root, leaf, plan, deadline

    def _select_plan(self, sudo: bool) -> IdentityPlan:
        if type(sudo) is not bool:
            raise ValidationError("File operations require an explicit elevation choice")
        if not sudo:
            return self._ordinary_plan
        if self._elevated_plan is None:
            raise StateError("File elevation is unavailable for this bound access")
        return self._elevated_plan

    def _decompose(self, path: PurePosixPath) -> tuple[str, str]:
        if type(path) is not PurePosixPath or not normalized_root(str(path)):
            raise ValidationError("File operation requires one normalized absolute path")
        if path == PurePosixPath("/"):
            raise ValidationError("Filesystem root is not a supported file target")
        if path == self._trusted_root:
            parent = path.parent
            leaf = path.name
        else:
            try:
                relative = path.relative_to(self._trusted_root)
            except ValueError:
                raise ValidationError("File target is outside the trusted root") from None
            parent = self._trusted_root
            leaf = str(relative)
        if not normalized_root(str(parent)) or not normalized_relative_path(leaf):
            raise ValidationError("File operation requires one confined target")
        return str(parent), leaf


def _validate_upload_source(source: object) -> UploadSource:
    try:
        reader = getattr(source, "read", None)
    except Exception:
        raise ValidationError("File upload requires a nonblocking byte source") from None
    if not callable(reader):
        raise ValidationError("File upload requires a nonblocking byte source")
    return cast("UploadSource", source)


def _json_source(document: JsonObject, *, max_bytes: int, max_depth: int) -> bytes:
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return serialize_json_source(validate_json_object(encoded, max_bytes=max_bytes, max_depth=max_depth))
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ValidationError("JSON update requires a bounded valid JSON object") from None
