"""Bound FileAccess checks over the real fixed local helpers."""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.access import FileAccess
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.execution.files import (
    Change,
    Create,
    DirectoryLimit,
    FileKind,
    JsonStrategy,
    Match,
    NewMetadata,
    Replace,
)
from agentworks.operations import OperationOwner
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._file_snapshot_support import install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")


class ReentrantCarrier:
    def __init__(self) -> None:
        self._inner = LocalCarrier()
        self.calls = 0
        self.callback: Callable[[], object] | None = None

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        if self.calls == 1:
            assert self.callback is not None
            with pytest.raises(StateError):
                self.callback()
        return self._inner.execute(invocation, io=io, deadline=deadline)


class UnsettledCarrier:
    def __init__(self) -> None:
        self._inner = LocalCarrier()

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        report = self._inner.execute(invocation, io=io, deadline=deadline)
        return replace(report, completion=ExitStatus(code=1))


class RecordingCarrier:
    def __init__(self) -> None:
        self._inner = LocalCarrier()
        self.deadlines: list[Deadline] = []

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.deadlines.append(deadline)
        return self._inner.execute(invocation, io=io, deadline=deadline)


class NoDispatchCarrier:
    def __init__(self) -> None:
        self.invocations: list[PreparedInvocation] = []

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del io, deadline
        self.invocations.append(invocation)
        return CarrierReport(dispatch=Dispatch.NOT_SENT)


class FirstDispatchCarrier(NoDispatchCarrier):
    def __init__(self) -> None:
        super().__init__()
        self._inner = LocalCarrier()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.invocations.append(invocation)
        if len(self.invocations) == 1:
            return self._inner.execute(invocation, io=io, deadline=deadline)
        return CarrierReport(dispatch=Dispatch.NOT_SENT)


class RecordingUploadSource:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.calls = 0
        self.limits: list[int] = []
        self.closed = False

    def read(self, maximum: int, /) -> bytes:
        self.calls += 1
        self.limits.append(maximum)
        if self._offset == len(self._data):
            return b""
        end = min(self._offset + maximum, len(self._data))
        data = self._data[self._offset : end]
        self._offset = end
        return data

    def close(self) -> None:
        self.closed = True


class FailingUploadSource:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0
        self.closed = False

    def read(self, maximum: int, /) -> bytes:
        del maximum
        self.calls += 1
        raise self.failure

    def close(self) -> None:
        self.closed = True


class StallingUploadSource:
    def __init__(self, value: object = None) -> None:
        self.value = value
        self.calls = 0
        self.closed = False

    def read(self, maximum: int, /) -> object:
        del maximum
        self.calls += 1
        return self.value

    def close(self) -> None:
        self.closed = True


class TemporaryThenDataSource:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def read(self, maximum: int, /) -> bytes | None:
        del maximum
        self.calls += 1
        if self.calls == 1:
            return None
        return b"x" if self.calls == 2 else b""

    def close(self) -> None:
        self.closed = True


class ExcessUploadSource:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def read(self, maximum: int, /) -> bytes:
        self.calls += 1
        return b"x" * (maximum + 1)

    def close(self) -> None:
        self.closed = True


class InvalidUploadSource:
    read = "not-callable"


class DescriptorUploadSource:
    def __init__(self) -> None:
        self.inspections = 0

    @property
    def read(self) -> object:
        self.inspections += 1
        raise AssertionError("upload source read descriptor must not be inspected")


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture
def metadata() -> NewMetadata:
    import grp
    import pwd

    return NewMetadata(pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name, 0o640)


@pytest.fixture
def bound_access(tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "approved"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-vm"),
        "file-access",
    )
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        LocalCarrier(),
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    try:
        yield access, root, owner, database
    finally:
        database.close()


def test_real_bound_methods_preserve_exact_public_values(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], metadata: NewMetadata
) -> None:
    access, root, owner, _database = bound_access
    target = PurePosixPath(root / "target")

    created = access.write_file(target, b"first", condition=Create(), create_metadata=metadata)
    assert created.change is Change.CHANGED and created.revision is not None
    observed = access.stat(target)
    assert observed is not None and observed.kind is FileKind.REGULAR
    assert access.read_file(target, max_bytes=16).data == b"first"  # type: ignore[union-attr]

    matched = access.write_file(target, b"second", condition=Match(observed.revision), create_metadata=metadata)
    assert matched.change is Change.CHANGED and matched.revision is not None
    replaced = access.write_file(target, b"third", condition=Replace(), create_metadata=metadata)
    assert replaced.change is Change.CHANGED
    assert access.read_file(target, max_bytes=16).data == b"third"  # type: ignore[union-attr]

    directory = access.ensure_directory(PurePosixPath(root / "directory"), metadata=metadata)
    assert directory.change is Change.CHANGED
    listed = access.list_directory(PurePosixPath(root), limit=DirectoryLimit(max_entries=8))
    assert {entry.relative_path for entry in listed} == {PurePosixPath("directory"), PurePosixPath("target")}
    converged = access.set_metadata(target, owner=metadata.owner, group=metadata.group, mode=0o600)
    assert converged.change is Change.CHANGED

    json_target = PurePosixPath(root / "settings.json")
    updated = access.update_json(
        json_target,
        {"enabled": True},
        strategy=JsonStrategy.REPLACE,
        create=True,
        create_metadata=metadata,
    )
    assert updated.change is Change.CHANGED
    current = access.stat(target)
    assert current is not None
    removed = access.remove(target, expected_kind=FileKind.REGULAR, expected=current.revision)
    assert removed.change is Change.CHANGED and removed.revision is None
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_upload_uses_bounded_caller_owned_source_and_exact_eof_probe(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], metadata: NewMetadata
) -> None:
    access, root, owner, _database = bound_access
    source = RecordingUploadSource(b"streamed")

    result = access.upload(
        PurePosixPath(root / "streamed"),
        source,
        size=8,
        condition=Create(),
        create_metadata=metadata,
    )

    assert result.change is Change.CHANGED
    assert access.read_file(PurePosixPath(root / "streamed"), max_bytes=32).data == b"streamed"  # type: ignore[union-attr]
    assert source.calls >= 2
    assert source.limits[-1] == 1
    assert all(0 < limit <= 12 * 1_024 for limit in source.limits)
    assert not source.closed
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("source", "size", "expected_reason"),
    [
        (RecordingUploadSource(b"short"), 6, "source_contract"),
        (StallingUploadSource("wrong-type"), 1, "source_contract"),
        (ExcessUploadSource(), 1, "source_contract"),
        (FailingUploadSource(RuntimeError("source-secret")), 1, "source"),
    ],
    ids=["early-eof", "nonbytes", "excess", "source-exception"],
)
def test_upload_reduces_source_failures_without_closing_source(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
    metadata: NewMetadata,
    source: RecordingUploadSource | StallingUploadSource | ExcessUploadSource | FailingUploadSource,
    size: int,
    expected_reason: str,
) -> None:
    access, root, owner, _database = bound_access

    with pytest.raises(ExternalError) as raised:
        access.upload(
            PurePosixPath(root / "failed"),
            source,  # type: ignore[arg-type]
            size=size,
            condition=Create(),
            create_metadata=metadata,
        )

    assert raised.value.details is not None
    assert raised.value.details.reason.value == expected_reason
    assert not source.closed
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_upload_deadline_rejection_precedes_source_read_and_does_not_close_source(
    tmp_path: Path, plan: IdentityPlan, metadata: NewMetadata
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-upload-deadline"),
        "file-access",
    )
    source = StallingUploadSource()
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        LocalCarrier(),
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(0),
    )
    try:
        with pytest.raises(ExternalError) as raised:
            access.upload(
                PurePosixPath(root / "stalled"),
                source,
                size=1,
                condition=Create(),
                create_metadata=metadata,
            )
        assert raised.value.details is not None
        assert raised.value.details.reason.value == "deadline"
        assert source.calls == 0
        assert not source.closed
    finally:
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
        database.close()


def test_upload_retries_temporary_none_without_closing_source(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], metadata: NewMetadata
) -> None:
    access, root, owner, _database = bound_access
    source = TemporaryThenDataSource()

    result = access.upload(
        PurePosixPath(root / "temporary"),
        source,
        size=1,
        condition=Create(),
        create_metadata=metadata,
    )

    assert result.change is Change.CHANGED
    assert source.calls >= 3
    assert not source.closed
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_upload_validation_rejects_before_source_read_or_dispatch(
    tmp_path: Path, plan: IdentityPlan, metadata: NewMetadata
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-upload-validation"),
        "file-access",
    )
    carrier = NoDispatchCarrier()
    source = RecordingUploadSource(b"secret")
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    try:
        with pytest.raises(ValidationError):
            access.upload(
                PurePosixPath(root / "../outside"),
                source,
                size=6,
                condition=Create(),
                create_metadata=metadata,
            )
        assert source.calls == 0
        assert carrier.invocations == []

        with pytest.raises(ValidationError):
            access.upload(
                PurePosixPath(root / "target"),
                InvalidUploadSource(),  # type: ignore[arg-type]
                size=6,
                condition=Create(),
                create_metadata=metadata,
            )
        assert source.calls == 0
        assert carrier.invocations == []
    finally:
        owner.close()
        database.close()


def test_upload_sudo_selects_bound_elevated_plan_before_dispatch(
    tmp_path: Path, plan: IdentityPlan, metadata: NewMetadata
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-upload-plan"),
        "file-access",
    )
    carrier = FirstDispatchCarrier()
    elevated = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=elevated,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    source = RecordingUploadSource(b"x")
    try:
        with pytest.raises(ExternalError):
            access.upload(
                PurePosixPath(root / "target"),
                source,
                size=1,
                condition=Create(),
                create_metadata=metadata,
                sudo=True,
            )
        assert any(invocation.argv[0] == "/usr/bin/sudo" for invocation in carrier.invocations)
        assert source.calls == 0
        assert not source.closed
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_upload_does_not_close_source_when_keyboard_interrupt_escapes(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], metadata: NewMetadata
) -> None:
    access, root, owner, _database = bound_access
    source = FailingUploadSource(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        access.upload(
            PurePosixPath(root / "interrupted"),
            source,
            size=1,
            condition=Create(),
            create_metadata=metadata,
        )

    assert source.calls > 0
    assert not source.closed
    with pytest.raises(StateError):
        owner.close()


def test_paths_are_exactly_confined_and_root_is_refused(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
) -> None:
    access, root, owner, _database = bound_access
    assert access.list_directory(PurePosixPath(root), limit=DirectoryLimit()) == ()

    invalid = (
        PurePosixPath("relative"),
        PurePosixPath(str(root) + "/../outside"),
        PurePosixPath("/"),
    )
    for path in invalid:
        with pytest.raises(ValidationError):
            access.stat(path)
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_lower_layer_object_refusals_and_diagnostics_remain_safe(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
) -> None:
    access, root, owner, _database = bound_access
    regular = root / "regular"
    regular.write_bytes(b"content")
    link = root / "link"
    link.symlink_to(regular)
    hard_link = root / "hard-link"
    os.link(regular, hard_link)
    fifo = root / "fifo"
    os.mkfifo(fifo)
    socket_path = root / "socket"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    try:
        for path in (link, hard_link, fifo):
            with pytest.raises(StateError) as raised:
                access.stat(PurePosixPath(path))
            rendered = repr(raised.value) + str(raised.value)
            assert str(path) not in rendered
            assert str(root) not in rendered
        socket_metadata = access.stat(PurePosixPath(socket_path))
        assert socket_metadata is not None and socket_metadata.kind is FileKind.SOCKET
        with pytest.raises(StateError):
            access.read_file(PurePosixPath(socket_path), max_bytes=32)
    finally:
        listener.close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_unavailable_elevation_refuses_before_validation_or_dispatch(
    tmp_path: Path, plan: IdentityPlan, metadata: NewMetadata
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-elevation"),
        "file-access",
    )
    carrier = NoDispatchCarrier()
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=None,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    try:
        with pytest.raises(ValidationError):
            access.update_json(
                PurePosixPath(root / "target"),
                {"invalid": float("nan")},
                strategy=JsonStrategy.REPLACE,
                create=True,
                create_metadata=metadata,
            )
        assert carrier.invocations == []
        with pytest.raises(StateError):
            access.update_json(
                PurePosixPath(root / "target"),
                {"invalid": float("nan")},
                strategy=JsonStrategy.REPLACE,
                create=True,
                create_metadata=metadata,
                sudo=True,
            )
        assert carrier.invocations == []

        source = DescriptorUploadSource()
        with pytest.raises(StateError):
            access.upload(
                PurePosixPath(root / "target"),
                source,  # type: ignore[arg-type]
                size=1,
                condition=Create(),
                create_metadata=metadata,
                sudo=True,
            )
        assert source.inspections == 0
        assert carrier.invocations == []
        owner.close()
    finally:
        database.close()


def test_json_workflow_reuses_one_bound_deadline(tmp_path: Path, plan: IdentityPlan, metadata: NewMetadata) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "settings.json"
    target.write_bytes(b'{"first":true}')
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-deadline"),
        "file-access",
    )
    carrier = RecordingCarrier()
    deadline = Deadline.after(30)
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: deadline,
    )
    try:
        result = access.update_json(
            PurePosixPath(target),
            {"second": True},
            strategy=JsonStrategy.MERGE_OVERWRITE,
            create=False,
            create_metadata=metadata,
        )
        assert result.change is Change.CHANGED
        assert len(carrier.deadlines) > 1
        assert {id(observed) for observed in carrier.deadlines} == {id(deadline)}
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_sudo_selects_the_bound_elevated_plan_before_dispatch(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-plan"),
        "file-access",
    )
    carrier = NoDispatchCarrier()
    elevated = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=elevated,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    try:
        with pytest.raises(ExternalError):
            access.stat(PurePosixPath(root / "target"), sudo=True)
        assert carrier.invocations[0].argv[0] == "/usr/bin/sudo"
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_bound_views_share_the_supplied_serial_owner(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    root.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-serial"),
        "file-access",
    )
    carrier = ReentrantCarrier()
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        carrier,
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    carrier.callback = lambda: access.stat(PurePosixPath(root / "target"))
    try:
        assert access.stat(PurePosixPath(root / "target")) is not None
        assert carrier.calls == 1
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_unresolved_observation_keeps_owner_close_refused(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    root.joinpath("target").write_bytes(b"content")
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-access-unresolved"),
        "file-access",
    )
    access = FileAccess(
        FileOperation(owner, target_for_owner(owner)),
        UnsettledCarrier(),
        trusted_root=PurePosixPath(root),
        runtime_selection=runtime_selection(sys.executable),
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="file",
        entity_name="configuration",
        deadline=lambda: Deadline.after(30),
    )
    try:
        with pytest.raises(ExternalError):
            access.stat(PurePosixPath(root / "target"))
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()
