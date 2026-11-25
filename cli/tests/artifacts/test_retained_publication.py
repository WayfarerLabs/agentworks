"""Unresolved inherited routes retain confirmed placement while independent files publish."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.publication import publish_artifacts
from agentworks.artifacts.sections import BEGIN, END
from tests.native_setup_fixtures import LocalFixtureAccount
from tests.vm_access_support import Admin, RecordingVMAccess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


@pytest.mark.parametrize("generated_section", [False, True])
@pytest.mark.parametrize("plan", ["absent", "changed", "same"])
def test_unresolved_routes_hold_previous_files_and_sections_without_inspecting_them(
    tmp_path, monkeypatch, generated_section, plan
):
    target = LocalFixtureAccount(tmp_path / "target")
    path = target.home / "AGENTS.md"
    if generated_section:
        path.write_bytes(b"operator prefix\n")
    item = ArtifactFile(str(path), b"managed fixture", ("a" * 64,), generated_section=generated_section)
    previous = publish_artifacts(target.files(), (item,), (), lambda files: None, roots=(str(target.home),)).files
    before, metadata = path.read_bytes(), path.stat()
    desired = () if plan == "absent" else (replace(item, data=b"changed" if plan == "changed" else item.data),)
    checkpoint = Mock()
    no_io = RecordingVMAccess([], Admin())
    no_io.default_outcome = AssertionError("no I/O")
    result = publish_artifacts(
        no_io.files(),
        desired,
        previous,
        checkpoint,
        roots=(str(target.home),),
        retain_origins=frozenset({"a" * 64, "b" * 64}),
    )
    assert result.files == previous and len(result.skipped) == 1
    assert result.skipped[0].path == str(path) and result.skipped[0].origins == item.origins
    assert path.read_bytes() == before and path.stat().st_ino == metadata.st_ino
    assert path.stat().st_mtime_ns == metadata.st_mtime_ns
    checkpoint.assert_not_called()


@pytest.mark.parametrize("legacy_package", [False, True])
def test_unresolved_routes_hold_package_updates_and_new_members_but_allow_independent_files(tmp_path, legacy_package):
    target = LocalFixtureAccount(tmp_path / "target")
    package = target.home / "skills/review"
    entrypoint = ArtifactFile(
        str(package / "SKILL.md"),
        b"old skill",
        ("a" * 64,),
        native_identity="skill:review",
        package_root=None if legacy_package else str(package),
    )
    supporting = replace(entrypoint, path=str(package / "run.sh"), data=b"old script", executable=True)
    previous = publish_artifacts(
        target.files(), (entrypoint, supporting), (), lambda files: None, roots=(str(target.home),)
    ).files
    snapshots = {file.path: (Path(file.path).read_bytes(), Path(file.path).stat()) for file in previous}
    moved = target.home / "replacement/review"
    new_entrypoint = replace(entrypoint, path=str(moved / "SKILL.md"), package_root=str(moved), origins=("d" * 64,))
    new_member = ArtifactFile(str(package / "new.sh"), b"new script", ("b" * 64,), executable=True)
    independent = ArtifactFile(str(target.home / "independent.md"), b"independent", ("c" * 64,))
    desired = (new_entrypoint, new_member, independent)
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    result = publish_artifacts(
        target.files(),
        desired,
        previous,
        checkpoints.append,
        roots=(str(target.home),),
        retain_origins=frozenset({"a" * 64, "b" * 64}),
    )
    assert result.files[: len(previous)] == previous
    assert {file.path for file in result.files} == {file.path for file in previous} | {independent.path}
    assert {skip.path for skip in result.skipped} == {
        entrypoint.path,
        supporting.path,
        new_entrypoint.path,
        new_member.path,
    }
    assert not moved.exists() and not Path(new_member.path).exists()
    assert Path(independent.path).read_bytes() == independent.data
    assert checkpoints == [result.files]
    for path, (data, metadata) in snapshots.items():
        assert Path(path).read_bytes() == data and Path(path).stat().st_ino == metadata.st_ino
        assert Path(path).stat().st_mtime_ns == metadata.st_mtime_ns
    # Once inputs resolve, ordinary convergence can update the package and retire old members.
    healed = publish_artifacts(
        target.files(),
        desired,
        result.files,
        checkpoints.append,
        roots=(str(target.home),),
    )
    assert not healed.skipped and not Path(supporting.path).exists()
    assert not Path(entrypoint.path).exists() and Path(new_entrypoint.path).read_bytes() == new_entrypoint.data
    assert Path(new_member.path).read_bytes() == new_member.data


def test_retained_aggregate_section_does_not_drop_unresolved_members_when_new_inputs_publish(tmp_path):
    target = LocalFixtureAccount(tmp_path / "target")
    path = target.home / "AGENTS.md"
    path.write_bytes(b"prefix\n")
    item = ArtifactFile(str(path), b"old rule and inherited hint", ("a" * 64, "b" * 64), generated_section=True)
    previous = publish_artifacts(target.files(), (item,), (), lambda files: None, roots=(str(target.home),)).files
    path.write_bytes(path.read_bytes() + b"suffix\n")
    before = path.read_bytes()
    independent = ArtifactFile(str(target.home / "new-rule.md"), b"new fixture rule", ("d" * 64,))
    result = publish_artifacts(
        target.files(),
        (replace(item, data=b"updated local rules", origins=("a" * 64, "c" * 64)), independent),
        previous,
        lambda files: None,
        roots=(str(target.home),),
        retain_origins=frozenset({"a" * 64, "b" * 64}),
    )
    assert path.read_bytes() == before
    assert BEGIN in before and END in before
    assert result.files[0] == previous[0]
    assert result.skipped[0].origins == (*item.origins, "c" * 64)
    assert Path(independent.path).read_bytes() == independent.data
