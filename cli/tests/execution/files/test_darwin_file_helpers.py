"""All ordinary file families compose with emulated Darwin descriptor primitives.

The helper bundles execute in isolated real Python processes on Linux. Only the
Darwin native calls are emulated; this does not certify macOS Python or filesystems.
"""

from __future__ import annotations

import ctypes
import grp
import importlib
import io
import os
import pwd
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentworks.execution import _file_read
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import (
    Change,
    Create,
    DirectoryLimit,
    DirectoryTransferLimit,
    ExistingFile,
    FileKind,
    JsonStrategy,
    Match,
    NewMetadata,
    Replace,
)
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection

_FAMILIES = ("read", "object", "inventory", "stage", "snapshot", "publication", "metadata")


def _install_bundles(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    for family in _FAMILIES:
        bundle = importlib.import_module(f"agentworks.execution._file_{family}_bundle")
        host = importlib.import_module(
            f"agentworks.execution._file_{family}"
            if family == "read"
            else f"agentworks.execution._file_{family}_exchange"
        )
        package = bundle._PACKAGE
        patch = f"""
import os
sys.platform='darwin'
d=sys.modules[{(package + "._darwin_descriptors")!r}]
d.extended_acl_tags=lambda fd:()
def forbid_saved_ids():
 raise AssertionError('Linux saved IDs used on Darwin')
os.getresuid=forbid_saved_ids
os.getresgid=forbid_saved_ids
"""
        if family in {"publication", "metadata"}:
            patch += f"""
m=sys.modules[{(package + "._darwin_metadata")!r}]
m.bsd_flags=lambda observed:0
m.has_xattrs=lambda fd:False
"""
        if family == "snapshot":
            patch += f"""
r=sys.modules[{(package + "._scratch_root")!r}]
r._DARWIN_SCRATCH_ROOT={str(scratch)!r}
r._EXPECTED_OWNER_UID=os.geteuid()
"""
        if family == "publication":
            patch += f"""
p=sys.modules[{(package + "._file_publication")!r}]
def emulated_exclusive_rename(parent,stage,leaf):
 try:
  os.link(stage,leaf,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
 except FileExistsError:
  raise p.FilePublicationError(p.PublicationFailureKind.CONFLICT,p.PublicationPhase.PUBLICATION) from None
 os.unlink(stage,dir_fd=parent)
p._rename_noreplace=emulated_exclusive_rename
"""
        monkeypatch.setattr(
            host, "FIXED_BUNDLE", fixture_file_bundle(package, bundle._MODULE_NAMES, f"_file_{family}_guest", patch)
        )


@pytest.fixture
def darwin_access(bound_access, monkeypatch):
    access, root, owner, database = bound_access
    _install_bundles(monkeypatch, root.parent / "scratch")
    return access, root, owner, database


def _metadata(mode: int = 0o640) -> NewMetadata:
    return NewMetadata(pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name, mode)


def _finish(owner) -> None:
    assert not owner.list_pending_lifecycle_obligations()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_public_ordinary_methods_compose_through_darwin_bundles(darwin_access, plan, tmp_path) -> None:
    access, root, owner, _ = darwin_access
    target = PurePosixPath(root / "target")
    created = access.write_file(target, b"binary\0\xff", condition=Create(), create_metadata=_metadata())
    assert created.change is Change.CHANGED
    before = (root / "target").stat()
    observed = access.stat(target)
    assert observed is not None and observed.kind is FileKind.REGULAR
    snapshot = access.read_file(target, max_bytes=1024)
    assert snapshot is not None and snapshot.data == b"binary\0\xff"
    assert (
        access.write_file(
            target, b"matched", condition=Match(snapshot.metadata.revision), create_metadata=_metadata(0o600)
        ).change
        is Change.CHANGED
    )
    assert (
        access.write_file(target, b"replaced\xff", condition=Replace(), create_metadata=_metadata()).change
        is Change.CHANGED
    )
    assert (root / "target").stat().st_ino == before.st_ino
    access.upload(target, io.BytesIO(b"stream\0\xff"), size=8, condition=Replace(), create_metadata=_metadata())
    assert (root / "target").read_bytes() == b"stream\0\xff"
    downloaded = tmp_path / "download"
    access.download(target, downloaded)
    assert downloaded.read_bytes() == b"stream\0\xff"
    directory = PurePosixPath(root / "directory")
    assert access.ensure_directory(directory, metadata=_metadata(0o750)).change is Change.CHANGED
    assert (
        access.set_metadata(target, owner=_metadata().owner, group=_metadata().group, mode=0o600).change
        is Change.CHANGED
    )
    assert {entry.relative_path for entry in access.list_directory(PurePosixPath(root), limit=DirectoryLimit())} == {
        PurePosixPath("target"),
        PurePosixPath("directory"),
    }
    direct = _file_read.read_file(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        max_bytes=1024,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert direct.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert direct.observation is not None and direct.observation.snapshot is not None
    assert direct.observation.snapshot.data == b"stream\0\xff"
    final = access.stat(target)
    assert final is not None
    assert access.remove(target, expected_kind=FileKind.REGULAR, expected=final.revision).change is Change.CHANGED
    _finish(owner)


@pytest.mark.parametrize("strategy", list(JsonStrategy))
def test_json_strategies_use_same_darwin_snapshot_and_publication(darwin_access, strategy) -> None:
    access, root, owner, _ = darwin_access
    target = PurePosixPath(root / "settings.json")
    access.update_json(
        target,
        {"old": True, "nested": {"value": 1}},
        strategy=JsonStrategy.REPLACE,
        create=True,
        create_metadata=_metadata(),
    )
    before = (root / "settings.json").stat()
    result = access.update_json(
        target, {"new": True, "nested": {"value": 2}}, strategy=strategy, create=True, create_metadata=_metadata(0o600)
    )
    assert (root / "settings.json").stat().st_ino == before.st_ino
    snapshot = access.read_file(target, max_bytes=4096)
    assert snapshot is not None
    import json

    value = json.loads(snapshot.data)
    if strategy is JsonStrategy.SKIP_EXISTING:
        assert result.change is Change.UNCHANGED and value == {"old": True, "nested": {"value": 1}}
    elif strategy is JsonStrategy.MERGE_PRESERVE:
        assert value == {"old": True, "new": True, "nested": {"value": 1}}
    elif strategy is JsonStrategy.MERGE_OVERWRITE:
        assert value == {"old": True, "new": True, "nested": {"value": 2}}
    else:
        assert value == {"new": True, "nested": {"value": 2}}
    _finish(owner)


def test_directory_transfer_uses_ordinary_darwin_children(darwin_access, tmp_path) -> None:
    access, root, owner, _ = darwin_access
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "binary").write_bytes(b"binary\0\xff")
    (source / "empty").touch()
    target = PurePosixPath(root / "tree")
    limit = DirectoryTransferLimit(max_bytes=4096)
    uploaded = access.upload_directory(
        target,
        source,
        limit=limit,
        existing_files=ExistingFile.ERROR,
        file_metadata=_metadata(),
        directory_metadata=_metadata(0o750),
    )
    assert uploaded.transferred_bytes == 8
    destination = tmp_path / "copied"
    downloaded = access.download_directory(target, destination, limit=limit, existing_files=ExistingFile.ERROR)
    assert downloaded.transferred_bytes == 8
    assert (destination / "nested" / "binary").read_bytes() == b"binary\0\xff"
    assert (destination / "empty").read_bytes() == b""
    _finish(owner)


@pytest.mark.parametrize("family", _FAMILIES)
def test_every_entry_refuses_wrong_darwin_identity_before_filesystem(family: str, monkeypatch) -> None:
    guest = importlib.import_module(f"agentworks.execution._file_{family}_guest")
    nonce = "0" * 32
    request = SimpleNamespace(
        nonce=nonce, identity=IdentityExpectation(os.geteuid() + 1, os.getegid(), (os.getegid(),))
    )
    failures = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(guest, "_read_request", lambda: request)
    monkeypatch.setattr(guest, "_finish_failure", lambda writer, failure: failures.append(failure) or 97)
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: pytest.fail("identity refusal touched paths"))
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: pytest.fail("identity refusal loaded native functions"))
    assert guest.main(nonce) == 97
    code = failures[0].code if hasattr(failures[0], "code") else failures[0]
    assert code.value == "identity_mismatch"
