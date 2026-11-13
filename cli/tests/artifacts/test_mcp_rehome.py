"""Copied workspace artifacts keep their current structural placement evidence."""

from __future__ import annotations

import sys

import pytest

from agentworks.artifacts.application import ArtifactDeferral, ArtifactSkip, OwnedArtifactFile
from agentworks.artifacts.git_excludes import reconcile_workspace_excludes
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.errors import ExternalError, StateError, UserAbort
from agentworks.harness_setup.cleanup import _relocated_record
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces.manager import rehome_workspace
from tests.artifacts.test_git_excludes import file, git
from tests.artifacts.test_workspace_exclusion_rehome import move_case as move_case
from tests.capabilities.test_native_setup_deletion import deletion as deletion

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


@pytest.mark.parametrize("remove_old", [False, True])
@pytest.mark.parametrize("include_mcp", [False, True])
def test_repeated_move_rebases_common_metadata(db, deletion, move_case, remove_old, include_mcp):
    target, group = move_case
    old = target.home / "original"
    git("init", "-q", old)
    native = old / ".codex/config.toml"
    native.parent.mkdir()
    native.write_text('[mcp_servers.foreign]\ncommand = "operator-server"\n')
    original = native.read_bytes()
    skill = file(old / ".agents/skills/review/SKILL.md").model_copy(
        update={"package_root": str(old / ".agents/skills/review")}
    )
    server = NativeClaim(
        role=MCP_CLAIM_ROLE,
        identifier="agentworks-review-" + "a" * 20,
        destination=str(native),
        source="b" * 64,
        sha256="c" * 64,
    )
    claims = (server,) if include_mcp else ()

    def checkpoint(value):
        nonlocal claims
        claims = value

    reconcile_workspace_excludes(
        target.access(),
        str(old),
        "codex",
        (skill,),
        claims,
        checkpoint,
        group=group,
        report_incomplete=lambda value: pytest.fail(value),
        redactions=(),
        shared_paths=(str(native),) if include_mcp else (),
    )
    db.update_workspace_path("project", str(old))
    vm = db.get_vm("box")
    record = SetupRecord(
        component="workspace",
        integration="codex",
        declaration={},
        complete=True,
        destination_id=destination_id(vm, target.access(), location=str(old)),
        claims=claims,
        artifact_files=(skill,),
        skipped=(ArtifactSkip(path=str(old / ".agents/skipped"), origins=("d" * 64,), reason="fixture"),),
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    identities = {record.destination_id}
    for index in range(2):
        new = target.home / f"moved-{index}"
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path=str(new),
            remove_old=remove_old,
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        current = read_native_setup(db, "workspace", "project").records[0]
        moved = tuple(claim for claim in current.claims if claim.role == MCP_CLAIM_ROLE)
        expected = (server.model_copy(update={"destination": str(new / ".codex/config.toml")}),) if include_mcp else ()
        assert moved == expected
        assert current.destination_id == destination_id(vm, target.access(), location=str(new))
        assert current.destination_id not in identities
        identities.add(current.destination_id)
        assert current.complete and not current.pending_cleanup
        assert current.artifact_files[0].path == str(new / ".agents/skills/review/SKILL.md")
        assert current.artifact_files[0].package_root == str(new / ".agents/skills/review")
        assert current.artifact_files[0].sha256 == skill.sha256
        assert current.skipped[0].path == str(new / ".agents/skipped")
        assert (new / ".codex/config.toml").read_bytes() == original
        if include_mcp:
            assert git("-C", new, "check-ignore", "--", ".codex/config.toml").returncode == 0
        assert old.exists() == (not remove_old)
        if old.exists():
            assert (old / ".codex/config.toml").read_bytes() == original
        old = new


@pytest.mark.parametrize("path", ["/other/config.toml", "/old/../outside", "https://example.test/mcp"])
def test_foreign_or_malformed_claim_is_not_relocated(path):
    record = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        claims=(NativeClaim(role=MCP_CLAIM_ROLE, identifier="server", destination=path),),
    )
    with pytest.raises(StateError):
        _relocated_record(record, "/old", "/new", "b" * 64)


def test_all_existing_record_shapes_follow_copy_without_inventing_activation(db, deletion, move_case, monkeypatch):
    target, _ = move_case
    old = target.home / "original"
    old.mkdir()
    db.update_workspace_path("project", str(old))
    opaque = NativeClaim(role="future-protocol", identifier="opaque", destination="https://example.test/../entry")
    legacy = NativeClaim(role="settings", identifier="legacy", destination="not a path", written_keys=(("x",),))
    base = SetupRecord(
        component="workspace", integration="empty", destination_id="a" * 64, declaration={}, complete=True
    )
    records: tuple[SetupRecord, ...] = (
        base,
        base.model_copy(update={"integration": "settings", "declaration": {"settings": {"source": "fixture"}}}),
        base.model_copy(
            update={
                "integration": "skips",
                "complete": False,
                "pending_cleanup": True,
                "incomplete_reasons": ("earlier unresolved result",),
                "skipped": (ArtifactSkip(path=str(old / "skipped"), origins=("b" * 64,), reason="fixture"),),
                "artifact_inputs": ("b" * 64,),
                "deferred": (ArtifactDeferral(input_id="b" * 64, destination="session", reason="fixture"),),
            }
        ),
        base.model_copy(update={"integration": "unknown", "claims": (opaque, legacy)}),
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=records), operation="workspace-create")
    observations = []

    def observe(vm, access, **kwargs):
        observations.append(kwargs["location"])
        return destination_id(vm, access, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.cleanup.destination_id", observe)
    for index in range(2):
        new = target.home / f"moved-{index}"
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path=str(new),
            remove_old=False,
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        current = read_native_setup(db, "workspace", "project").records
        identity = destination_id(db.get_vm("box"), target.access(), location=str(new))
        assert len(current) == len(records)
        for before, after in zip(records, current, strict=True):
            assert after.destination_id == identity and after.destination_id != before.destination_id
            assert after.component == before.component and after.integration == before.integration
            assert after.declaration == before.declaration
            assert after.claims == before.claims and after.artifact_files == before.artifact_files
            assert after.artifact_inputs == before.artifact_inputs and after.deferred == before.deferred
            assert after.complete == before.complete and after.pending_cleanup == before.pending_cleanup
            assert after.incomplete_reasons == before.incomplete_reasons
            expected_paths = (str(new / "skipped"),) if before.integration == "skips" else ()
            assert tuple(skip.path for skip in after.skipped) == expected_paths
            assert tuple((skip.origins, skip.reason) for skip in after.skipped) == tuple(
                (skip.origins, skip.reason) for skip in before.skipped
            )
        assert current[-1].claims == (opaque, legacy)
        records = current
    assert observations == [str(target.home / f"moved-{index}") for index in range(2)]
    assert all(opaque.destination not in command and legacy.destination not in command for command in target.commands)


@pytest.mark.parametrize("evidence", ["absent", "empty", "future", "malformed"])
def test_rehome_does_not_replace_absent_or_unreadable_evidence(db, deletion, move_case, evidence, captured_output):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    old.mkdir()
    db.update_workspace_path("project", str(old))
    if evidence != "absent":
        payload = {
            "empty": VersionedPayload(3, {"records": []}),
            "future": VersionedPayload(99, {"future": True}),
            "malformed": VersionedPayload(3, {"records": "invalid"}),
        }[evidence]
        db.instance_state.replace_applied_slices(
            "workspace", "project", "workspace-create", {AppliedStateKey.HARNESS_NATIVE_SETUP: payload}
        )
    before = db.instance_state.get_applied_slices("workspace", "project")
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=False,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert db.instance_state.get_applied_slices("workspace", "project") == before
    assert db.get_workspace("project").workspace_path == str(new)
    if evidence in ("future", "malformed"):
        assert captured_output.warnings


@pytest.mark.parametrize("field", ["file", "package", "skip", "mcp"])
def test_out_of_owner_known_metadata_is_retained_without_blocking_move(db, deletion, move_case, field, captured_output):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    old.mkdir()
    item = file(old / "skill/SKILL.md")
    files: tuple[OwnedArtifactFile, ...] = ()
    skips: tuple[ArtifactSkip, ...] = ()
    claims: tuple[NativeClaim, ...] = ()
    if field == "file":
        files = (item.model_copy(update={"path": str(target.home / "foreign/SKILL.md")}),)
    elif field == "package":
        files = (item.model_copy(update={"package_root": str(target.home)}),)
    elif field == "skip":
        skips = (ArtifactSkip(path=str(target.home / "foreign/skip"), origins=("a" * 64,), reason="fixture"),)
    else:
        claims = (NativeClaim(role=MCP_CLAIM_ROLE, identifier="server", destination="https://example.test/../entry"),)
    record = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        complete=True,
        artifact_files=files,
        skipped=skips,
        claims=claims,
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    db.update_workspace_path("project", str(old))
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=False,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    current = read_native_setup(db, "workspace", "project").records[0]
    assert db.get_workspace("project").workspace_path == str(new)
    assert current.artifact_files == files and current.skipped == skips and current.claims == claims
    assert current.destination_id == record.destination_id
    assert not current.complete and current.pending_cleanup and current.incomplete_reasons
    assert len(current.incomplete_reasons) == 1 and len(current.incomplete_reasons[0]) <= 1024
    assert captured_output.warnings


def test_modified_file_and_literal_absolute_body_keep_bytes_and_saved_fingerprints(db, deletion, move_case):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    git("init", "-q", old)
    item = file(old / "skill/SKILL.md").model_copy(update={"package_root": str(old / "skill"), "executable": True})
    section = file(old / "guidance.md").model_copy(update={"generated_section": True, "native_identity": "legacy"})
    body = f"operator modified body referring to {old}/literal.txt".encode()
    (old / "skill/SKILL.md").write_bytes(body)
    (old / "guidance.md").write_bytes(body)
    record = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        complete=True,
        artifact_files=(item, section),
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    db.update_workspace_path("project", str(old))
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=False,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    current = read_native_setup(db, "workspace", "project").records[0]
    assert current.artifact_files == (
        item.model_copy(update={"path": str(new / "skill/SKILL.md"), "package_root": str(new / "skill")}),
        section.model_copy(update={"path": str(new / "guidance.md")}),
    )
    assert (new / "skill/SKILL.md").read_bytes() == body
    assert (new / "guidance.md").read_bytes() == body


def test_relocation_checkpoint_failure_escapes_after_copy(db, deletion, move_case, monkeypatch):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    old.mkdir()
    record = SetupRecord(component="workspace", integration="codex", destination_id="a" * 64, declaration={})
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    db.update_workspace_path("project", str(old))
    failure = StateError("fixture checkpoint failure")

    def checkpoint(db, kind, name, state, **kwargs):
        if state.records[0].destination_id != record.destination_id:
            raise failure
        write_native_setup(db, kind, name, state, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.cleanup.write_native_setup", checkpoint)
    with pytest.raises(StateError) as observed:
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path=str(new),
            remove_old=True,
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert observed.value is failure
    assert db.get_workspace("project").workspace_path == str(old)
    assert old.exists() and new.exists()
    assert read_native_setup(db, "workspace", "project").records == (record,)


@pytest.mark.parametrize("failure", [UserAbort("fixture cancellation"), RuntimeError("fixture internal failure")])
@pytest.mark.parametrize("stage", ["old-git", "new-git", "identity"])
def test_cancellation_and_internal_failures_escape(db, deletion, move_case, monkeypatch, failure, stage):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    old.mkdir()
    record = SetupRecord(component="workspace", integration="codex", destination_id="a" * 64, declaration={})
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    db.update_workspace_path("project", str(old))

    def reconcile(access, path, *args, **kwargs):
        if path == str(old if stage == "old-git" else new):
            raise failure

    def observe(*args, **kwargs):
        raise failure

    monkeypatch.setattr("agentworks.harness_setup.cleanup.reconcile_workspace_excludes", reconcile)
    if stage == "identity":
        monkeypatch.setattr(
            "agentworks.harness_setup.cleanup.reconcile_workspace_excludes", lambda *args, **kwargs: None
        )
        monkeypatch.setattr("agentworks.harness_setup.cleanup.destination_id", observe)
    expected = type(failure) if isinstance(failure, UserAbort) else ExternalError
    with pytest.raises(expected) as observed:
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path=str(new),
            remove_old=True,
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert observed.value is failure or observed.value.__cause__ is failure
    assert db.get_workspace("project").workspace_path == str(old)
    assert old.exists() and new.exists()


def test_failed_destination_observation_is_shared_across_records(db, deletion, move_case, monkeypatch):
    target, _ = move_case
    old, new = target.home / "original", target.home / "moved"
    old.mkdir()
    records = tuple(
        SetupRecord(component="workspace", integration=name, destination_id="a" * 64, declaration={}, complete=True)
        for name in ("first", "second")
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=records), operation="workspace-create")
    db.update_workspace_path("project", str(old))
    observations = []

    def observe(*args, **kwargs):
        observations.append(kwargs["location"])
        raise ExternalError("fixture identity observation failed")

    monkeypatch.setattr("agentworks.harness_setup.cleanup.destination_id", observe)
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=False,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    current = read_native_setup(db, "workspace", "project").records
    assert observations == [str(new)]
    assert len(current) == 2
    assert all(record.destination_id == "a" * 64 for record in current)
    assert all(not record.complete and record.pending_cleanup and record.incomplete_reasons for record in current)
