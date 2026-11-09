"""Exact local exclusions preserve operator metadata, tracked paths and owners."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactFile
from agentworks.artifacts.git_excludes import reconcile_workspace_excludes
from agentworks.errors import StateError
from tests.artifacts.test_native_delivery import owned
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem")


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path):
    target = LocalFixtureTransport(tmp_path / "guest")
    workspace = target.home / "project"
    git("init", "-q", workspace)
    return target, workspace


def apply(target, workspace, files, claims=(), integration="codex"):
    current = claims
    checkpoints = []
    incomplete = []

    def checkpoint(value):
        nonlocal current
        checkpoints.append(value)
        current = value

    reconcile_workspace_excludes(
        target, str(workspace), integration, files, claims, checkpoint, group="", report_incomplete=incomplete.append
    )
    return current, checkpoints, incomplete


def file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture artifact")
    return owned(ArtifactFile(str(path), path.read_bytes(), ("a" * 64,)))


def test_exact_literal_exclusions_are_idempotent_and_retire_only_owned_pairs(repository):
    target, workspace = repository
    path = workspace / ".agents/skills/policy/data [x]*?.txt"
    record = file(path)
    other = workspace / ".agents/skills/policy/data x1.txt"
    other.write_bytes(b"operator file")
    exclude = workspace / ".git/info/exclude"
    prefix = b"# operator\n*.local\n"
    exclude.write_bytes(prefix)
    claims, checkpoints, incomplete = apply(target, workspace, (record,))
    assert len(claims) == 1 and checkpoints and not incomplete
    assert claims[0].destination == str(exclude)
    assert git("-C", workspace, "check-ignore", "--no-index", "--", path).returncode == 0
    unmatched = subprocess.run(
        ["git", "-C", str(workspace), "check-ignore", "--no-index", "--", str(other)], capture_output=True
    )
    assert unmatched.returncode == 1
    before = exclude.stat().st_mtime_ns
    assert apply(target, workspace, (record,), claims) == (claims, [], [])
    assert exclude.stat().st_mtime_ns == before
    remaining, _, _ = apply(target, workspace, (), claims)
    assert not remaining and exclude.read_bytes() == prefix
    assert other.read_bytes() == b"operator file"


def test_preexisting_identical_pattern_and_unrelated_claim_survive_retirement(repository):
    from agentworks.harness_setup.model import NativeClaim

    target, workspace = repository
    record = file(workspace / ".agents/skills/x/SKILL.md")
    exclude = workspace / ".git/info/exclude"
    original = b"/.agents/skills/x/SKILL.md\n"
    exclude.write_bytes(original)
    foreign = NativeClaim(role="operator-plugin", identifier="keep", destination="/unrelated")
    claims, _, _ = apply(target, workspace, (record,), (foreign,))
    assert foreign in claims and len(claims) == 2
    remaining, _, _ = apply(target, workspace, (), claims)
    assert remaining == (foreign,) and exclude.read_bytes() == original


def test_tracked_artifact_is_not_hidden_or_untracked(repository, captured_output):
    target, workspace = repository
    record = file(workspace / ".agents/skills/tracked/SKILL.md")
    git("-C", workspace, "add", record.path)
    claims, _, _ = apply(target, workspace, (record,))
    assert not claims
    assert git("-C", workspace, "ls-files", "--error-unmatch", "--", record.path).stdout
    assert captured_output.warnings


def test_modified_owned_exclusion_retains_its_bytes_and_ownership(repository):
    target, workspace = repository
    record = file(workspace / ".agents/skills/x/SKILL.md")
    claims, _, _ = apply(target, workspace, (record,))
    exclude = Path(claims[0].destination)
    value = exclude.read_bytes().replace(claims[0].identifier.encode(), b"operator-edited-pattern")
    exclude.write_bytes(value)
    remaining, checkpoints, incomplete = apply(target, workspace, (), claims)
    assert remaining == claims and not checkpoints and incomplete
    assert exclude.read_bytes() == value


def test_forged_persisted_metadata_destination_cannot_authorize_a_write(repository):
    target, workspace = repository
    record = file(workspace / ".agents/skills/x/SKILL.md")
    claims, _, _ = apply(target, workspace, (record,))
    unrelated = target.home / "operator"
    unrelated.write_bytes(b"operator content")
    bad = claims[0].model_copy(update={"destination": str(unrelated)})
    with pytest.raises(StateError):
        apply(target, workspace, (), (bad,))
    assert unrelated.read_bytes() == b"operator content"


def test_linked_worktree_resolves_common_metadata_and_preserves_other_owner(repository):
    target, workspace = repository
    git(
        "-C",
        workspace,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    linked = target.home / "linked"
    git("-C", workspace, "worktree", "add", "--detach", linked, "HEAD")
    first = file(workspace / ".agents/skills/first/SKILL.md")
    second = file(linked / ".agents/skills/second/SKILL.md")
    first_claims, _, _ = apply(target, workspace, (first,))
    second_claims, _, _ = apply(target, linked, (second,), integration="grok-build")
    assert first_claims[0].destination == second_claims[0].destination
    remaining, _, _ = apply(target, workspace, (), first_claims)
    assert not remaining
    assert git("-C", linked, "check-ignore", "--no-index", "--", second.path).returncode == 0
    assert second_claims[0].identifier.encode() in Path(second_claims[0].destination).read_bytes()
    git("-C", workspace, "worktree", "remove", "--force", linked)
