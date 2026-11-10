"""Workspace deletion retires shared Git effects before losing their owner."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces.manager import delete_workspace
from tests.artifacts.test_git_excludes import apply, file, git
from tests.capabilities.test_native_setup_deletion import deletion as deletion
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


def store(db: Database, name: str, claims: tuple[NativeClaim, ...]) -> None:
    write_native_setup(
        db,
        "workspace",
        name,
        NativeSetupState(
            records=(
                SetupRecord(
                    component="workspace",
                    integration="codex",
                    destination_id="a" * 64,
                    declaration={},
                    complete=True,
                    claims=claims,
                ),
            )
        ),
        operation="workspace-create",
    )


@pytest.fixture
def shared_workspace(db, deletion, tmp_path, monkeypatch):
    import grp

    target = LocalFixtureTransport(tmp_path / "guest")
    repository = target.home / "repository"
    git("init", "-q", repository)
    first = repository / "project"
    second = repository / "survivor"
    first_file = file(first / ".agents/skills/policy/SKILL.md")
    second_file = file(second / ".agents/skills/other/SKILL.md")
    exclude = repository / ".git/info/exclude"
    prefix = b"# operator\n*.local\n"
    exclude.write_bytes(prefix)
    exclude.chmod(0o640)
    first_claims, _, _ = apply(target, first, (first_file,))
    second_claims, _, _ = apply(target, second, (second_file,))
    db.update_workspace_path("project", str(first))
    group = grp.getgrgid(os.getgid()).gr_name
    db._conn.execute("UPDATE workspaces SET linux_group = ? WHERE name = 'project'", (group,))
    db._conn.commit()
    db.insert_workspace("survivor", str(second), "box", group)
    db.update_vm_tailscale("box", "box")
    foreign = NativeClaim(role="unrelated", identifier="keep", destination="native")
    store(db, "project", (*first_claims, foreign))
    store(db, "survivor", second_claims)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    removed = []

    def remove(vm, config, name, path, linux_group, **kwargs):
        assert not any(
            claim.role == "artifact-git-exclude"
            for record in read_native_setup(db, "workspace", name).records
            for claim in record.claims
        )
        assert read_native_setup(db, "workspace", name).records[0].claims == (foreign,)
        assert first_claims[0].identifier.encode() not in exclude.read_bytes()
        assert second_claims[0].identifier.encode() in exclude.read_bytes()
        shutil.rmtree(path)
        removed.append(name)

    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", remove)
    return target, first, second, exclude, prefix, first_claims, second_claims, removed


def test_workspace_delete_retires_only_its_shared_exclusions(db, deletion, shared_workspace):
    target, first, second, exclude, prefix, first_claims, second_claims, removed = shared_workspace
    survivor = db.get_workspace("survivor")
    survivor_state = db.instance_state.get_applied_slices("workspace", "survivor")
    before = exclude.stat()
    delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert removed == ["project"] and not first.exists()
    assert db.get_workspace("project") is None
    assert db.instance_state.get_applied_slices("workspace", "project") == ()
    assert db.get_workspace("survivor") == survivor and second.exists()
    assert db.instance_state.get_applied_slices("workspace", "survivor") == survivor_state
    assert exclude.read_bytes().startswith(prefix)
    assert first_claims[0].identifier.encode() not in exclude.read_bytes()
    assert second_claims[0].identifier.encode() in exclude.read_bytes()
    after = exclude.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert (
        git("-C", second, "check-ignore", "--no-index", "--", second / ".agents/skills/other/SKILL.md").returncode == 0
    )


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("problem", ["modified", "duplicate", "unavailable", "missing-workspace", "transport", "trust"])
def test_incomplete_shared_cleanup_warns_and_keeps_parent_deletion_best_effort(
    db, deletion, shared_workspace, problem, force, monkeypatch, captured_output
):
    target, first, second, exclude, prefix, first_claims, second_claims, removed = shared_workspace
    original = exclude.read_bytes()
    git_dir = exclude.parent.parent
    displaced = git_dir.with_name("saved-git")
    if problem == "modified":
        exclude.write_bytes(original.replace(first_claims[0].identifier.encode(), b"operator-modified-pattern"))
    elif problem == "duplicate":
        from agentworks.artifacts.git_excludes import _pair

        exclude.write_bytes(original + _pair(first_claims[0], "codex"))
    elif problem == "unavailable":
        git_dir.rename(displaced)
    elif problem == "missing-workspace":
        shutil.rmtree(first)
    elif problem == "transport":
        from agentworks.ssh import SSHError

        monkeypatch.setattr(target, "run", lambda *args, **kwargs: (_ for _ in ()).throw(SSHError("fixture transport")))
    elif problem == "trust":
        target.environment["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
    before = exclude.read_bytes() if exclude.exists() else None
    observed = []

    def remove(vm, config, name, path, linux_group, **kwargs):
        record = read_native_setup(db, "workspace", name).records[0]
        observed.append(record)
        assert record.pending_cleanup and not record.complete and record.incomplete_reasons
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)

    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", remove)
    try:
        delete_workspace(db, deletion.config, "project", force=force, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        assert removed == ["project"] and observed and captured_output.warnings
        assert db.get_workspace("project") is None and not first.exists()
        assert db.instance_state.get_applied_slices("workspace", "project") == ()
        assert first_claims[0] in observed[0].claims
        if before is not None:
            assert exclude.read_bytes() == before
        assert db.get_workspace("survivor") is not None and second.exists()
    finally:
        if problem == "unavailable":
            displaced.rename(git_dir)
        target.environment.pop("GIT_TEST_ASSUME_DIFFERENT_OWNER", None)


@pytest.mark.parametrize("failure", [RuntimeError("internal"), KeyboardInterrupt(), StateError("shared persistence")])
def test_cleanup_does_not_swallow_shared_failures_or_cancellation(db, deletion, shared_workspace, monkeypatch, failure):
    target, _, _, _, _, _, _, removed = shared_workspace

    def fail(*args, **kwargs):
        raise failure

    if isinstance(failure, StateError):
        monkeypatch.setattr("agentworks.harness_setup.cleanup.write_native_setup", fail)
    else:
        monkeypatch.setattr(target, "run", fail)
    with pytest.raises(type(failure)) as caught:
        delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert caught.value is failure and not removed and db.get_workspace("project") is not None


def test_vm_deletion_does_not_require_child_workspace_git_cleanup(db, deletion, shared_workspace, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agentworks.vms.manager import delete_vm

    _, _, _, exclude, _, _, _, _ = shared_workspace
    before = exclude.read_bytes()
    removed = []
    monkeypatch.setattr(
        "agentworks.harness_setup.cleanup.cleanup_workspace_excludes",
        lambda *args: pytest.fail("VM deletion must not require workspace metadata cleanup"),
    )
    monkeypatch.setattr(
        "agentworks.vms.manager.power._live_vm_boundary",
        lambda *args, **kwargs: (
            SimpleNamespace(
                site=SimpleNamespace(platform=SimpleNamespace(delete=lambda vm, ctx: removed.append(vm.name)))
            ),
            Mock(),
        ),
    )
    delete_vm(db, deletion.config, "box", force=True, yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert removed == ["box"]
    assert db.get_vm("box") is None and db.get_workspace("project") is None and db.get_workspace("survivor") is None
    assert exclude.read_bytes() == before


def test_shared_persistence_failure_during_git_refusal_is_not_wrapped_or_swallowed(
    db, deletion, shared_workspace, monkeypatch
):
    from agentworks.harness_setup.state import write_native_setup

    target, _, _, _, _, _, _, removed = shared_workspace
    target.environment["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
    failure = StateError("shared persistence")
    calls = 0

    def persist(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return write_native_setup(*args, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.cleanup.write_native_setup", persist)
    with pytest.raises(StateError) as caught:
        delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert caught.value is failure and calls == 2
    assert not removed and db.get_workspace("project") is not None


@pytest.mark.parametrize("destination", [None, 17, [], {}, True, "relative"])
def test_malformed_metadata_destination_warns_without_blocking_actual_parent_deletion(
    db, deletion, shared_workspace, monkeypatch, captured_output, destination
):
    from tests.artifacts.test_git_excludes import metadata_destination

    target, first, second, exclude, _, first_claims, _, removed = shared_workspace
    before = exclude.read_bytes()
    metadata_destination(target, destination)
    observed = []

    def remove(vm, config, name, path, linux_group, **kwargs):
        current = read_native_setup(db, "workspace", name).records[0]
        observed.append(current)
        assert current.claims[0] == first_claims[0] and current.incomplete_reasons
        shutil.rmtree(path)
        removed.append(name)

    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", remove)
    delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert observed and removed == ["project"] and captured_output.warnings
    assert db.get_workspace("project") is None and not first.exists() and second.exists()
    assert exclude.read_bytes() == before
