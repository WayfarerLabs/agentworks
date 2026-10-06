"""Shared bound FileAccess fixture for file-operation behavior tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.access import FileAccess
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._file_snapshot_support import install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


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
