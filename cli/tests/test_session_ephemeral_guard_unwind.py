"""Session rollback reaches real owner deletion with its explicit family token."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from agentworks.agents.nodes import AgentTemplateNode, PendingAgentNode, pending_agent_node
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.db import Database
from agentworks.errors import StateError, UserAbort
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.manager import create_session
from agentworks.vms.nodes import LiveVMNode, VMSiteNode
from agentworks.workspaces.nodes import PendingWorkspaceNode, pending_workspace_node
from tests.conftest import _StubPlatform, stub_build_registry
from tests.sessions.test_create_start_restart_orchestrated import _create_stubs

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config

pytestmark = pytest.mark.windows


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    stub_build_registry(monkeypatch)


@pytest.mark.parametrize("failure", [RuntimeError("launch failed"), UserAbort("cancelled launch"), KeyboardInterrupt()])
def test_session_failure_unwinds_actual_realized_owners_through_real_delete_services(tmp_path, monkeypatch, failure):
    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    config = SimpleNamespace(
        session=SimpleNamespace(history_limit=1),
        paths=SimpleNamespace(vm_workspaces="/srv", vscode_workspaces=tmp_path / "vscode"),
    )
    logger = MagicMock()
    logger.sanitize.side_effect = lambda message: message
    monkeypatch.setattr("agentworks.ssh.SSHLogger", lambda *a, **k: logger)
    monkeypatch.setattr("agentworks.agents.initializer.create_new_agent_user", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.initializer.create_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.create_vm_workspace", lambda *a, **k: "/srv/s1")
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.generate_vscode_workspace", lambda *a, **k: tmp_path / "s1.code-workspace"
    )
    removed: list[str] = []

    def remote_remove(kind: str) -> Callable[..., None]:
        def remove(*args, **kwargs) -> None:
            with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "vm1"):
                pytest.fail("owner deletion lost its enclosing family guard")
            removed.append(kind)

        return remove

    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", remote_remove("agent"))
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", remote_remove("workspace"))

    def fail_launch(*args, **kwargs):
        assert db.get_agent("s1") is not None and db.get_workspace("s1") is not None
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "vm1"):
            pytest.fail("session launch lost its enclosing family guard")
        raise failure

    monkeypatch.setattr("agentworks.sessions.manager._require_workspace", fail_launch)
    try:
        with pytest.raises(type(failure)) as caught:
            create_session(
                db,
                config,
                name="s1",
                new_workspace=True,
                new_agent=True,
                vm_name="vm1",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        assert caught.value is failure
        assert removed == ["agent", "workspace"]
        assert db.get_agent("s1") is None and db.get_workspace("s1") is None
        assert db.get_session("s1") is None
        assert db.get_workspace("ws1") is not None
        with native_mutation_guard(db.path, "vm1"):
            pass
    finally:
        db.close()


def _pending(
    db: Database, config: Config, name: str, vm_node: LiveVMNode, kind: str
) -> PendingAgentNode | PendingWorkspaceNode:
    if kind == "agent":
        db.insert_agent(name, vm_node.row.name, f"agt-{name}")
        return pending_agent_node(
            db,
            config,
            name,
            AgentTemplateNode(ResolvedAgentTemplate("default"), ()),
            vm_node,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    db.insert_workspace(name, f"/srv/{name}", vm_node.row.name, f"ws-{name}")
    return pending_workspace_node(db, config, name, vm_node, None, interaction=TtyInteractionPolicy.REFUSE)


@pytest.mark.parametrize("kind", ["agent", "workspace"])
@pytest.mark.parametrize("invalid", ["family", "database", "completed"])
def test_real_nested_deletion_rejects_wrong_or_completed_guard_tokens(db, tmp_path, kind, invalid):
    vm = db.insert_vm("vm1", "local", "vm1")
    config = SimpleNamespace(paths=SimpleNamespace(vscode_workspaces=tmp_path))
    path = tmp_path / "different.db" if invalid == "database" else db.path
    family = "other" if invalid == "family" else vm.name
    with native_mutation_guard(path, family) as guard:
        node = LiveVMNode(db, config, MagicMock(), vm, cast("VMSiteNode", MagicMock()), held_native_guard=guard)
        pending = _pending(db, config, "new", node, kind)
        if invalid != "completed":
            with pytest.raises(StateError) as rejected:
                pending.teardown()
    if invalid == "completed":
        with pytest.raises(StateError) as rejected:
            pending.teardown()
    assert isinstance(rejected.value.__cause__, StateError)
    assert (db.get_agent("new") if kind == "agent" else db.get_workspace("new")) is not None


@pytest.mark.parametrize("kind", ["agent", "workspace"])
def test_pending_teardown_preserves_user_abort_from_actual_delete_path(db, tmp_path, monkeypatch, kind):
    vm = db.insert_vm("vm1", "local", "vm1")
    config = SimpleNamespace(paths=SimpleNamespace(vscode_workspaces=tmp_path))
    error = UserAbort("cancelled cleanup")

    def abort(*args, **kwargs):
        raise error

    monkeypatch.setattr("agentworks.ssh.SSHLogger", lambda *a, **k: MagicMock())
    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", abort)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", abort)
    with native_mutation_guard(db.path, vm.name) as guard:
        site = VMSiteNode("local", cast("VMPlatform", _StubPlatform()), (), MagicMock())
        node = LiveVMNode(db, config, MagicMock(), vm, site, held_native_guard=guard)
        pending = _pending(db, config, "new", node, kind)
        with pytest.raises(UserAbort) as caught:
            pending.teardown()
    assert caught.value is error
