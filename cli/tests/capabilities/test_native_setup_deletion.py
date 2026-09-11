"""Parent deletion ignores native receipts while excluding concurrent setup."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentworks.agents.manager import delete_agent
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.errors import StateError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import write_native_setup
from agentworks.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import Transport
from agentworks.vms.manager import delete_vm
from agentworks.workspaces.manager import delete_workspace


def _record(component) -> SetupRecord:
    return SetupRecord(
        component=component,
        integration="shell",
        destination_id="a" * 64,
        declaration={"config": {"name": "shell"}},
        complete=True,
        claims=(
            NativeClaim(role="fixture", identifier="first", destination="native"),
            NativeClaim(role="fixture", identifier="second", destination="native"),
        ),
    )


@pytest.fixture
def deletion(db, tmp_path, monkeypatch):
    vm = db.insert_vm("box", site="fixture", hostname="box", admin_username="admin")
    db.insert_agent("agent", "box", "agt-agent")
    db.insert_workspace("project", "/work/project", "box", "ws-project")
    registry = Registry.empty()
    origin = Origin.built_in(source="fixture")
    registry.add("harness-integration", "shell", HarnessIntegrationEntry("shell", origin), origin)
    registry.finalize()
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: registry)
    monkeypatch.setattr("agentworks.agents.manager.lifecycle.gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.workspaces.manager.delete.gated_vm_boundary", lambda *a, **k: nullcontext())
    target = Mock(spec=Transport)
    target.run.return_value = SSHResult(0, "native", "")
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.harness_setup.lifecycle.site_platform_name", lambda *a: "fixture")
    monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *a, **kw: "a" * 64)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.grants.revoke_workspace_grants", lambda *a, **k: None)
    from agentworks.agents.initializer import delete_agent_on_vm
    from agentworks.workspaces.backends.vm import delete_vm_workspace

    removed = []

    def remove_agent(*args, **kwargs):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "box"):
            pass
        removed.append("agent")

    def remove_workspace(*args, **kwargs):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "box"):
            pass
        removed.append("workspace")

    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", remove_agent)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", remove_workspace)
    logs = []

    class Logger:
        closed = False
        display_path = "fixture.log"

        def __init__(self, *a, **k):
            logs.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("agentworks.ssh.SSHLogger", Logger)
    config = SimpleNamespace(paths=SimpleNamespace(vscode_workspaces=tmp_path))
    return SimpleNamespace(
        vm=vm,
        registry=registry,
        target=target,
        config=config,
        removed=removed,
        logs=logs,
        delete_agent_on_vm=delete_agent_on_vm,
        delete_vm_workspace=delete_vm_workspace,
    )


def _store_receipt(db, kind, name, receipt) -> None:
    if receipt == "claims":
        component = "admin" if kind == "vm" else kind
        write_native_setup(db, kind, name, NativeSetupState(records=(_record(component),)), operation=f"{kind}-create")
    else:
        payload = (
            VersionedPayload(99, {"future": True}) if receipt == "unknown" else VersionedPayload(1, {"records": False})
        )
        db.instance_state.replace_applied_slices(
            kind, name, f"{kind}-create", {AppliedStateKey.HARNESS_NATIVE_SETUP: payload}
        )


@pytest.mark.parametrize("kind,name", [("agent", "agent"), ("workspace", "project")])
@pytest.mark.parametrize("receipt", ["claims", "unknown", "malformed"])
def test_parent_deletion_does_not_require_native_receipt_cleanup(db, deletion, monkeypatch, kind, name, receipt):
    _store_receipt(db, kind, name, receipt)
    monkeypatch.setattr(ShellIntegration, "user_init", lambda *a: pytest.fail("individual plugin cleanup"))
    monkeypatch.setattr(ShellIntegration, "workspace_init", lambda *a: pytest.fail("individual plugin cleanup"))
    if kind == "agent":
        delete_agent(db, deletion.config, name=name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
    else:
        delete_workspace(db, deletion.config, name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert deletion.removed == [kind]
    assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is None
    assert db.instance_state.get_applied_slices(kind, name) == ()
    assert deletion.logs[0].closed


@pytest.mark.parametrize("receipt", ["claims", "unknown", "malformed"])
@pytest.mark.parametrize("outcome", ["success", "binding-failed", "delete-failed"])
def test_vm_deletion_preserves_existing_backend_failure_policy(db, deletion, monkeypatch, receipt, outcome):
    owners = [("vm", "box"), ("agent", "agent"), ("workspace", "project")]
    for kind, name in owners:
        _store_receipt(db, kind, name, receipt)
    previous = {kind: db.instance_state.get_applied_slices(kind, name) for kind, name in owners}
    removed = []

    def remove(vm, ctx):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "box"):
            pass
        if outcome == "delete-failed":
            raise SSHError("platform refused")
        removed.append(vm.name)

    def boundary(*args, **kwargs):
        if outcome == "binding-failed":
            raise StateError("platform unavailable")
        return SimpleNamespace(site=SimpleNamespace(platform=SimpleNamespace(delete=remove))), Mock()

    monkeypatch.setattr("agentworks.vms.manager.power._live_vm_boundary", boundary)
    if outcome == "delete-failed":
        with pytest.raises(SSHError):
            delete_vm(db, deletion.config, "box", force=True, interaction=TtyInteractionPolicy.REFUSE)
        assert db.get_vm("box") and db.get_agent("agent") and db.get_workspace("project")
        for kind, name in owners:
            assert db.instance_state.get_applied_slices(kind, name) == previous[kind]
    else:
        delete_vm(db, deletion.config, "box", force=True, interaction=TtyInteractionPolicy.REFUSE)
        assert removed == (["box"] if outcome == "success" else [])
        assert db.get_vm("box") is None and db.get_agent("agent") is None and db.get_workspace("project") is None
        for kind, name in owners:
            assert db.instance_state.get_applied_slices(kind, name) == ()
    assert deletion.removed == []


@pytest.mark.parametrize("kind,name", [("agent", "agent"), ("workspace", "project"), ("vm", "box")])
def test_deletion_refuses_vm_family_contention_before_native_mutation(db, deletion, kind, name):
    with native_mutation_guard(db.path, "box"), pytest.raises(NativeSetupBusyError):
        if kind == "agent":
            delete_agent(db, deletion.config, name=name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        elif kind == "workspace":
            delete_workspace(db, deletion.config, name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        else:
            delete_vm(db, deletion.config, name, force=True, interaction=TtyInteractionPolicy.REFUSE)
    assert deletion.removed == [] and deletion.logs == []


def test_orphan_workspace_deletion_does_not_require_native_target(db, deletion):
    _store_receipt(db, "workspace", "project", "claims")
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'box'")
    db._conn.commit()
    delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_workspace("project") is None
    assert deletion.removed == []


@pytest.mark.parametrize("kind,name", [("agent", "agent"), ("workspace", "project")])
def test_remote_cleanup_failure_keeps_existing_best_effort_parent_deletion(db, deletion, monkeypatch, kind, name):
    _store_receipt(db, kind, name, "claims")
    deletion.target.run.side_effect = SSHError("unreachable")
    if kind == "agent":
        monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", deletion.delete_agent_on_vm)
        delete_agent(db, deletion.config, name=name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        assert db.get_agent(name) is None
    else:
        monkeypatch.setattr("agentworks.workspaces.backends.vm.delete_vm_workspace", deletion.delete_vm_workspace)
        db.update_vm_tailscale("box", "box")
        delete_workspace(db, deletion.config, name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        assert db.get_workspace(name) is None
    assert deletion.logs[0].closed


@pytest.mark.parametrize("receipt", ["none", "claims", "malformed", "future"])
def test_rehome_holds_its_vm_guard_without_interpreting_receipts(db, deletion, monkeypatch, receipt):
    from agentworks.workspaces.manager import rehome_workspace

    if receipt != "none":
        _store_receipt(db, "workspace", "project", receipt)
    previous = db.instance_state.get_applied_slices("workspace", "project")
    calls = []

    def move(*args, **kwargs):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "box"):
            pass
        calls.append("move")

    monkeypatch.setattr("agentworks.workspaces.manager.rehome._rehome_vm", move)
    rehome_workspace(
        db, deletion.config, "project", target_path="/new/project", yes=True, interaction=TtyInteractionPolicy.REFUSE
    )
    assert calls == ["move"]

    assert db.instance_state.get_applied_slices("workspace", "project") == previous
