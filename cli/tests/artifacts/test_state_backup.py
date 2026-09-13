"""Core captures and session ownership participate in ordinary VM snapshots."""

from agentworks.artifacts.model import ArtifactContent, ArtifactInput, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.artifacts.state import CapturedArtifacts, decode_captures, write_capture
from agentworks.db import AppliedStateKey, SessionMode
from tests.artifacts._fixtures import group


def test_vm_snapshot_includes_session_capture_and_ignores_unrelated_damaged_session(db):
    db.insert_vm("box", site="local", hostname="box")
    db.insert_vm("other", site="local", hostname="other")
    db.insert_workspace("work", workspace_path="/work", vm_name="box", linux_group="work", template="default")
    db.insert_workspace(
        "elsewhere", workspace_path="/elsewhere", vm_name="other", linux_group="other", template="default"
    )
    session = db.insert_session("run", "work", "default", SessionMode.ADMIN)
    db.insert_session("broken", "elsewhere", "default", SessionMode.ADMIN)
    db._conn.execute("UPDATE sessions SET session_uuid = 'damaged' WHERE name = 'broken'")
    db._conn.commit()
    item = ArtifactInput(
        ArtifactContent(ArtifactType.HINT, "setup", text="captured context"),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", "run", bundle="tools", entry="setup"),
    )
    capture = CapturedArtifacts("a" * 64, group(item))
    write_capture(db, "session", "run", "session", capture, operation="session-start")
    snapshot = db.snapshot_vm_backup_data("box")
    assert snapshot[3] == [session]
    slices = snapshot[-1]
    assert len(slices) == 1 and slices[0].key is AppliedStateKey.ARTIFACT_INPUTS
    assert decode_captures(slices[0]) == {"session": capture}


def test_native_file_package_root_survives_database_and_backup_round_trip(db):
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import decode_native_setup, read_native_setup, write_native_setup

    db.insert_vm("box", site="local", hostname="box")
    file = OwnedArtifactFile(
        path="/home/user/.claude/skills/review/scripts/check.sh",
        sha256="a" * 64,
        origins=("b" * 64,),
        native_identity="skill:review",
        package_root="/home/user/.claude/skills/review",
    )
    state = NativeSetupState(
        records=(
            SetupRecord(
                component="admin",
                integration="claude-code",
                destination_id="c" * 64,
                declaration={},
                artifact_files=(file,),
            ),
        )
    )
    write_native_setup(db, "vm", "box", state, operation="fixture")
    assert read_native_setup(db, "vm", "box") == state
    saved = next(
        record for record in db.snapshot_vm_backup_data("box")[-1] if record.key is AppliedStateKey.HARNESS_NATIVE_SETUP
    )
    assert decode_native_setup(saved) == state


def test_persisted_package_root_requires_a_normalized_containing_path():
    import pytest
    from pydantic import ValidationError

    from agentworks.artifacts.application import OwnedArtifactFile

    for root in ("relative", "/home/../other", "/other", "/home/user/file"):
        with pytest.raises(ValidationError):
            OwnedArtifactFile(path="/home/user/file", sha256="a" * 64, origins=("b" * 64,), package_root=root)
