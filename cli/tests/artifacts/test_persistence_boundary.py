"""Captured inputs remain readable before lifecycle effects or state replacement."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.artifacts import codec
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec
from agentworks.artifacts.model import ArtifactContent, ArtifactInput, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.artifacts.state import (
    CapturedArtifacts,
    UnsupportedArtifactCaptureVersionError,
    capture_owner,
    decode_captures,
    read_captures,
    write_capture,
)
from agentworks.db import AppliedStateKey, Database, VersionedPayload
from agentworks.doctor import InstanceStateHealthFactType, Status
from agentworks.doctor_state import check_database
from agentworks.errors import StateError
from agentworks.resources.registry import Registry
from tests.artifacts._fixtures import group
from tests.conftest import _StubRegistry


def _input(entry: str = "setup") -> ArtifactInput:
    return ArtifactInput(
        ArtifactContent(ArtifactType.HINT, entry, text="private fixture content"),
        ArtifactProvenance(),
        ArtifactOrigin("vm", "vm", "box", bundle="tools", entry=entry),
    )


@pytest.mark.parametrize("invalid", ["origin", "metadata", "aggregate", "declaration", "owner"])
def test_invalid_write_retains_readable_prior_capture(
    db: Database, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    db.insert_vm("box", site="local", hostname="box")
    item = _input()
    previous = CapturedArtifacts("a" * 64, group(item))
    write_capture(db, "vm", "box", "vm", previous, operation="test")
    before = db.instance_state.get_applied_slices("vm", "box")
    candidate = replace(previous, declaration="b" * 64)
    if invalid == "origin":
        candidate = replace(candidate, inputs=group(replace(item, origin=replace(item.origin, producer="x" * 4097))))
    elif invalid == "metadata":
        candidate = replace(
            candidate, inputs=group(replace(item, content=replace(item.content, metadata_json=" " * 65537)))
        )
    elif invalid == "aggregate":
        monkeypatch.setattr(codec, "_LIMITS", replace(codec._LIMITS, total_bytes=1024))
        candidate = replace(
            candidate,
            inputs=group(
                *(replace(value, content=replace(value.content, text="x" * 700)) for value in (item, _input("second")))
            ),
        )
    elif invalid == "declaration":
        candidate = replace(candidate, declaration="invalid")
    else:
        candidate = replace(candidate, inputs=group(replace(item, origin=replace(item.origin, resource_name="other"))))

    with pytest.raises(StateError) as error:
        write_capture(db, "vm", "box", "vm", candidate, operation="replace")

    assert error.value.entity_kind == "vm"
    assert error.value.entity_name == "box"
    assert item.content.text not in str(error.value)
    assert db.instance_state.get_applied_slices("vm", "box") == before
    assert read_captures(db, "vm", "box") == {"vm": previous}


def test_capture_rejects_persistence_limits_before_returning_buffered_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = ArtifactBundle(name="tools", hints={"setup": HintArtifactSpec(text="private fixture content")})
    registry = cast(Registry, _StubRegistry(SimpleNamespace(artifact_bundles={"tools": bundle})))
    monkeypatch.setattr(codec, "_LIMITS", replace(codec._LIMITS, total_bytes=8))

    with pytest.raises(StateError) as error:
        capture_owner(registry, "agent", "new-agent", "agent", ArtifactsConfig(bundles=["tools"]))

    assert error.value.entity_kind == "agent"
    assert error.value.entity_name == "new-agent"
    assert "private fixture content" not in str(error.value)


@pytest.mark.parametrize("version", [2, 3])
def test_doctor_distinguishes_corrupt_capture_from_newer_version(
    db: Database, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    monkeypatch.setattr("agentworks.db.DB_PATH", Path(db._conn.execute("PRAGMA database_list").fetchone()[2]))
    db.insert_vm("box", site="local", hostname="box")
    payload = VersionedPayload(version, {"private-fixture-payload": True})
    db.instance_state.replace_applied_slices("vm", "box", "test", {AppliedStateKey.ARTIFACT_INPUTS: payload})
    before = db.instance_state.get_applied_slices("vm", "box")

    checks = [
        check
        for check in check_database(None).checks
        if check.instance_state is not None and check.instance_state.record_key == AppliedStateKey.ARTIFACT_INPUTS.value
    ]

    assert len(checks) == 1
    assert checks[0].status is (Status.INFO if version == 3 else Status.FAIL)
    assert checks[0].instance_state is not None
    assert checks[0].instance_state.fact_type is (
        InstanceStateHealthFactType.UNCONSUMED_RECORD if version == 3 else InstanceStateHealthFactType.MALFORMED_RECORD
    )
    assert "private-fixture-payload" not in str(checks)
    assert db.instance_state.get_applied_slices("vm", "box") == before
    if version == 3:
        with pytest.raises(UnsupportedArtifactCaptureVersionError):
            decode_captures(before[0])
        with pytest.raises(UnsupportedArtifactCaptureVersionError):
            write_capture(db, "vm", "box", "vm", CapturedArtifacts("a" * 64, group(_input())), operation="replace")
        assert db.snapshot_vm_backup_data("box")[-1][0].payload == payload
        assert db.instance_state.get_applied_slices("vm", "box") == before
