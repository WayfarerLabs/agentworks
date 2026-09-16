"""Whole-file artifact ownership against an isolated real filesystem transport."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactContent, ArtifactInput, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.artifacts.publication import publish_artifacts, validate_application
from agentworks.errors import StateError
from tests.artifacts._fixtures import received
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


@pytest.fixture
def target(tmp_path):
    return LocalFixtureTransport(tmp_path / "target")


def artifact(path: Path, data: bytes = b"content", *, executable: bool = False) -> ArtifactFile:
    return ArtifactFile(str(path), data, ("a" * 64,), executable=executable)


def test_owned_file_update_mode_and_idempotent_removal(target):
    path = target.home / "skills/review/run.sh"
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    first = publish_artifacts(target, (artifact(path),), (), checkpoints.append, roots=(str(target.home),)).files
    assert path.read_bytes() == b"content"
    before = path.stat().st_mtime_ns
    same = publish_artifacts(target, (artifact(path),), first, checkpoints.append, roots=(str(target.home),)).files
    assert same == first and path.stat().st_mtime_ns == before
    updated = publish_artifacts(
        target,
        (artifact(path, b"#!/bin/sh\n", executable=True),),
        first,
        checkpoints.append,
        roots=(str(target.home),),
    ).files
    assert path.stat().st_mode & 0o777 == 0o700
    assert len(checkpoints) == 2
    assert publish_artifacts(target, (), updated, checkpoints.append, roots=(str(target.home),)).files == ()
    assert not path.exists()
    assert publish_artifacts(target, (), updated, checkpoints.append, roots=(str(target.home),)).files == ()


def test_entire_plan_collision_checked_before_first_write(target):
    first, second = target.home / "first", target.home / "second"
    second.write_bytes(b"content")
    with pytest.raises(StateError):
        publish_artifacts(
            target, (artifact(first), artifact(second)), (), lambda files: None, roots=(str(target.home),)
        )
    assert not first.exists() and second.read_bytes() == b"content"


def test_modified_obsolete_file_retains_ownership(target):
    path = target.home / "managed"
    owned = publish_artifacts(target, (artifact(path),), (), lambda files: None, roots=(str(target.home),)).files
    path.write_bytes(b"operator edit")
    assert publish_artifacts(target, (), owned, lambda files: None, roots=(str(target.home),)).files == owned
    assert path.read_bytes() == b"operator edit"
    with pytest.raises(StateError):
        publish_artifacts(target, (artifact(path, b"update"),), owned, lambda files: None, roots=(str(target.home),))


def test_modified_file_mode_is_preserved_and_diagnosed(target):
    path = target.home / "managed"
    owned = publish_artifacts(target, (artifact(path),), (), lambda files: None, roots=(str(target.home),)).files
    path.chmod(0o700)
    with pytest.raises(StateError):
        publish_artifacts(target, (artifact(path),), owned, lambda files: None, roots=(str(target.home),))
    assert publish_artifacts(target, (), owned, lambda files: None, roots=(str(target.home),)).files == owned
    assert path.exists() and path.stat().st_mode & 0o777 == 0o700


def test_checkpoint_failure_stops_further_publication(target):
    first, second = target.home / "first", target.home / "second"

    def unavailable(files):
        assert len(files) == 1
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        publish_artifacts(target, (artifact(first), artifact(second)), (), unavailable, roots=(str(target.home),))
    assert first.exists() and not second.exists()
    assert not list((target.root / "tmp").iterdir())


def test_scope_escape_and_links_are_refused(target):
    outside = target.root / "outside"
    outside.mkdir()
    with pytest.raises(StateError):
        publish_artifacts(target, (artifact(outside / "file"),), (), lambda files: None, roots=(str(target.home),))
    (target.home / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError):
        publish_artifacts(
            target, (artifact(target.home / "link/file"),), (), lambda files: None, roots=(str(target.home),)
        )
    assert not (outside / "file").exists()


def test_deferred_input_must_be_known_unique_and_routed_inward():
    item = ArtifactInput(
        ArtifactContent(ArtifactType.HINT, "setup", text="hint"),
        ArtifactProvenance(),
        ArtifactOrigin("vm", "vm", "box", entry="setup"),
    )
    deferred = ArtifactDeferral(input_id=item.identity, destination="user", reason="native user placement")
    assert validate_application(
        ArtifactApplication(deferred=(deferred,)), received(item), "vm", integration="fixture"
    ).deferred == (deferred,)
    for facet, inputs, result in (
        ("vm", received(), ArtifactApplication(deferred=(deferred,))),
        ("vm", received(item), ArtifactApplication(deferred=(deferred, deferred))),
        ("workspace", received(item), ArtifactApplication(deferred=(deferred,))),
        ("session", received(item), ArtifactApplication(deferred=(deferred,))),
    ):
        with pytest.raises(StateError):
            validate_application(result, inputs, facet, integration="fixture")


def test_body_is_not_exposed_through_transport_log(target):
    content = b"private artifact fixture content"
    publish_artifacts(
        target, (artifact(target.home / "rule.md", content),), (), lambda files: None, roots=(str(target.home),)
    )
    assert content.decode() not in "".join(target.commands + target.logged_output)


def test_skill_retirement_prunes_only_owned_file_parents_and_retries_interruption(target, monkeypatch):
    from dataclasses import replace

    from agentworks.native_files import NativeFiles

    root = target.home / ".claude/skills/review"
    files = (
        replace(artifact(root / "SKILL.md"), native_identity="skill:review", package_root=str(root)),
        replace(artifact(root / "scripts/nested/check.sh"), native_identity="skill:review", package_root=str(root)),
    )
    previous = publish_artifacts(target, files, (), lambda files: None, roots=(str(target.home),)).files
    checkpoints = [previous]
    prune = NativeFiles.prune_empty_parents
    calls = 0

    def interrupted(self, destination, *, root):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("fixture interrupted cleanup")
        prune(self, destination, root=root)

    monkeypatch.setattr(NativeFiles, "prune_empty_parents", interrupted)
    with pytest.raises(StateError):
        publish_artifacts(target, (), previous, checkpoints.append, roots=(str(target.home),))
    # Supporting members are retired first; the absent entrypoint still records the exact root for retry.
    assert [Path(file.path).name for file in checkpoints[-1]] == ["SKILL.md"]
    assert not Path(checkpoints[-1][0].path).exists()
    monkeypatch.setattr(NativeFiles, "prune_empty_parents", prune)
    assert publish_artifacts(target, (), checkpoints[-1], checkpoints.append, roots=(str(target.home),)).files == ()
    assert not root.exists()
    assert root.parent.is_dir()


@pytest.mark.parametrize("rootless", ["none", "support", "all"])
def test_skill_retirement_preserves_modified_and_unowned_files(target, rootless):
    from dataclasses import replace

    root = target.home / ".claude/skills/review"
    files = tuple(
        replace(artifact(root / path), native_identity="skill:review", package_root=str(root))
        for path in ("SKILL.md", "scripts/modified.sh", "data/retired.txt")
    )
    previous = publish_artifacts(target, files, (), lambda files: None, roots=(str(target.home),)).files
    if rootless != "none":
        previous = tuple(
            record.model_copy(update={"package_root": None})
            if rootless == "all" or record.path.endswith("/modified.sh")
            else record
            for record in previous
        )
    (root / "scripts/modified.sh").write_text("operator change")
    (root / "data/unowned.txt").write_text("unowned content")
    remaining = publish_artifacts(target, (), previous, lambda files: None, roots=(str(target.home),)).files
    assert [Path(file.path).name for file in remaining] == ["SKILL.md", "modified.sh"]
    assert (root / "SKILL.md").exists()
    assert (root / "scripts/modified.sh").read_text() == "operator change"
    assert (root / "data/unowned.txt").read_text() == "unowned content"
    assert not (root / "data/retired.txt").exists()


def test_skill_parent_pruning_refuses_symlinks_and_scope_escape(target):
    from agentworks.native_files import NativeFiles

    root = target.home / ".claude/skills/review"
    root.mkdir(parents=True)
    outside = target.root / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    files = NativeFiles(target)
    with pytest.raises(StateError):
        files.prune_empty_parents(str(root / "linked/retired"), root=str(root))
    with pytest.raises(StateError):
        files.prune_empty_parents(str(outside / "retired"), root=str(root))
    assert outside.is_dir() and (root / "linked").is_symlink()


def test_recorded_package_root_never_prunes_same_named_ancestor_components(tmp_path):
    from agentworks.plugins.claude.artifacts import outer_artifacts
    from tests.artifacts.test_native_delivery import artifact as native_artifact

    target = LocalFixtureTransport(tmp_path / "skills/review")
    native_home = target.home / ".claude"
    package = native_home / "skills/review"
    plan = outer_artifacts(received(native_artifact(ArtifactType.SKILL)), str(native_home))
    records = publish_artifacts(target, plan.files, (), lambda files: None, roots=(str(target.home),)).files
    assert {file.package_root for file in records} == {str(package)}
    assert publish_artifacts(target, (), records, lambda files: None, roots=(str(target.home),)).files == ()
    assert not package.exists()
    assert native_home.is_dir() and package.parent.is_dir() and target.home.is_dir()


def test_rootless_records_without_package_root_retire_files_without_guessing(target):
    import hashlib

    root = target.home / ".claude/skills/review"
    root.mkdir(parents=True)
    path = root / "SKILL.md"
    path.write_bytes(b"rootless bytes")
    path.chmod(0o600)
    rootless = OwnedArtifactFile.model_validate(
        {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "origins": ["a" * 64],
            "native_identity": "skill:review",
        }
    )
    assert rootless.package_root is None
    assert publish_artifacts(target, (), (rootless,), lambda files: None, roots=(str(target.home),)).files == ()
    assert not path.exists() and root.is_dir()


@pytest.mark.parametrize("invalid", ["above-owner", "owner", "outside-package", "relative", "wrong-type"])
def test_package_root_is_validated_before_any_publication(target, invalid):
    from dataclasses import replace

    package = target.home / ".claude/skills/review"
    path = package / "SKILL.md"
    root = {
        "above-owner": str(target.root),
        "owner": str(target.home),
        "outside-package": str(target.home / "other"),
        "relative": "relative/package",
        "wrong-type": 42,
    }[invalid]
    malformed = replace(artifact(path), native_identity="skill:review", package_root=root)
    with pytest.raises(StateError):
        publish_artifacts(target, (malformed,), (), lambda files: None, roots=(str(target.home),))
    assert not path.exists() and target.commands == []


def test_persisted_package_root_cannot_escape_current_publication_scope(target):
    import hashlib

    path = target.home / ".claude/skills/review/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"owned")
    record = OwnedArtifactFile(
        path=str(path), sha256=hashlib.sha256(b"owned").hexdigest(), origins=("a" * 64,), package_root=str(target.root)
    )
    with pytest.raises(StateError):
        publish_artifacts(target, (), (record,), lambda files: None, roots=(str(target.home),))
    assert path.read_bytes() == b"owned" and target.commands == []


def test_rootless_skill_entrypoints_retire_deepest_first_after_supporting_members(target):
    from dataclasses import replace

    root = target.home / ".claude/skills/review"
    files = tuple(
        replace(artifact(root / path), native_identity="skill:review")
        for path in ("SKILL.md", "nested/SKILL.md", "nested/support.txt")
    )
    previous = publish_artifacts(target, files, (), lambda files: None, roots=(str(target.home),)).files
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    assert publish_artifacts(target, (), previous, checkpoints.append, roots=(str(target.home),)).files == ()
    assert [[Path(file.path).relative_to(root).as_posix() for file in records] for records in checkpoints] == [
        ["SKILL.md", "nested/SKILL.md"],
        ["SKILL.md"],
        [],
    ]
    assert root.is_dir() and (root / "nested").is_dir()


@pytest.mark.parametrize("unowned", [False, True])
def test_final_root_permission_denial_only_completes_when_directory_is_verified_empty(target, monkeypatch, unowned):
    from dataclasses import replace

    from agentworks.native_files import NativeFiles

    root = target.home / ".claude/skills/review"
    previous = publish_artifacts(
        target,
        (replace(artifact(root / "SKILL.md"), native_identity="skill:review", package_root=str(root)),),
        (),
        lambda files: None,
        roots=(str(target.home),),
    ).files
    if unowned:
        (root / "operator-note.txt").write_text("retain this")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.native_files.output.warn", warnings.append)
    checkpoints = [previous]
    remove = NativeFiles.remove
    removals = []

    def record_remove(self, destination, *, expected):
        removals.append(destination)
        remove(self, destination, expected=expected)

    monkeypatch.setattr(NativeFiles, "remove", record_remove)
    root.parent.chmod(0o500)
    try:
        if unowned:
            with pytest.raises(StateError):
                publish_artifacts(target, (), previous, checkpoints.append, roots=(str(target.home),))
            assert checkpoints[-1] == previous and not warnings
        else:
            assert publish_artifacts(target, (), previous, checkpoints.append, roots=(str(target.home),)).files == ()
            assert checkpoints[-1] == () and len(warnings) == 1
        assert not (root / "SKILL.md").exists() and root.is_dir()
    finally:
        root.parent.chmod(0o700)
    assert publish_artifacts(target, (), checkpoints[-1], checkpoints.append, roots=(str(target.home),)).files == ()
    assert removals == [str(root / "SKILL.md")]
    if unowned:
        assert (root / "operator-note.txt").read_text() == "retain this"


@pytest.mark.parametrize("invalid", [None, "unknown", "duplicate", "route", "file", "directory", "escape"])
def test_terminal_warning_follows_complete_session_validation(captured_output, invalid):
    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.session import validate_session_application

    item = ArtifactInput(
        ArtifactContent(ArtifactType.HINT, "setup", text="private hint"),
        ArtifactProvenance(),
        ArtifactOrigin("vm", "vm", "box", producer="core", bundle="team", entry="setup"),
    )
    context = SessionArtifactContext(
        inputs=received(item), home="/home/user", directory="/home/user/run", session_uuid="session", run_id="run"
    )
    deferred = ArtifactDeferral(input_id=item.identity, destination="session", reason="fixture-workaround")
    application = ArtifactApplication(deferred=(deferred,))
    if invalid in ("unknown", "duplicate", "route"):
        second = ArtifactDeferral(
            input_id="a" * 64 if invalid == "unknown" else item.identity,
            destination="user" if invalid == "route" else "session",
            reason="unsupported",
        )
        application = ArtifactApplication(deferred=(second,) if invalid == "route" else (deferred, second))
    elif invalid == "file":
        application = ArtifactApplication(
            deferred=(deferred,), files=(ArtifactFile("/home/user/run/file", b"body", ("a" * 64,)),)
        )
    elif invalid == "directory":
        application = ArtifactApplication(deferred=(deferred,), artifacts_dir="/home/user/other-run")
    elif invalid == "escape":
        application = ArtifactApplication(
            deferred=(deferred,), files=(ArtifactFile("/home/user/shared", b"body", (item.origin_identity,)),)
        )
    if invalid is not None:
        with pytest.raises(StateError):
            validate_session_application(application, context, integration="fixture")
        assert not captured_output.warnings
    else:
        assert validate_session_application(application, context, integration="fixture") == application
        assert len(captured_output.warnings) == 1
        warning = captured_output.warnings[0]
        for value in ("fixture", item.content.name, item.origin.resource_name, item.origin.bundle, deferred.reason):
            assert value in warning
        assert item.content.text not in warning
