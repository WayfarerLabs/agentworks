"""Elevated publication stays inside the dedicated root directories, before any file operation."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.publication import publish_artifacts
from agentworks.errors import StateError
from tests.vm_access_support import Admin, RecordingVMAccess


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
def test_publication_rejects_desired_and_persisted_root_escape_before_any_request(path, package_root, previous):
    access = RecordingVMAccess([], Admin())
    checkpoint = Mock()
    desired = ArtifactFile(path, b"rule", ("a" * 64,), package_root=package_root)
    prior = OwnedArtifactFile(path=path, sha256="b" * 64, origins=("a" * 64,), package_root=package_root)
    # Even a caller-supplied broader scope cannot enlarge the global boundary.
    with pytest.raises(StateError):
        publish_artifacts(
            access.files(),
            () if previous else (desired,),
            (prior,) if previous else (),
            checkpoint,
            roots=("/etc",),
            root=True,
        )
    assert access.requests() == []
    checkpoint.assert_not_called()
