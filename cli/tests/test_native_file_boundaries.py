"""Elevated native operations are confined before any payload staging or dispatch."""

from __future__ import annotations

import hashlib
import sys
from unittest.mock import Mock

import pytest

import agentworks.native_files as native
from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.publication import publish_artifacts
from agentworks.errors import StateError
from agentworks.transports import Transport
from tests.native_setup_fixtures import LocalFixtureTransport


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/etc",
        "/etc/shadow",
        "/root/private",
        "/etc/codex-other/rule",
        "/opt/agentworks/artifacts-other/rule",
        "/etc/codex/../shadow",
        "/etc/codex//rule",
        "/etc/codex/./rule",
        "etc/codex/rule",
    ],
)
@pytest.mark.parametrize("operation", ["read", "fingerprint", "directory", "mkdir", "publish", "remove", "prune"])
def test_disallowed_root_operations_do_not_stage_or_dispatch(path, operation, monkeypatch):
    runner = Mock(spec=Transport)
    files = native.NativeFiles(runner, root=True)
    slot = Mock(side_effect=AssertionError("must reject before staging"))
    monkeypatch.setattr(files, "slot", slot)
    with pytest.raises(StateError):
        if operation == "publish":
            files.publish(path, b"private payload", expected=None)
        elif operation == "remove":
            files.remove(path, expected="a" * 64)
        elif operation == "prune":
            files.prune_empty_parents(path, root="/etc/codex/skills")
        elif operation == "mkdir":
            files.directory(path, create=True)
        else:
            getattr(files, operation)(path)
    assert runner.mock_calls == []
    slot.assert_not_called()


@pytest.mark.parametrize("boundary", native.ROOT_FILE_DIRECTORIES)
def test_allowed_trees_accept_directories_and_descendants_but_not_boundary_files(boundary):
    runner = Mock(spec=Transport)
    runner.run.return_value.ok = True
    runner.run.return_value.stdout = '{"exists": false}'
    files = native.NativeFiles(runner, root=True)
    assert not files.directory(boundary)
    assert not files.directory(boundary, create=True)
    assert files.fingerprint(boundary + "/rule.md") is None
    runner.reset_mock()
    with pytest.raises(StateError):
        files.publish(boundary, b"cannot replace boundary", expected=None)
    with pytest.raises(StateError):
        files.remove(boundary, expected="a" * 64)
    with pytest.raises(StateError):
        files.prune_empty_parents(boundary + "/rule.md", root=boundary)
    assert runner.mock_calls == []


@pytest.mark.parametrize("package_root", ["/", "/etc", "/etc/codex", "/etc/codex-other", "/root"])
def test_root_pruning_refuses_cleanup_outside_boundary_before_dispatch(package_root):
    runner = Mock(spec=Transport)
    with pytest.raises(StateError):
        native.NativeFiles(runner, root=True).prune_empty_parents(
            "/etc/codex/skills/review/SKILL.md", root=package_root
        )
    assert runner.mock_calls == []


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize(
    ("path", "package_root"),
    [
        ("/etc/shadow", None),
        ("/etc/codex", None),
        ("/etc/codex-other/rule.md", None),
        ("/etc/codex/skills/review/SKILL.md", "/etc"),
        ("/etc/codex/skills/review/SKILL.md", "/etc/codex"),
    ],
)
def test_publication_rejects_desired_and_persisted_root_escape_before_staging(path, package_root, previous):
    runner = Mock(spec=Transport)
    checkpoint = Mock()
    desired = ArtifactFile(path, b"rule", ("a" * 64,), package_root=package_root)
    prior = OwnedArtifactFile(path=path, sha256="b" * 64, origins=("a" * 64,), package_root=package_root)
    # Even a caller-supplied broader scope cannot enlarge the global boundary.
    with pytest.raises(StateError):
        publish_artifacts(
            runner,
            () if previous else (desired,),
            (prior,) if previous else (),
            checkpoint,
            roots=("/etc",),
            root=True,
        )
    assert runner.mock_calls == []
    checkpoint.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")
def test_allowed_root_lifecycle_keeps_boundary_and_refuses_symlinks(tmp_path, monkeypatch):
    target = LocalFixtureTransport(tmp_path / "target")
    boundary = target.root / "machine/artifacts"
    monkeypatch.setattr(native, "ROOT_FILE_DIRECTORIES", (str(boundary),))
    run = target.run
    monkeypatch.setattr(target, "run", lambda command, **kwargs: run(command.removeprefix("sudo -n -- "), **kwargs))
    package = boundary / "skills/review"
    path = package / "SKILL.md"
    outside = target.home / "outside"
    outside.mkdir()
    with native.NativeFiles(target, root=True) as files:
        assert not files.directory(str(boundary))
        assert files.directory(str(boundary), create=True)
        files.publish(str(path), b"skill", expected=None)
        digest = hashlib.sha256(b"skill").hexdigest()
        assert files.read(str(path)) == b"skill"
        assert files.fingerprint(str(path)) == (digest, 0o644)
        files.remove(str(path), expected=digest)
        files.prune_empty_parents(str(path), root=str(package))
        assert not package.exists()
        assert boundary.is_dir()
        (boundary / "link").symlink_to(outside, target_is_directory=True)
        for operation in (files.read, files.fingerprint):
            with pytest.raises(StateError):
                operation(str(boundary / "link/private"))
        with pytest.raises(StateError):
            files.publish(str(boundary / "link/private"), b"refused", expected=None)
        assert not list(outside.iterdir())
    assert not list((target.root / "tmp").iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")
def test_unprivileged_operations_keep_normal_scope_access(tmp_path):
    target = LocalFixtureTransport(tmp_path / "target")
    package = target.home / "skills/review"
    path = package / "SKILL.md"
    with native.NativeFiles(target) as files:
        assert files.directory(str(package), create=True)
        files.publish(str(path), b"skill", expected=None)
        digest = hashlib.sha256(b"skill").hexdigest()
        assert files.read(str(path)) == b"skill"
        assert files.fingerprint(str(path)) == (digest, 0o600)
        files.remove(str(path), expected=digest)
        files.prune_empty_parents(str(path), root=str(package))
    assert not package.exists()
    assert target.home.is_dir()
