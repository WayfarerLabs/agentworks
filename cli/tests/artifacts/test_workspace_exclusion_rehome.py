"""Real Git and copy orchestration keep exclusions bound to their workspace owner."""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from agentworks.artifacts.application import OwnedArtifactFile
from agentworks.artifacts.git_excludes import _pair
from agentworks.db import Database, InitStatus
from agentworks.harness_setup.cleanup import cleanup_workspace_excludes
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.ssh import SSHResult
from agentworks.workspaces.manager import delete_workspace, rehome_workspace
from tests.artifacts.test_git_excludes import apply, file, git
from tests.capabilities.test_native_setup_deletion import deletion as deletion
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


class RehomeTransport(LocalFixtureTransport):
    """Keep actual copy/Git operations but suppress host ownership and ancestor changes."""

    def run(self, command: str, **kwargs) -> SSHResult:
        if command.startswith(("chown ", "p=$(dirname")):
            self.commands.append(command)
            return SSHResult(0, "", "")
        if command == "which rsync":
            self.commands.append(command)
            return SSHResult(1, "", "")
        return super().run(command, **kwargs)


def save(db: Database, name: str, claims: tuple[NativeClaim, ...], files: tuple[OwnedArtifactFile, ...] = ()) -> None:
    record = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        complete=True,
        claims=claims,
        artifact_files=files,
    )
    write_native_setup(db, "workspace", name, NativeSetupState(records=(record,)), operation="workspace-create")


@pytest.fixture
def move_case(db, deletion, tmp_path, monkeypatch):
    import grp

    target = RehomeTransport(tmp_path / "guest")
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    db.update_vm_tailscale("box", "box")
    group = grp.getgrgid(os.getgid()).gr_name
    db._conn.execute("UPDATE workspaces SET linux_group = ? WHERE name = 'project'", (group,))
    db._conn.commit()
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.workspaces.manager.rehome.gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.workspaces.templates.resolve_live_tmuxinator", lambda *a, **k: False)
    monkeypatch.setattr("agentworks.workspaces.acls.apply_workspace_acls", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.generate_vscode_workspace", lambda *a, **k: tmp_path / "x")
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.delete_vm_workspace",
        lambda vm, config, name, path, group, **kwargs: shutil.rmtree(path),
    )
    return target, group


@pytest.mark.parametrize("remove_old", [False, True])
@pytest.mark.parametrize("topology", ["full", "linked", "nested"])
def test_actual_rehome_rebinds_only_its_pairs_and_delete_retires_them(db, deletion, move_case, topology, remove_old):
    target, group = move_case
    root = target.home / "repository"
    git("init", "-q", root)
    old = root
    new = target.home / "moved project"
    survivor_claims: tuple[NativeClaim, ...] = ()
    if topology == "linked":
        git(
            "-C",
            root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        )
        old = target.home / "linked"
        git("-C", root, "worktree", "add", "--detach", old, "HEAD")
    elif topology == "nested":
        old = root / "nested"
        new = root / "moved project"
    original = b"# operator\nprivate.env\n"
    exclude = root / ".git/info/exclude"
    exclude.write_bytes(original)
    exclude.chmod(0o640)
    if topology != "full":
        survivor_file = file(root / ".agents/skills/policy/SKILL.md")
        survivor_claims, _, _ = apply(target, root, (survivor_file,))
        db.insert_workspace("survivor", str(root), "box", group)
        save(db, "survivor", survivor_claims, (survivor_file,))
    item = file(old / ".agents/skills/policy/SKILL.md")
    claims, _, _ = apply(target, old, (item,))
    old_pair = _pair(claims[0], "codex")
    save(db, "project", claims, (item,))
    db.update_workspace_path("project", str(old))
    survivor_state = db.instance_state.get_applied_slices("workspace", "survivor")
    metadata = exclude.stat()
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=remove_old,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert db.get_workspace("project").workspace_path == str(new)
    assert old.exists() == (not remove_old)
    current = read_native_setup(db, "workspace", "project").records[0]
    assert len(current.claims) == 1 and current.claims[0].source == str(new)
    # Only exclusion associations move; setup's original file inventory remains stale.
    assert current.artifact_files == (item,)
    current_exclude = Path(current.claims[0].destination)
    new_pair = _pair(current.claims[0], "codex")
    assert old_pair not in current_exclude.read_bytes() and new_pair in current_exclude.read_bytes()
    assert git("-C", new, "check-ignore", "--no-index", "--", new / ".agents/skills/policy/SKILL.md").returncode == 0
    if old.exists():
        assert old_pair not in exclude.read_bytes()
    if survivor_claims:
        assert _pair(survivor_claims[0], "codex") in exclude.read_bytes()
        assert db.instance_state.get_applied_slices("workspace", "survivor") == survivor_state
    after = current_exclude.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (metadata.st_uid, metadata.st_gid, metadata.st_mode)
    delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_workspace("project") is None and not new.exists()
    if topology != "full":
        assert exclude.read_bytes() == original + _pair(survivor_claims[0], "codex")
        assert db.instance_state.get_applied_slices("workspace", "survivor") == survivor_state


@pytest.mark.parametrize("remove_old", [False, True])
def test_rehome_does_not_accept_another_owners_source(db, deletion, move_case, remove_old):
    target, group = move_case
    root, old, new = (target.home / part for part in ("repository", "linked", "moved"))
    git("init", "-q", root)
    git(
        "-C",
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    git("-C", root, "worktree", "add", "--detach", old, "HEAD")
    first = file(root / ".agents/skills/policy/SKILL.md")
    second = file(old / ".agents/skills/policy/SKILL.md")
    first_claims, _, _ = apply(target, root, (first,))
    exclude = Path(first_claims[0].destination)
    sentinel = target.home / "foreign-destination"
    sentinel.write_bytes(b"operator sentinel")
    forged = first_claims[0].model_copy(update={"destination": str(sentinel)})
    save(db, "project", (forged,), (second,))
    db.update_workspace_path("project", str(old))
    first_pair = _pair(first_claims[0], "codex")
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=remove_old,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert first_pair in exclude.read_bytes() and sentinel.read_bytes() == b"operator sentinel"
    current = read_native_setup(db, "workspace", "project").records[0]
    assert current.claims and all(claim.source == str(new) for claim in current.claims)
    cleanup_workspace_excludes(db, db.get_workspace("project"), lambda: target)
    assert exclude.read_bytes().endswith(first_pair)
    assert sentinel.read_bytes() == b"operator sentinel"
    assert all(str(sentinel) not in command for command in target.commands)


@pytest.mark.parametrize("remove_old", [False, True])
@pytest.mark.parametrize("fault", ["retire-old", "publish-new"])
def test_rehome_metadata_failure_warns_without_vetoing_the_core_move(
    db, deletion, move_case, remove_old, fault, monkeypatch, captured_output
):
    from agentworks.errors import ExternalError
    from agentworks.native_files import NativeFiles

    target, _ = move_case
    old, new = target.home / "old", target.home / "moved"
    git("init", "-q", old)
    item = file(old / ".agents/skills/policy/SKILL.md")
    claims, _, _ = apply(target, old, (item,))
    old_pair = _pair(claims[0], "codex")
    new_exclude = new / ".git/info/exclude"
    new_claim = claims[0].model_copy(update={"source": str(new), "destination": str(new_exclude)})
    new_pair = _pair(new_claim, "codex")
    save(db, "project", claims, (item,))
    db.update_workspace_path("project", str(old))
    publish = NativeFiles.publish
    failures = []

    def fail_selected(self, destination, content, **kwargs):
        selected = (
            fault == "retire-old"
            and destination == claims[0].destination
            or fault == "publish-new"
            and destination == str(new_exclude)
            and new_pair in content
        )
        if selected:
            failures.append(destination)
            raise ExternalError("fixture metadata publication failure")
        return publish(self, destination, content, **kwargs)

    monkeypatch.setattr(NativeFiles, "publish", fail_selected)
    rehome_workspace(
        db,
        deletion.config,
        "project",
        target_path=str(new),
        remove_old=remove_old,
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert failures and captured_output.warnings
    assert db.get_workspace("project").workspace_path == str(new)
    assert new.exists() and old.exists() == (not remove_old)
    current = read_native_setup(db, "workspace", "project").records[0]
    assert not current.complete and current.pending_cleanup and current.incomplete_reasons
    assert (new / ".agents/skills/policy/SKILL.md").read_bytes() == b"fixture artifact"
    if fault == "retire-old":
        assert current.claims == (new_claim,)
        assert old_pair not in new_exclude.read_bytes() and new_pair in new_exclude.read_bytes()
        if old.exists():
            assert old_pair in Path(claims[0].destination).read_bytes()
    else:
        assert not current.claims and new_pair not in new_exclude.read_bytes()
