"""Exact local exclusions preserve operator metadata, tracked paths and owners."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.git_excludes import reconcile_workspace_excludes
from agentworks.errors import StateError
from agentworks.harness_setup.model import NativeClaim
from tests.artifacts.test_native_delivery import owned
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest filesystem")


def git(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[LocalFixtureTransport, Path]:
    target = LocalFixtureTransport(tmp_path / "guest")
    workspace = target.home / "project"
    git("init", "-q", workspace)
    return target, workspace


def apply(
    target: LocalFixtureTransport,
    workspace: Path,
    files: Sequence[OwnedArtifactFile],
    claims: tuple[NativeClaim, ...] = (),
    integration: str = "codex",
) -> tuple[tuple[NativeClaim, ...], list[tuple[NativeClaim, ...]], list[str]]:
    current = claims
    checkpoints: list[tuple[NativeClaim, ...]] = []
    incomplete: list[str] = []

    def checkpoint(value: tuple[NativeClaim, ...]) -> None:
        nonlocal current
        checkpoints.append(value)
        current = value

    reconcile_workspace_excludes(
        target, str(workspace), integration, files, claims, checkpoint, group="", report_incomplete=incomplete.append
    )
    return current, checkpoints, incomplete


def file(path: Path) -> OwnedArtifactFile:
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


def test_nested_workspace_queries_track_exact_literal_paths_from_repository_root(repository, captured_output):
    target, repository_root = repository
    workspace = repository_root / "nested"
    tracked = file(workspace / ".agents/skills/tracked/data [x]*?.txt")
    untracked = file(workspace / ".agents/skills/tracked/data [y]*?.txt")
    neighbor = file(workspace / ".agents/skills/tracked/data y1.txt")
    git("--literal-pathspecs", "-C", repository_root, "add", "--", tracked.path, neighbor.path)
    claims, _, _ = apply(target, workspace, (tracked, untracked))
    assert len(claims) == 1
    assert git("--literal-pathspecs", "-C", workspace, "ls-files", "--full-name", "--", tracked.path).stdout
    assert captured_output.warnings
    assert git("-C", workspace, "check-ignore", "--no-index", "--", untracked.path).returncode == 0
    assert all(path in git("-C", repository_root, "ls-files").stdout for path in ("data [x]*?.txt", "data y1.txt"))


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
    current, _, incomplete = apply(target, workspace, (), (bad,))
    assert not current and not incomplete
    assert unrelated.read_bytes() == b"operator content"
    assert all(str(unrelated) not in command for command in target.commands)


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
    unrelated = linked / ".agents/skills/first/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated operator skill")
    first_claims, _, _ = apply(target, workspace, (first,))
    assert git("-C", linked, "check-ignore", "--no-index", "--", unrelated).returncode == 0
    second_claims, _, _ = apply(target, linked, (second,), integration="grok-build")
    assert first_claims[0].destination == second_claims[0].destination
    remaining, _, _ = apply(target, workspace, (), first_claims)
    assert not remaining
    assert git("-C", linked, "check-ignore", "--no-index", "--", second.path).returncode == 0
    assert unrelated.read_bytes() == b"unrelated operator skill"
    assert second_claims[0].identifier.encode() in Path(second_claims[0].destination).read_bytes()
    git("-C", workspace, "worktree", "remove", "--force", linked)


@pytest.mark.parametrize("nested", [False, True])
def test_workspace_alias_uses_git_relative_names_for_tracking_and_exact_patterns(repository, nested, captured_output):
    target, root = repository
    workspace = root / "nested" if nested else root
    workspace.mkdir(exist_ok=True)
    alias = target.home / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    tracked = file(alias / ".agents/skills/policy/tracked [x]*?.txt")
    untracked = file(alias / ".agents/skills/policy/new [x]*?.txt")
    git("--literal-pathspecs", "-C", alias, "add", "--", ".agents/skills/policy/tracked [x]*?.txt")
    claims, _, incomplete = apply(target, alias, (tracked, untracked))
    assert not incomplete and len(claims) == 1
    assert claims[0].source == str(alias)
    assert captured_output.warnings
    assert (
        git(
            "-C", root, "check-ignore", "--no-index", "--", workspace / ".agents/skills/policy/new [x]*?.txt"
        ).returncode
        == 0
    )
    remaining, _, incomplete = apply(target, alias, (), claims)
    assert not remaining and not incomplete
    assert git(
        "--literal-pathspecs",
        "-C",
        alias,
        "ls-files",
        "--error-unmatch",
        "--",
        ".agents/skills/policy/tracked [x]*?.txt",
    ).stdout


@pytest.mark.parametrize("reapply", [False, True])
def test_git_directory_move_rebinds_only_current_metadata_and_converges(repository, reapply):
    target, workspace = repository
    record = file(workspace / ".agents/skills/policy/SKILL.md")
    prefix = b"# operator\n*.local\n"
    exclude = workspace / ".git/info/exclude"
    exclude.write_bytes(prefix)
    claims, _, _ = apply(target, workspace, (record,))
    before = exclude.read_bytes()
    moved = target.home / "git-metadata"
    git("-C", workspace, "init", "-q", "--separate-git-dir", moved)
    destination = moved / "info/exclude"
    destination.chmod(0o640)
    metadata = destination.stat()
    first = len(target.commands)
    remaining, _, incomplete = apply(target, workspace, (record,) if reapply else (), claims)
    assert not incomplete
    assert all(claim.destination == str(destination) for claim in remaining)
    assert all(claims[0].destination not in command for command in target.commands[first:])
    assert destination.read_bytes() == (before if reapply else prefix)
    after = destination.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (metadata.st_uid, metadata.st_gid, metadata.st_mode)
    if reapply:
        assert after.st_mtime_ns == metadata.st_mtime_ns
        assert apply(target, workspace, (record,), remaining) == (remaining, [], [])
        retired, _, _ = apply(target, workspace, (), remaining)
        assert not retired and destination.read_bytes() == prefix
    else:
        assert not remaining


@pytest.mark.parametrize("change", ["source", "glob", "escape", "outside"])
def test_invalid_persisted_association_is_released_without_writing_its_destination(repository, change):
    target, workspace = repository
    record = file(workspace / ".agents/skills/policy/SKILL.md")
    claims, _, _ = apply(target, workspace, (record,))
    exclude = Path(claims[0].destination)
    before = exclude.read_bytes()
    updates = {
        "source": {"source": str(workspace) + "-other"},
        "glob": {"identifier": "/*"},
        "escape": {"identifier": "/\\x"},
        "outside": {"identifier": "/../outside"},
    }
    bad = claims[0].model_copy(update=updates[change])
    remaining, _, incomplete = apply(target, workspace, (), (bad,))
    assert not remaining and not incomplete
    assert exclude.read_bytes() == before


def test_exclusion_refuses_paths_outside_the_owning_workspace(repository):
    target, workspace = repository
    record = file(target.home / "outside/SKILL.md")
    with pytest.raises(StateError):
        apply(target, workspace, (record,))


def test_rebound_git_claims_roundtrip_every_persisted_checkpoint(db, repository):
    from agentworks.harness_setup.model import NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import read_native_setup, write_native_setup

    target, workspace = repository
    record = file(workspace / ".agents/skills/policy/SKILL.md")
    claims, _, _ = apply(target, workspace, (record,))
    older = claims[0].model_copy(
        update={"destination": str(target.home / "old/exclude"), "sha256": "a" * 64, "strategy": "merge"}
    )
    db.insert_vm("vm", "site", "host")
    db.insert_workspace("project", str(workspace), "vm", "")
    original = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        claims=(older, *claims),
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(original,)), operation="workspace-create")
    exclude = Path(claims[0].destination)
    before = exclude.read_bytes(), exclude.stat().st_mtime_ns
    checkpoints: list[NativeSetupState] = []

    def checkpoint(value):
        current = original.model_copy(update={"claims": value})
        write_native_setup(
            db, "workspace", "project", NativeSetupState(records=(current,)), operation="workspace-create"
        )
        checkpoints.append(read_native_setup(db, "workspace", "project"))

    reconcile_workspace_excludes(
        target,
        str(workspace),
        "codex",
        (record,),
        original.claims,
        checkpoint,
        group="",
        report_incomplete=lambda reason: pytest.fail(reason),
    )
    assert checkpoints and all(state.records[0].claims == claims for state in checkpoints)
    assert read_native_setup(db, "workspace", "project").records[0].claims == claims
    assert (exclude.read_bytes(), exclude.stat().st_mtime_ns) == before


def test_git_metadata_tracking_does_not_execute_configured_fsmonitor(repository, captured_output):
    import shlex

    target, workspace = repository
    tracked = file(workspace / ".agents/skills/policy/tracked [x]*?.txt")
    relative = ".agents/skills/policy/tracked [x]*?.txt"
    git("--literal-pathspecs", "-C", workspace, "add", "--", relative)
    marker = target.home / "monitor-ran"
    monitor = target.home / "fsmonitor.sh"
    monitor.write_text(
        "#!/bin/sh\n" + f"printf 'called\\n' >> {shlex.quote(str(marker))}\n" + "printf 'fixture-token\\0'\n"
    )
    monitor.chmod(0o700)
    git("-C", workspace, "config", "core.fsmonitor", monitor)
    git("-C", workspace, "config", "core.fsmonitorHookVersion", "2")
    assert git("--literal-pathspecs", "-C", workspace, "ls-files", "--", relative).stdout.strip() == relative
    assert marker.exists()
    marker.unlink()
    claims, _, incomplete = apply(target, workspace, (tracked,))
    assert not claims and not incomplete and captured_output.warnings
    assert not marker.exists()


@pytest.mark.parametrize("placement", ["prefixed", "prefixed-first", "prefixed-last"])
@pytest.mark.parametrize("reapply", [False, True])
def test_modified_marker_line_preserves_exclusions_and_evidence_until_repaired(repository, placement, reapply):
    from agentworks.artifacts.git_excludes import _pair

    target, workspace = repository
    record = file(workspace / ".agents/skills/policy/SKILL.md")
    claims, _, _ = apply(target, workspace, (record,))
    exclude = Path(claims[0].destination)
    pair = _pair(claims[0], "codex")
    prefixed = b"# operator note " + pair
    variants = {"prefixed": prefixed, "prefixed-first": prefixed + pair, "prefixed-last": pair + prefixed}
    independent = workspace / "private.env"
    independent.write_bytes(b"fixture only")
    modified = b"# operator\n" + variants[placement] + b"private.env\n"
    exclude.write_bytes(modified)
    before = exclude.stat().st_mtime_ns
    assert git("-C", workspace, "check-ignore", "--no-index", "--", independent).returncode == 0
    remaining, checkpoints, incomplete = apply(target, workspace, (record,) if reapply else (), claims)
    assert remaining == claims and not checkpoints and incomplete
    assert exclude.read_bytes() == modified and exclude.stat().st_mtime_ns == before
    assert git("-C", workspace, "check-ignore", "--no-index", "--", independent).returncode == 0
    exclude.write_bytes(b"# operator\n" + pair + b"private.env\n")
    retired, _, incomplete = apply(target, workspace, (), claims)
    assert not retired and not incomplete
    assert exclude.read_bytes() == b"# operator\nprivate.env\n"
    assert git("-C", workspace, "check-ignore", "--no-index", "--", independent).returncode == 0
