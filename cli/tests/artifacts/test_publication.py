"""Whole-file artifact ownership against an isolated real filesystem transport."""

from __future__ import annotations

import sys
from dataclasses import replace
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
    first = publish_artifacts(
        target.files(), (artifact(path),), (), checkpoints.append, roots=(str(target.home),)
    ).files
    assert path.read_bytes() == b"content"
    before = path.stat().st_mtime_ns
    same = publish_artifacts(
        target.files(), (artifact(path),), first, checkpoints.append, roots=(str(target.home),)
    ).files
    assert same == first and path.stat().st_mtime_ns == before
    updated = publish_artifacts(
        target.files(),
        (artifact(path, b"#!/bin/sh\n", executable=True),),
        first,
        checkpoints.append,
        roots=(str(target.home),),
    ).files
    assert path.stat().st_mode & 0o777 == 0o700
    assert len(checkpoints) == 2
    assert publish_artifacts(target.files(), (), updated, checkpoints.append, roots=(str(target.home),)).files == ()
    assert not path.exists()
    assert publish_artifacts(target.files(), (), updated, checkpoints.append, roots=(str(target.home),)).files == ()


def test_conflicting_artifact_does_not_block_an_independent_artifact(target):
    first, second = target.home / "first", target.home / "second"
    second.write_bytes(b"operator content")
    result = publish_artifacts(
        target.files(),
        (artifact(first), replace(artifact(second), origins=("b" * 64,))),
        (),
        lambda files: None,
        roots=(str(target.home),),
    )
    assert first.read_bytes() == b"content" and second.read_bytes() == b"operator content"
    assert tuple(file.path for file in result.files) == (str(first),)
    assert tuple(file.path for file in result.skipped) == (str(second),)


def test_matching_unclaimed_file_is_adopted_and_retired(target, monkeypatch):
    path = target.home / "matching"
    path.write_bytes(b"content")
    path.chmod(0o600)
    before = path.stat().st_mtime_ns
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.info", reports.append)
    result = publish_artifacts(target.files(), (artifact(path),), (), checkpoints.append, roots=(str(target.home),))
    assert len(result.files) == 1 and checkpoints == [result.files]
    assert path.stat().st_mtime_ns == before and not result.skipped
    assert len(reports) == 1 and str(path) in reports[0]
    assert not publish_artifacts(target.files(), (), result.files, checkpoints.append, roots=(str(target.home),)).files
    assert not path.exists()
    assert len(reports) == 2 and str(path) in reports[-1]


@pytest.mark.parametrize("count", [1, 3, 5])
def test_owned_file_retirement_reports_a_bounded_confirmed_summary(target, monkeypatch, count):
    paths = tuple(target.home / f"retired-{chr(97 + index)}" for index in range(count))
    previous = publish_artifacts(
        target.files(), tuple(artifact(path) for path in paths), (), lambda files: None, roots=(str(target.home),)
    ).files
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.info", reports.append)
    result = publish_artifacts(target.files(), (), previous, lambda files: None, roots=(str(target.home),))
    assert not result.files and all(not path.exists() for path in paths)
    assert len(reports) == 1
    for index, path in enumerate(paths):
        assert (str(path) in reports[0]) == (index < (count if count <= 3 else 2))
    reports.clear()
    publish_artifacts(target.files(), (), result.files, lambda files: None, roots=(str(target.home),))
    assert not reports


def test_owned_file_replacement_reports_confirmed_change_only(target, monkeypatch):
    path = target.home / "replacement"
    previous = publish_artifacts(
        target.files(), (artifact(path),), (), lambda files: None, roots=(str(target.home),)
    ).files
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.info", reports.append)
    desired = (artifact(path, b"new content"),)
    result = publish_artifacts(target.files(), desired, previous, lambda files: None, roots=(str(target.home),))
    assert path.read_bytes() == b"new content" and len(reports) == 1
    assert str(path) in reports[0]
    reports.clear()
    publish_artifacts(target.files(), desired, result.files, lambda files: None, roots=(str(target.home),))
    assert not reports


def test_generated_section_retirement_reports_without_removing_operator_content(target, monkeypatch):
    path = target.home / "AGENTS.md"
    original = b"Operator guidance.\n"
    path.write_bytes(original)
    desired = (replace(artifact(path), generated_section=True),)
    previous = publish_artifacts(target.files(), desired, (), lambda files: None, roots=(str(target.home),)).files
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.info", reports.append)
    result = publish_artifacts(target.files(), (), previous, lambda files: None, roots=(str(target.home),))
    remaining = path.read_bytes()
    assert not result.files and remaining.startswith(original)
    from agentworks.artifacts.sections import BEGIN, END

    assert remaining[len(original) :] == BEGIN + b"\n" + END + b"\n"
    assert len(reports) == 1 and str(path) in reports[0]


def test_retained_modified_file_is_not_reported_as_removed(target, monkeypatch):
    path = target.home / "retained"
    previous = publish_artifacts(
        target.files(), (artifact(path),), (), lambda files: None, roots=(str(target.home),)
    ).files
    path.write_bytes(b"operator change")
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.info", reports.append)
    result = publish_artifacts(target.files(), (), previous, lambda files: None, roots=(str(target.home),))
    assert result.files == previous and path.read_bytes() == b"operator change"
    assert not reports


def test_conflicting_skill_member_preserves_the_whole_package(target):
    root = target.home / "skills/review"
    root.mkdir(parents=True)
    member = root / "run.sh"
    member.write_bytes(b"operator script")
    desired = tuple(replace(artifact(root / name), package_root=str(root)) for name in ("SKILL.md", "run.sh"))
    result = publish_artifacts(target.files(), desired, (), lambda files: None, roots=(str(target.home),))
    assert not result.files
    assert {entry.path for entry in result.skipped} == {entry.path for entry in desired}
    assert not (root / "SKILL.md").exists() and member.read_bytes() == b"operator script"


@pytest.mark.parametrize("rootless", [False, True])
def test_modified_obsolete_member_preserves_requested_skill_as_a_unit(target, rootless, monkeypatch):
    root = target.home / "skills/review"

    def member(name: str, data: bytes = b"old") -> ArtifactFile:
        return replace(
            artifact(root / name, data),
            package_root=None if rootless else str(root),
            native_identity="skill:review",
        )

    previous = publish_artifacts(
        target.files(), (member("SKILL.md"), member("old.sh")), (), lambda files: None, roots=(str(target.home),)
    ).files
    (root / "old.sh").write_bytes(b"operator script")
    independent = replace(artifact(target.home / "independent", b"independent"), origins=("b" * 64,))
    desired = (member("SKILL.md", b"new instructions"), member("new.sh", b"new script"), independent)
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.warn", warnings.append)
    result = publish_artifacts(target.files(), desired, previous, lambda files: None, roots=(str(target.home),))
    assert len(warnings) == 1 and str(root / "old.sh") in warnings[0]
    assert (root / "SKILL.md").read_bytes() == b"old"
    assert (root / "old.sh").read_bytes() == b"operator script"
    assert not (root / "new.sh").exists()
    assert Path(independent.path).read_bytes() == independent.data
    assert {entry.path for entry in result.skipped} == {str(root / name) for name in ("SKILL.md", "old.sh", "new.sh")}
    assert {file.path for file in result.files} == {file.path for file in previous} | {independent.path}
    # Resolving the local modification makes retry converge the complete package.
    (root / "old.sh").write_bytes(b"old")
    retried = publish_artifacts(target.files(), desired, result.files, lambda files: None, roots=(str(target.home),))
    assert not retried.skipped and not (root / "old.sh").exists()
    assert (root / "SKILL.md").read_bytes() == b"new instructions"
    assert (root / "new.sh").read_bytes() == b"new script"


def test_modified_obsolete_file_retains_ownership(target):
    path = target.home / "managed"
    owned = publish_artifacts(
        target.files(), (artifact(path),), (), lambda files: None, roots=(str(target.home),)
    ).files
    path.write_bytes(b"operator edit")
    assert publish_artifacts(target.files(), (), owned, lambda files: None, roots=(str(target.home),)).files == owned
    assert path.read_bytes() == b"operator edit"
    result = publish_artifacts(
        target.files(), (artifact(path, b"update"),), owned, lambda files: None, roots=(str(target.home),)
    )
    assert result.files == owned and result.skipped[0].path == str(path)
    assert path.read_bytes() == b"operator edit"


def test_modified_executable_intent_is_preserved_and_diagnosed(target):
    path = target.home / "managed"
    owned = publish_artifacts(
        target.files(), (artifact(path),), (), lambda files: None, roots=(str(target.home),)
    ).files
    path.chmod(0o700)
    result = publish_artifacts(target.files(), (artifact(path),), owned, lambda files: None, roots=(str(target.home),))
    assert result.files == owned and result.skipped[0].path == str(path)
    assert path.stat().st_mode & 0o777 == 0o700


def test_matching_unclaimed_content_converges_permissions_without_changing_executable_intent(target):
    path = target.home / "matching"
    path.write_bytes(b"content")
    path.chmod(0o644)
    result = publish_artifacts(target.files(), (artifact(path),), (), lambda files: None, roots=(str(target.home),))
    assert len(result.files) == 1 and not result.skipped
    assert path.read_bytes() == b"content" and path.stat().st_mode & 0o777 == 0o600


def test_checkpoint_failure_stops_further_publication(target):
    first, second = target.home / "first", target.home / "second"

    def unavailable(files):
        assert len(files) == 1
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        publish_artifacts(
            target.files(), (artifact(first), artifact(second)), (), unavailable, roots=(str(target.home),)
        )
    assert first.exists() and not second.exists()
    assert not list((target.root / "tmp").iterdir())
    retried = publish_artifacts(
        target.files(), (artifact(first), artifact(second)), (), lambda files: None, roots=(str(target.home),)
    )
    assert len(retried.files) == 2 and not retried.skipped


def test_scope_escape_and_links_are_refused(target):
    outside = target.root / "outside"
    outside.mkdir()
    with pytest.raises(StateError):
        publish_artifacts(
            target.files(), (artifact(outside / "file"),), (), lambda files: None, roots=(str(target.home),)
        )
    (target.home / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError):
        publish_artifacts(
            target.files(), (artifact(target.home / "link/file"),), (), lambda files: None, roots=(str(target.home),)
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


def test_skill_retirement_prunes_only_owned_file_parents_and_retries_interruption(target, monkeypatch):
    from dataclasses import replace

    from agentworks.execution.files import Files

    root = target.home / ".claude/skills/review"
    files = (
        replace(artifact(root / "SKILL.md"), native_identity="skill:review", package_root=str(root)),
        replace(artifact(root / "scripts/nested/check.sh"), native_identity="skill:review", package_root=str(root)),
    )
    previous = publish_artifacts(target.files(), files, (), lambda files: None, roots=(str(target.home),)).files
    checkpoints = [previous]
    prune = Files.prune_empty_parents
    calls = 0

    def interrupted(self, destination, *, root, elevated=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("fixture interrupted cleanup")
        return prune(self, destination, root=root, elevated=elevated)

    monkeypatch.setattr(Files, "prune_empty_parents", interrupted)
    with pytest.raises(StateError):
        publish_artifacts(target.files(), (), previous, checkpoints.append, roots=(str(target.home),))
    # Supporting members are retired first; the absent entrypoint still records the exact root for retry.
    assert [Path(file.path).name for file in checkpoints[-1]] == ["SKILL.md"]
    assert not Path(checkpoints[-1][0].path).exists()
    monkeypatch.setattr(Files, "prune_empty_parents", prune)
    assert (
        publish_artifacts(target.files(), (), checkpoints[-1], checkpoints.append, roots=(str(target.home),)).files
        == ()
    )
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
    previous = publish_artifacts(target.files(), files, (), lambda files: None, roots=(str(target.home),)).files
    if rootless != "none":
        previous = tuple(
            record.model_copy(update={"package_root": None})
            if rootless == "all" or record.path.endswith("/modified.sh")
            else record
            for record in previous
        )
    (root / "scripts/modified.sh").write_text("operator change")
    (root / "data/unowned.txt").write_text("unowned content")
    remaining = publish_artifacts(target.files(), (), previous, lambda files: None, roots=(str(target.home),)).files
    assert [Path(file.path).name for file in remaining] == ["SKILL.md", "modified.sh"]
    assert (root / "SKILL.md").exists()
    assert (root / "scripts/modified.sh").read_text() == "operator change"
    assert (root / "data/unowned.txt").read_text() == "unowned content"
    assert not (root / "data/retired.txt").exists()


def test_skill_parent_pruning_refuses_symlinks_and_scope_escape(target):
    root = target.home / ".claude/skills/review"
    root.mkdir(parents=True)
    outside = target.root / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    files = target.files()
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
    records = publish_artifacts(target.files(), plan.files, (), lambda files: None, roots=(str(target.home),)).files
    assert {file.package_root for file in records} == {str(package)}
    assert publish_artifacts(target.files(), (), records, lambda files: None, roots=(str(target.home),)).files == ()
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
    assert publish_artifacts(target.files(), (), (rootless,), lambda files: None, roots=(str(target.home),)).files == ()
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
    access = target.access()
    with pytest.raises(StateError):
        publish_artifacts(access.files(), (malformed,), (), lambda files: None, roots=(str(target.home),))
    assert not path.exists() and access.requests() == []


def test_persisted_package_root_cannot_escape_current_publication_scope(target):
    import hashlib

    path = target.home / ".claude/skills/review/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"owned")
    record = OwnedArtifactFile(
        path=str(path), sha256=hashlib.sha256(b"owned").hexdigest(), origins=("a" * 64,), package_root=str(target.root)
    )
    access = target.access()
    with pytest.raises(StateError):
        publish_artifacts(access.files(), (), (record,), lambda files: None, roots=(str(target.home),))
    assert path.read_bytes() == b"owned" and access.requests() == []


def test_rootless_skill_entrypoints_retire_deepest_first_after_supporting_members(target):
    from dataclasses import replace

    root = target.home / ".claude/skills/review"
    files = tuple(
        replace(artifact(root / path), native_identity="skill:review")
        for path in ("SKILL.md", "nested/SKILL.md", "nested/support.txt")
    )
    previous = publish_artifacts(target.files(), files, (), lambda files: None, roots=(str(target.home),)).files
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    assert publish_artifacts(target.files(), (), previous, checkpoints.append, roots=(str(target.home),)).files == ()
    assert [[Path(file.path).relative_to(root).as_posix() for file in records] for records in checkpoints] == [
        ["SKILL.md", "nested/SKILL.md"],
        ["SKILL.md"],
        [],
    ]
    assert root.is_dir() and (root / "nested").is_dir()


@pytest.mark.parametrize("unowned", [False, True])
def test_final_root_permission_denial_only_completes_when_directory_is_verified_empty(target, monkeypatch, unowned):
    from dataclasses import replace

    from agentworks.execution.files import Files

    root = target.home / ".claude/skills/review"
    previous = publish_artifacts(
        target.files(),
        (replace(artifact(root / "SKILL.md"), native_identity="skill:review", package_root=str(root)),),
        (),
        lambda files: None,
        roots=(str(target.home),),
    ).files
    if unowned:
        (root / "operator-note.txt").write_text("retain this")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.publication.output.warn", warnings.append)
    checkpoints = [previous]
    remove = Files.remove_if
    removals = []

    def record_remove(self, destination, *, expected, elevated=False):
        removals.append(destination)
        remove(self, destination, expected=expected, elevated=elevated)

    monkeypatch.setattr(Files, "remove_if", record_remove)
    root.parent.chmod(0o500)
    try:
        if unowned:
            with pytest.raises(StateError):
                publish_artifacts(target.files(), (), previous, checkpoints.append, roots=(str(target.home),))
            assert checkpoints[-1] == previous and not warnings
        else:
            assert (
                publish_artifacts(target.files(), (), previous, checkpoints.append, roots=(str(target.home),)).files
                == ()
            )
            assert checkpoints[-1] == () and len(warnings) == 1
        assert not (root / "SKILL.md").exists() and root.is_dir()
    finally:
        root.parent.chmod(0o700)
    assert (
        publish_artifacts(target.files(), (), checkpoints[-1], checkpoints.append, roots=(str(target.home),)).files
        == ()
    )
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


@pytest.mark.parametrize("previous", [False, True])
def test_a_directory_where_a_file_belongs_is_refused_before_any_write(target, previous):
    from agentworks.execution.errors import PathRefusal, PathRefused

    occupied, independent = target.home / "occupied", target.home / "independent"
    occupied.mkdir()
    desired = (artifact(independent), artifact(occupied))
    prior = (OwnedArtifactFile(path=str(occupied), sha256="b" * 64, origins=("a" * 64,)),) if previous else ()
    with pytest.raises(PathRefused) as refused:
        publish_artifacts(target.files(), desired, prior, lambda files: None, roots=(str(target.home),))
    assert (refused.value.path, refused.value.reason) == (str(occupied), PathRefusal.UNSUITABLE)
    assert occupied.is_dir() and not independent.exists()
