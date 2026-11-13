"""``workspace delete`` through the orchestrated model.

Gate-prompt parity, its pre-gate refusals, the group removal (issue #249),
stored paths it cannot address, and its two paths: its own
``gated_vm_boundary`` composition when standalone, the caller's bound
platform held verbatim on the nested-teardown path, and no boundary at all
without a VM row.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import InitStatus, VMStatus
from agentworks.errors import StateError, UserAbort
from agentworks.execution import trust
from agentworks.execution.access import Admin
from agentworks.execution.errors import TrustMissing
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms.manager.boundary import require_vm_ssh_boundary
from agentworks.workspaces import manager as workspace_manager
from tests.conftest import seed_vm_trust
from tests.workspaces._lifecycle_support import (
    Guest,
    arguments,
    no_gate,
    node_holding,
    reachable,
    route,
    seed,
    seed_live_session,
    starts,
    stop_the_vm,
)
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput

# Captured at import, before the autouse fixture stubs the SSH precondition.
_REAL_SSH_BOUNDARY = require_vm_ssh_boundary

REMOVE_DIRECTORY = ("rm", "-rf", "--", "/srv/ws1")
REMOVE_GROUP = ("/usr/sbin/groupdel", "ws-ws1")


def test_delete_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert REMOVE_DIRECTORY in guest.argvs()
    assert db.get_workspace("ws1") is None
    assert "Workspace 'ws1' deleted" in captured_output.info


def test_delete_on_a_vm_with_no_host_key_record_refuses_before_touching_the_row_or_the_vm(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The remote cleanup is best-effort over SSH errors, so a missing record
    must refuse up front: deleting the row would orphan the VM-side directory."""
    config = make_config()
    seed(db)
    trust.remove("box")
    reachable(monkeypatch, True)
    # The module stubs the SSH precondition; this test is about the real one.
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", _REAL_SSH_BOUNDARY)
    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", _REAL_SSH_BOUNDARY)

    with pytest.raises(TrustMissing):
        workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_workspace("ws1") is not None
    assert guest.requests == []
    assert resolve_counter == []


def test_delete_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    seed(db)
    events: list[str] = []
    stop_the_vm(monkeypatch, events)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status", "status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_workspace("ws1") is None


# -- validation stays pre-gate ------------------------------------------------


def test_delete_sessions_guard_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    seed(db)
    seed_live_session(db, name="s1", ws="ws1")
    no_gate(monkeypatch)

    with pytest.raises(StateError, match="has 1 session"):
        workspace_manager.delete_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert guest.requests == []
    assert db.get_workspace("ws1") is not None


def test_delete_declined_confirm_aborts_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    seed(db)
    no_gate(monkeypatch)
    captured_output.confirm_response = False

    with pytest.raises(UserAbort, match="delete cancelled"):
        workspace_manager.delete_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert guest.requests == []
    assert db.get_workspace("ws1") is not None


# -- delete removes the workspace's Linux group (issue #249) -------------------


def test_delete_removes_the_workspace_group_after_the_directory(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Delete tears down the workspace's Linux group, the symmetric
    counterpart of the group creation at create, so no residual group
    survives the delete (issue #249), and only once the directory is gone."""
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    commands = guest.argvs()
    assert commands.index(REMOVE_GROUP) > commands.index(REMOVE_DIRECTORY)
    assert db.get_workspace("ws1") is None


def test_delete_group_removal_uses_the_recorded_linux_group(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The group removed is the one RECORDED on the workspace row, not a
    re-derivation from the name: a legacy workspace whose on-VM group
    keeps the older ``ws--`` prefix drops that exact group."""
    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    seed_vm_trust("box")
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    db.insert_workspace(
        "legacy",
        vm_name="box",
        workspace_path="/srv/legacy",
        template="default",
        linux_group="ws--legacy",
    )
    reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "legacy", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    removals = [command for command in guest.argvs() if command[:1] == ("/usr/sbin/groupdel",)]
    assert removals == [("/usr/sbin/groupdel", "ws--legacy")]


def test_delete_group_removal_failure_does_not_break_the_delete(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A group the removal cannot remove (already gone, or refused) is
    tolerated: its non-zero exit never raises, the delete still finishes (row
    gone, success reported), and no 'remote cleanup failed' warning fires."""
    fake = Guest(fails=(REMOVE_GROUP,))
    route(monkeypatch, fake)

    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert REMOVE_GROUP in fake.argvs()
    assert db.get_workspace("ws1") is None
    assert "Workspace 'ws1' deleted" in captured_output.info
    assert not any("remote cleanup failed" in w for w in captured_output.warnings)


# -- stored paths the file operations cannot address --------------------------


def test_delete_of_a_stored_path_that_is_not_absolute_removes_the_row_and_link_and_leaves_the_vm_files(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A '~' row names no one directory: nothing it names is touched, and the
    console link goes, so a new create of the name can link its config
    without replacing."""
    from agentworks.linux_users import managed_home
    from agentworks.workspaces.tmuxinator import remove_link

    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = '~/ws/ws1' WHERE name = 'ws1'")
    db._conn.commit()
    reachable(monkeypatch, True)
    vm = db.get_vm("box")
    assert vm is not None
    link_removal = remove_link(managed_home(vm.admin_username), "ws1")

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_workspace("ws1") is None
    assert link_removal in guest.requests
    others = [request for request in guest.requests if request != link_removal]
    assert not any("ws/ws1" in argument or "tmuxinator" in argument for r in others for argument in arguments(r))
    assert REMOVE_GROUP in guest.argvs()
    assert len(captured_output.warnings) == 1
    assert "~/ws/ws1" in captured_output.warnings[0]


def test_delete_of_a_stored_path_with_a_dotdot_removes_its_directory_and_link_as_the_vm_resolves_them(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.linux_users import managed_home
    from agentworks.workspaces.tmuxinator import remove_link_to

    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = '/home/admin/../srv/ws1' WHERE name = 'ws1'")
    db._conn.commit()
    reachable(monkeypatch, True)
    vm = db.get_vm("box")
    assert vm is not None

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_workspace("ws1") is None
    assert ("rm", "-rf", "--", "/home/admin/../srv/ws1") in guest.argvs()
    assert remove_link_to(managed_home(vm.admin_username), "ws1", "/home/admin/../srv/ws1") in guest.requests
    assert captured_output.warnings == []


# -- delete's two paths -------------------------------------------------------


def test_delete_nested_platform_path_reuses_the_callers_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The nested-teardown seam: the caller hands its already-held VM
    NODE, whose gate has converged and holds the VM, so delete performs
    ZERO additional resolves and composes no second boundary (a status
    probe would be one); it trusts that gate and re-enters only the
    keepalive hold, reaching the platform through the node's site edge."""

    class _BoundPlatformStub:
        name = "proxmox"

        def __init__(self) -> None:
            self.holds = 0

        def vm_active(self, row: object, *, config: object | None = None) -> contextlib.AbstractContextManager[None]:
            self.holds += 1
            return contextlib.nullcontext()

        def status(self, row: object, ctx: object) -> VMStatus:
            raise AssertionError("nested delete must not probe status")

    config = make_config()
    seed(db)
    no_gate(monkeypatch)  # any boundary composition would probe status and fail
    reachable(monkeypatch, True)
    bound = _BoundPlatformStub()
    vm_node = node_holding(db, config, bound)

    workspace_manager.delete_workspace(
        db, config, "ws1", force=True, yes=True, vm_node=vm_node, interaction=TtyInteractionPolicy.REFUSE
    )

    assert resolve_counter == []  # nothing resolved beyond the caller's pass
    assert bound.holds == 1  # the hold was re-entered, nothing else
    assert db.get_workspace("ws1") is None
    assert REMOVE_DIRECTORY in guest.argvs()


def test_delete_nested_rejects_a_mismatched_vm_node(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown must hand ``delete_workspace`` the workspace's OWN vm node.
    A node for a different VM would hold that VM active while the delete
    body works against the workspace's real VM; the guard raises a typed
    ``StateError`` before the hold is ever entered."""
    config = make_config()
    seed(db)  # ws1 on 'box'
    db.insert_vm("other", site="proxmox", hostname="other")
    db.update_vm_tailscale("other", "100.64.0.10")
    no_gate(monkeypatch)  # nothing may probe status or hold the VM
    vm_node = node_holding(db, config, object(), vm_name="other")

    with pytest.raises(StateError, match="teardown-wiring bug"):
        workspace_manager.delete_workspace(
            db, config, "ws1", force=True, yes=True, vm_node=vm_node, interaction=TtyInteractionPolicy.REFUSE
        )

    assert resolve_counter == []  # refused before any resolve
    assert db.get_workspace("ws1") is not None  # nothing was deleted


def test_delete_without_vm_row_is_db_only_with_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A workspace whose VM row is gone (drift) deletes DB-side only, with no
    boundary, no gate, and no request."""
    config = make_config()
    seed(db)
    # Fabricate the drift: drop the VM row out from under the workspace
    # (FKs off for the surgery only).
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'box'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    no_gate(monkeypatch)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert guest.requests == []
    assert db.get_workspace("ws1") is None


def test_delete_tears_down_a_live_session_on_the_admins_access_before_the_directory(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """With ``--force``, each session is torn down on the admin's access, which
    owns an admin session, before anything of the workspace is removed."""
    from agentworks.sessions import manager as sessions_manager

    config = make_config()
    seed(db)
    seed_live_session(db, name="s1", ws="ws1")
    reachable(monkeypatch, True)
    torn_down: list[tuple[str, object, bool]] = []
    monkeypatch.setattr(sessions_manager, "ensure_pids_batch", lambda sessions, **k: sessions)

    def teardown(session, *, access, target_owns_session, **kwargs):  # noqa: ANN001, ANN202
        torn_down.append((session.name, access.identity, target_owns_session))
        assert not any(starts("rm")(request) for request in guest.requests)

    monkeypatch.setattr(sessions_manager, "_teardown_session", teardown)

    workspace_manager.delete_workspace(db, config, "ws1", force=True, yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert torn_down == [("s1", Admin(), True)]
    assert REMOVE_DIRECTORY in guest.argvs()
