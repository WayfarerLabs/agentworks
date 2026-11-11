"""Copied MCP entries keep their owning workspace's current placement evidence."""

from __future__ import annotations

import sys

import pytest

from agentworks.artifacts.application import ArtifactSkip
from agentworks.artifacts.git_excludes import reconcile_workspace_excludes
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE
from agentworks.errors import StateError
from agentworks.harness_setup.cleanup import _relocated_record
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces.manager import rehome_workspace
from tests.artifacts.test_git_excludes import file, git
from tests.artifacts.test_workspace_exclusion_rehome import move_case as move_case
from tests.capabilities.test_native_setup_deletion import deletion as deletion

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


@pytest.mark.parametrize("remove_old", [False, True])
def test_repeated_move_rebases_mcp_and_coexisting_file_evidence(db, deletion, move_case, remove_old):
    target, group = move_case
    old = target.home / "original"
    git("init", "-q", old)
    native = old / ".codex/config.toml"
    native.parent.mkdir()
    native.write_text('[mcp_servers.foreign]\ncommand = "operator-server"\n')
    original = native.read_bytes()
    skill = file(old / ".agents/skills/review/SKILL.md").model_copy(
        update={"package_root": str(old / ".agents/skills/review")}
    )
    server = NativeClaim(
        role=MCP_CLAIM_ROLE,
        identifier="agentworks-review-" + "a" * 20,
        destination=str(native),
        source="b" * 64,
        sha256="c" * 64,
    )
    claims = (server,)

    def checkpoint(value):
        nonlocal claims
        claims = value

    reconcile_workspace_excludes(
        target,
        str(old),
        "codex",
        (skill,),
        claims,
        checkpoint,
        group=group,
        report_incomplete=lambda value: pytest.fail(value),
        shared_paths=(str(native),),
    )
    db.update_workspace_path("project", str(old))
    vm = db.get_vm("box")
    record = SetupRecord(
        component="workspace",
        integration="codex",
        declaration={},
        complete=True,
        destination_id=destination_id(vm, target, location=str(old)),
        claims=claims,
        artifact_files=(skill,),
        skipped=(ArtifactSkip(path=str(old / ".agents/skipped"), origins=("d" * 64,), reason="fixture"),),
    )
    write_native_setup(db, "workspace", "project", NativeSetupState(records=(record,)), operation="workspace-create")
    identities = {record.destination_id}
    for index in range(2):
        new = target.home / f"moved-{index}"
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path=str(new),
            remove_old=remove_old,
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        current = read_native_setup(db, "workspace", "project").records[0]
        moved = next(claim for claim in current.claims if claim.role == MCP_CLAIM_ROLE)
        assert moved == server.model_copy(update={"destination": str(new / ".codex/config.toml")})
        assert current.destination_id == destination_id(vm, target, location=str(new))
        assert current.destination_id not in identities
        identities.add(current.destination_id)
        assert current.complete and not current.pending_cleanup
        assert current.artifact_files[0].path == str(new / ".agents/skills/review/SKILL.md")
        assert current.artifact_files[0].package_root == str(new / ".agents/skills/review")
        assert current.artifact_files[0].sha256 == skill.sha256
        assert current.skipped[0].path == str(new / ".agents/skipped")
        assert (new / ".codex/config.toml").read_bytes() == original
        assert git("-C", new, "check-ignore", "--", ".codex/config.toml").returncode == 0
        assert old.exists() == (not remove_old)
        if old.exists():
            assert (old / ".codex/config.toml").read_bytes() == original
        old = new


@pytest.mark.parametrize("path", ["/other/config.toml", "/old/../outside", "https://example.test/mcp"])
def test_foreign_or_malformed_claim_is_not_relocated(path):
    record = SetupRecord(
        component="workspace",
        integration="codex",
        destination_id="a" * 64,
        declaration={},
        claims=(NativeClaim(role=MCP_CLAIM_ROLE, identifier="server", destination=path),),
    )
    with pytest.raises(StateError):
        _relocated_record(record, "/old", "/new", "b" * 64)
