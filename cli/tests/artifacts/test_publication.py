"""Whole-file artifact ownership against an isolated real filesystem transport."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactContent, ArtifactInput, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.artifacts.publication import publish_artifacts, validate_application
from agentworks.errors import StateError
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
    first = publish_artifacts(target, (artifact(path),), (), checkpoints.append, roots=(str(target.home),))
    assert path.read_bytes() == b"content"
    before = path.stat().st_mtime_ns
    same = publish_artifacts(target, (artifact(path),), first, checkpoints.append, roots=(str(target.home),))
    assert same == first and path.stat().st_mtime_ns == before
    updated = publish_artifacts(
        target,
        (artifact(path, b"#!/bin/sh\n", executable=True),),
        first,
        checkpoints.append,
        roots=(str(target.home),),
    )
    assert path.stat().st_mode & 0o777 == 0o700
    assert len(checkpoints) == 2
    assert publish_artifacts(target, (), updated, checkpoints.append, roots=(str(target.home),)) == ()
    assert not path.exists()
    assert publish_artifacts(target, (), updated, checkpoints.append, roots=(str(target.home),)) == ()


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
    owned = publish_artifacts(target, (artifact(path),), (), lambda files: None, roots=(str(target.home),))
    path.write_bytes(b"operator edit")
    assert publish_artifacts(target, (), owned, lambda files: None, roots=(str(target.home),)) == owned
    assert path.read_bytes() == b"operator edit"
    with pytest.raises(StateError):
        publish_artifacts(target, (artifact(path, b"update"),), owned, lambda files: None, roots=(str(target.home),))


def test_modified_file_mode_is_preserved_and_diagnosed(target):
    path = target.home / "managed"
    owned = publish_artifacts(target, (artifact(path),), (), lambda files: None, roots=(str(target.home),))
    path.chmod(0o700)
    with pytest.raises(StateError):
        publish_artifacts(target, (artifact(path),), owned, lambda files: None, roots=(str(target.home),))
    assert publish_artifacts(target, (), owned, lambda files: None, roots=(str(target.home),)) == owned
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
        ArtifactOrigin("vm", "vm", "box"),
    )
    deferred = ArtifactDeferral(input_id=item.identity, destination="user", reason="native user placement")
    assert validate_application(
        ArtifactApplication(deferred=(deferred,)), (item,), "vm", integration="fixture"
    ).deferred == (deferred,)
    for facet, inputs, result in (
        ("vm", (), ArtifactApplication(deferred=(deferred,))),
        ("vm", (item,), ArtifactApplication(deferred=(deferred, deferred))),
        ("workspace", (item,), ArtifactApplication(deferred=(deferred,))),
        ("session", (item,), ArtifactApplication(deferred=(deferred,))),
    ):
        with pytest.raises(StateError):
            validate_application(result, inputs, facet, integration="fixture")


def test_body_is_not_exposed_through_transport_log(target):
    content = b"private artifact fixture content"
    publish_artifacts(
        target, (artifact(target.home / "rule.md", content),), (), lambda files: None, roots=(str(target.home),)
    )
    assert content.decode() not in "".join(target.commands + target.logged_output)
