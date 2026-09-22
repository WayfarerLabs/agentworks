"""Bound FileAccess checks over the real fixed local helpers."""

from __future__ import annotations

import grp
import os
import pwd
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


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture
def metadata() -> NewMetadata:
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
        FileOperation(owner),
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
        owner.close()
        database.close()


def test_real_bound_methods_preserve_exact_public_values(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database], metadata: NewMetadata
) -> None:
    access, root, _owner, _database = bound_access
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


def test_paths_are_exactly_confined_and_root_is_refused(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
) -> None:
    access, root, _owner, _database = bound_access
    assert access.list_directory(PurePosixPath(root), limit=DirectoryLimit()) == ()

    invalid = (
        PurePosixPath("relative"),
        PurePosixPath(str(root) + "/../outside"),
        PurePosixPath("/"),
    )
    for path in invalid:
        with pytest.raises(ValidationError):
            access.stat(path)


def test_lower_layer_object_refusals_and_diagnostics_remain_safe(
    bound_access: tuple[FileAccess, Path, OperationOwner, Database],
) -> None:
    access, root, _owner, _database = bound_access
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
    carrier = LocalCarrier()
    access = FileAccess(
        FileOperation(owner),
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
        assert carrier.calls == 0
        with pytest.raises(StateError):
            access.update_json(
                PurePosixPath(root / "target"),
                {"invalid": float("nan")},
                strategy=JsonStrategy.REPLACE,
                create=True,
                create_metadata=metadata,
                sudo=True,
            )
        assert carrier.calls == 0
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
        FileOperation(owner),
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
        FileOperation(owner),
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
        FileOperation(owner),
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
        FileOperation(owner),
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
