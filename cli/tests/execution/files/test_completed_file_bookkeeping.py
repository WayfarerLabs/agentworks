"""Completed file custody retries through actual local host context helpers."""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError
from agentworks.execution._file_json import FileJsonChange, FileJsonOutcome, FileJsonStatus
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_operations import OwnedFileOutcome
from agentworks.execution._platform_host_context import PlatformHostContextControlFact
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import DirectoryLimit, FileKind, JsonStrategy, NewMetadata
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationBorrow
from tests.execution.files._file_upload_support import LostCallStdoutCarrier
from tests.execution.test_platform_host_context import fixture as fixture

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._file_operation import (
        _ActiveFileInventory,
        _ActiveFileJsonUpdate,
        _ActiveFileMetadata,
        _ActiveFileRemove,
        _ActiveFileStat,
    )
    from agentworks.execution.access import FileAccess
    from agentworks.execution.files import Revision

type _CompletedCall = (
    _ActiveFileStat | _ActiveFileInventory | _ActiveFileRemove | _ActiveFileMetadata | _ActiveFileJsonUpdate
)

_FAMILIES = ("stat", "inventory", "remove", "metadata", "ensure", "create", "json-changed", "json-unchanged")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="ordinary local file helper proof requires Linux")


def _active(operation: FileOperation, family: str) -> tuple[_CompletedCall, ...]:
    if family.startswith("json"):
        return operation.active_json_updates
    if family in {"metadata", "ensure", "create"}:
        return operation.active_metadata
    if family == "inventory":
        return operation.active_inventories
    if family == "remove":
        return operation.active_removals
    return operation.active_stats


def _call(files: FileAccess, root: Path, metadata: NewMetadata, family: str, expected: Revision | None) -> None:
    path = PurePosixPath(root / "target")
    if family == "stat":
        files.stat(path)
    elif family == "inventory":
        files.list_directory(PurePosixPath(root / "listed"), limit=DirectoryLimit(16, 1, 4096))
    elif family == "remove":
        assert expected is not None
        files.remove(path, expected_kind=FileKind.REGULAR, expected=expected)
    elif family == "metadata":
        files.set_metadata(path, owner=metadata.owner, group=metadata.group, mode=0o640)
    elif family in {"ensure", "create"}:
        create = files.ensure_directory if family == "ensure" else files.create_directory
        create(PurePosixPath(root / "created"), metadata=NewMetadata(metadata.owner, metadata.group, 0o700))
    else:
        files.update_json(
            path,
            {"added": 2} if family == "json-changed" else {"original": 1},
            strategy=JsonStrategy.SKIP_EXISTING if family == "json-unchanged" else JsonStrategy.MERGE_OVERWRITE,
            create=False,
            create_metadata=metadata,
        )


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("committed", [False, True], ids=["before-close", "after-close"])
def test_completed_calls_retry_original_borrow_without_replay(fixture, monkeypatch, family, committed):
    db, carrier, root, scratch, metadata, lifetimes, context = fixture
    root.joinpath("target").write_bytes(b'{"original":1}')
    root.joinpath("listed").mkdir()
    root.joinpath("listed", "entry").write_bytes(b"inventory")
    close = OperationBorrow.close
    control, cause = KeyboardInterrupt(), ValueError("original control cause")
    control.__cause__ = cause
    captured = []

    def interrupted(borrow):
        active = _active(lifetimes[0].files, family)[0]
        assert active.borrow is borrow and active.outcome is not None
        assert not active.outcome.requires_owner_retention and not borrow.has_outstanding_attempt
        captured.append(active)
        if committed:
            close(borrow)
        raise control

    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        target = ctx.platform_host_execution_target()
        execution, files = target.execution(), target.files()
        result = execution.run(Command(["/bin/sh", "-c", "printf before-file"]), profile=Protection.DIRECT)
        assert result.ok and result.stdout.data == b"before-file"
        observed = files.stat(PurePosixPath(root / "target")) if family == "remove" else None
        expected = observed.revision if observed is not None else None
        monkeypatch.setattr(OperationBorrow, "close", interrupted)
        _call(files, root, metadata, family, expected)
    assert caught.value is control
    operation = lifetimes[0].files
    active = captured[0]
    outcome = active.outcome
    assert outcome is not None
    assert _active(operation, family) == (() if committed else (active,))
    assert active.borrow.closed is committed and not active.borrow.has_outstanding_attempt
    if family.startswith("json"):
        assert isinstance(outcome, FileJsonOutcome)
        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.change is (FileJsonChange.CHANGED if family == "json-changed" else FileJsonChange.UNCHANGED)
    else:
        assert isinstance(outcome, OwnedFileOutcome)
        assert outcome.result is not None and outcome.result.observation is not None
    before = carrier.calls
    assert active.obligation is not None
    payload = active.obligation._persisted_obligation
    monkeypatch.setattr(OperationBorrow, "close", close)
    if committed:
        assert control.__cause__ is cause
        assert db.operations.inspect(lifetimes[0].owner.ownership.scope) is None
        lifetimes[0].close(Deadline.after(10))
    else:
        fact = control.__cause__
        assert isinstance(fact, PlatformHostContextControlFact) and fact.__cause__ is cause
        fact.retry_cleanup(Deadline.after(10))
        fact.retry_cleanup(Deadline.after(10))
    assert active.borrow.closed and active.outcome is outcome
    assert active.obligation._persisted_obligation == payload
    assert _active(operation, family) == () and not operation.has_pending_custody
    assert operation.unfinished_owned_files == operation.unfinished_json_updates == ()
    assert carrier.calls == before and db.operations.inspect(lifetimes[0].owner.ownership.scope) is None
    assert result.ok and result.stdout.data == b"before-file"
    if family == "remove":
        assert not root.joinpath("target").exists()
    else:
        assert json.loads(root.joinpath("target").read_bytes()) == (
            {"original": 1, "added": 2} if family == "json-changed" else {"original": 1}
        )
    if family == "metadata":
        assert stat.S_IMODE(root.joinpath("target").stat().st_mode) == 0o640
    if family in {"ensure", "create"}:
        assert root.joinpath("created").is_dir()
    assert not list(scratch.iterdir())


@pytest.mark.parametrize(
    "family,unknown",
    [
        ("stat", "no-outcome"),
        ("json-changed", "no-outcome"),
        ("stat", "remote-completion"),
        ("json-changed", "remote-completion"),
        ("json-changed", "publication-debt"),
    ],
)
def test_unknown_or_missing_capture_retains_exact_custody_without_reclassification(
    fixture, monkeypatch, family, unknown
):
    db, carrier, root, _, metadata, lifetimes, context = fixture
    root.joinpath("target").write_bytes(b'{"original":1}')
    control, cause = KeyboardInterrupt(), ValueError("uncertain call cause")
    control.__cause__ = cause
    lost = LostCallStdoutCarrier(4)

    def interrupted(*args, **kwargs):
        raise control

    with pytest.raises(KeyboardInterrupt) as caught, context() as ctx:
        files = ctx.platform_host_execution_target().files()
        if unknown == "no-outcome":
            monkeypatch.setattr(
                FileOperation, "_capture_json_update" if family.startswith("json") else "_capture_owned", interrupted
            )
        elif unknown == "remote-completion":
            execute = carrier.execute

            def uncertain(*args, **kwargs):
                return replace(execute(*args, **kwargs), completion=None)

            monkeypatch.setattr(carrier, "execute", uncertain)
            monkeypatch.setattr(OperationBorrow, "handoff_unresolved", interrupted)
        else:
            monkeypatch.setattr(carrier, "execute", lost.execute)
            monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", interrupted)
        _call(files, root, metadata, family, None)
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, PlatformHostContextControlFact)
    operation = lifetimes[0].files
    active = _active(operation, family)[0]
    if unknown == "no-outcome":
        assert active.outcome is None
    else:
        assert active.outcome is not None and active.outcome.requires_owner_retention
        if unknown == "remote-completion":
            assert active.borrow.has_outstanding_attempt
        else:
            assert isinstance(active.outcome, FileJsonOutcome)
            assert active.outcome.upload_outcome is not None
            assert active.outcome.upload_outcome.scratch_cleanup_debt is not None
            assert not active.borrow.has_outstanding_attempt
    before = carrier.calls
    lost_before = lost.calls
    outcome = active.outcome
    pending = lifetimes[0].owner.list_pending_lifecycle_obligations()
    unfinished = (operation.unfinished_owned_files, operation.unfinished_json_updates)
    for _ in range(2):
        with pytest.raises(StateError):
            fact.retry_cleanup(Deadline.after(10))
        assert _active(operation, family) == (active,) and active.outcome is outcome
        assert (operation.unfinished_owned_files, operation.unfinished_json_updates) == unfinished
        assert lifetimes[0].owner.list_pending_lifecycle_obligations() == pending
        assert carrier.calls == before and db.operations.inspect(lifetimes[0].owner.ownership.scope) is not None
        assert lost.calls == lost_before
