"""Workspace collaboration and native publication against actual POSIX ACLs."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.publication import publish_artifacts
from agentworks.workspaces.acls import apply_workspace_acls
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("setfacl") is None or shutil.which("getfacl") is None,
    reason="requires Linux POSIX ACL tools",
)


def fixture_group() -> str:
    return subprocess.run(["id", "-gn"], check=True, capture_output=True, text=True).stdout.strip()


def snapshot(path: Path) -> str:
    return subprocess.run(["getfacl", "-R", "-n", "-p", str(path)], check=True, capture_output=True, text=True).stdout


def test_cloned_artifacts_adopt_and_first_repair_keeps_published_file_acls(tmp_path):
    target = LocalFixtureTransport(tmp_path / "target")
    parent = target.home / "workspaces"
    parent.mkdir()
    parent.chmod(0o2770)
    apply_workspace_acls(target.files(), str(parent))
    source = target.root / "source"
    source.mkdir()
    content = {
        ".claude/rules/review.md": (b"fixture rule", False, "a" * 64),
        ".claude/agents/reviewer.md": (b"fixture agent", False, "b" * 64),
        ".codex/skills/review/SKILL.md": (b"fixture skill", False, "c" * 64),
        ".codex/skills/review/run.sh": (b"#!/bin/sh\nexit 0\n", True, "c" * 64),
    }
    for name, (data, executable, _) in content.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if executable else 0o644)
    for command in (
        f"git init -q {shlex.quote(str(source))}",
        f"git -C {shlex.quote(str(source))} add .",
        f"git -C {shlex.quote(str(source))} "
        "-c user.name=Fixture -c user.email=fixture@example.invalid commit -qm fixture",
    ):
        assert target.run(command).ok
    workspace = parent / "project"
    assert target.run(f"git clone -- {shlex.quote(str(source))} {shlex.quote(str(workspace))}").ok
    for directory in (workspace, *(entry for entry in workspace.rglob("*") if entry.is_dir())):
        directory.chmod(directory.stat().st_mode | 0o2000)
    apply_workspace_acls(target.files(), str(workspace))
    desired = tuple(
        ArtifactFile(
            str(workspace / name),
            data,
            (origin,),
            executable=executable,
            package_root=str(workspace / ".codex/skills/review") if origin == "c" * 64 else None,
        )
        for name, (data, executable, origin) in content.items()
    )
    before = {item.path: Path(item.path).stat() for item in desired}
    checkpoints: list[tuple[OwnedArtifactFile, ...]] = []
    group = fixture_group()
    adopted = publish_artifacts(target.files(), desired, (), checkpoints.append, roots=(str(workspace),), group=group)
    assert not adopted.skipped and len(adopted.files) == len(content)
    for item in desired:
        observed = Path(item.path).stat()
        assert (observed.st_ino, observed.st_mtime_ns) == (before[item.path].st_ino, before[item.path].st_mtime_ns)
        assert observed.st_mode & 0o777 == (0o770 if item.executable else 0o660)
        assert os.access(item.path, os.R_OK | os.W_OK)
        assert os.access(item.path, os.X_OK) == item.executable
    assert target.run(shlex.quote(str(workspace / ".codex/skills/review/run.sh"))).ok
    new_file = workspace / ".codex/rules/new.md"
    updated = publish_artifacts(
        target.files(),
        (*desired, ArtifactFile(str(new_file), b"new fixture rule", ("d" * 64,))),
        adopted.files,
        checkpoints.append,
        roots=(str(workspace),),
        group=group,
    )
    assert not updated.skipped and new_file.stat().st_mode & 0o777 == 0o660
    acl_before = snapshot(workspace)
    apply_workspace_acls(target.files(), str(workspace))
    assert snapshot(workspace) == acl_before
    for entry in (workspace, *workspace.rglob("*")):
        assert entry.stat().st_gid == os.getgid()
        assert entry.stat().st_mode & 0o007 == 0
        if entry.is_dir():
            assert entry.stat().st_mode & 0o2070 == 0o2070
            assert "default:other::---" in snapshot(entry)


@pytest.mark.parametrize("original_mode", [0o644, 0o755, 0o650])
def test_access_mask_preserves_effective_executable_intent_and_is_idempotent(tmp_path, original_mode):
    target = LocalFixtureTransport(tmp_path / "target")
    workspace = target.home / "workspace"
    workspace.mkdir()
    workspace.chmod(0o2770)
    apply_workspace_acls(target.files(), str(workspace))
    path = workspace / "file"
    path.write_bytes(b"fixture")
    path.chmod(original_mode)
    apply_workspace_acls(target.files(), str(workspace))
    assert bool(path.stat().st_mode & 0o111) == bool(original_mode & 0o111)
    assert path.stat().st_mode & 0o070 == (0o070 if original_mode & 0o111 else 0o060)
    assert path.stat().st_mode & 0o007 == 0
    before = snapshot(workspace)
    apply_workspace_acls(target.files(), str(workspace))
    assert snapshot(workspace) == before


@pytest.mark.parametrize("executable", [False, True])
def test_differing_executable_intent_is_retained_after_workspace_acl_application(tmp_path, executable):
    target = LocalFixtureTransport(tmp_path / "target")
    path = target.home / "rule.md"
    path.write_bytes(b"fixture")
    path.chmod(0o644 if executable else 0o755)
    apply_workspace_acls(target.files(), str(target.home))
    before = path.stat()
    result = publish_artifacts(
        target.files(),
        (ArtifactFile(str(path), b"fixture", ("a" * 64,), executable=executable),),
        (),
        lambda files: None,
        roots=(str(target.home),),
        group=fixture_group(),
    )
    assert not result.files and len(result.skipped) == 1
    assert result.skipped[0].path == str(path)
    assert path.read_bytes() == b"fixture"
    assert path.stat().st_mode == before.st_mode and path.stat().st_ino == before.st_ino


def test_workspace_acl_does_not_follow_a_link_outside_the_tree(tmp_path):
    target = LocalFixtureTransport(tmp_path / "target")
    workspace = target.home / "workspace"
    workspace.mkdir()
    outside = target.home / "outside"
    outside.write_bytes(b"fixture")
    outside.chmod(0o644)
    (workspace / "link").symlink_to(outside)
    apply_workspace_acls(target.files(), str(workspace))
    assert outside.stat().st_mode & 0o777 == 0o644


def test_parent_acl_repair_prunes_private_clone_attempt_trees(tmp_path):
    target = LocalFixtureTransport(tmp_path / "target")
    parent = target.home / "workspaces"
    parent.mkdir()
    apply_workspace_acls(target.files(), str(parent))
    workspace = parent / "project"
    workspace.mkdir()
    ordinary = workspace / "tracked"
    ordinary.write_bytes(b"shared")
    ordinary.chmod(0o600)
    attempt = parent / ("project.agentworks-clone-" + "a" * 32)
    attempt.mkdir(mode=0o700)
    checkout = attempt / "checkout"
    checkout.mkdir(mode=0o700)
    private = checkout / "private"
    private.write_bytes(b"private")
    private.chmod(0o600)
    executable = checkout / "tool"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o700)
    before = snapshot(attempt)

    apply_workspace_acls(target.files(), str(parent))

    assert snapshot(attempt) == before
    assert attempt.stat().st_mode & 0o077 == 0
    assert private.stat().st_mode & 0o077 == 0
    assert ordinary.stat().st_mode & 0o770 == 0o660
    assert "default:group::rwx" in snapshot(workspace)
