"""Initial guest deadline checks before destination filesystem access."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType
from typing import cast

import pytest

from agentworks.execution import (
    _file_inventory_guest,
    _file_metadata_guest,
    _file_object_guest,
    _file_read_guest,
    _file_stage_guest,
)
from agentworks.execution._file_inventory_protocol import FileInventoryFailureCode, FileInventoryRequest
from agentworks.execution._file_metadata import MetadataFailureKind, MetadataPhase
from agentworks.execution._file_metadata_protocol import (
    FileMetadataFailureCode,
    FileMetadataFailureControl,
    FileMetadataOperation,
    FileMetadataRequest,
)
from agentworks.execution._file_object_protocol import (
    FileObjectFailureCode,
    FileObjectFailureControl,
    FileObjectOperation,
    FileObjectRequest,
)
from agentworks.execution._file_objects import FileObjectFailureKind, FileObjectPhase
from agentworks.execution._file_read_protocol import FileReadFailure, FileReadRequest
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageFailureCode,
    FileStageFailureControl,
)
from agentworks.execution._helper_identity import IdentityExpectation

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file helpers require Linux")

_NONCE = "0" * 32
_IDENTITY = IdentityExpectation(1000, 1000, (1000,))
_CASES = (
    (
        _file_read_guest,
        FileReadRequest(_NONCE, "/private", "leaf", 1, _IDENTITY, 0.0),
        FileReadFailure.DEADLINE,
    ),
    (
        _file_inventory_guest,
        FileInventoryRequest(_NONCE, "/private", "leaf", 8, 1, 4096, _IDENTITY, 0.0),
        FileInventoryFailureCode.DEADLINE,
    ),
    (
        _file_metadata_guest,
        FileMetadataRequest(
            _NONCE,
            FileMetadataOperation.SET_METADATA,
            "/private",
            "leaf",
            _IDENTITY.euid,
            _IDENTITY.egid,
            0o600,
            0.0,
            _IDENTITY,
        ),
        FileMetadataFailureControl(
            FileMetadataFailureCode.METADATA,
            MetadataFailureKind.DEADLINE,
            MetadataPhase.OBSERVATION,
        ),
    ),
    (
        _file_object_guest,
        FileObjectRequest(_NONCE, FileObjectOperation.STAT, "/private", "leaf", 0.0, _IDENTITY),
        FileObjectFailureControl(
            FileObjectFailureCode.OBJECT,
            FileObjectFailureKind.DEADLINE,
            FileObjectPhase.OBSERVATION,
        ),
    ),
    (
        _file_stage_guest,
        FileStageBeginRequest(_NONCE, "/private", "leaf", bytes(range(16)), 1, _IDENTITY, 0.0),
        FileStageFailureControl(FileStageFailureCode.DEADLINE),
    ),
)


@pytest.mark.parametrize(
    ("guest", "decoded", "expected"),
    _CASES,
    ids=("read", "inventory", "metadata", "object", "stage"),
)
def test_expired_budget_refuses_before_root_access(
    guest: ModuleType,
    decoded: object,
    expected: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []

    def forbid_root_access(_path: str) -> int:
        pytest.fail("destination root was accessed after the budget expired")

    def finish_failure(_writer: object, failure: object) -> int:
        observed.append(failure)
        return 0

    monkeypatch.setattr(guest, "_read_request", lambda: decoded)
    monkeypatch.setattr(guest, "matches_current_identity", lambda _expected: True)
    monkeypatch.setattr(f"{guest.__name__}.time.monotonic", lambda: 10.0)
    monkeypatch.setattr(guest, "open_linux_root", forbid_root_access)
    monkeypatch.setattr(guest, "_finish_failure", finish_failure)
    main = cast("Callable[[str], int]", guest.main)

    assert main(_NONCE) == 0
    assert observed == [expected]
