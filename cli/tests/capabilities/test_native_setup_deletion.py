"""Owner deletion consumes recorded claims under the VM-family mutation guard."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentworks.agents.manager import delete_agent
from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import StateError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, write_native_setup
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
    return SimpleNamespace(vm=vm, registry=registry, target=target, config=config, removed=removed, logs=logs)


@pytest.mark.parametrize("kind,name", [("agent", "agent"), ("workspace", "project")])
@pytest.mark.parametrize("outcome", ["success", "unavailable", "interrupted", "retained", "changed"])
def test_retirement_precedes_native_owner_destruction_and_preserves_failures(
    db, deletion, monkeypatch, kind, name, outcome
):
    previous = _record(kind)
    write_native_setup(db, kind, name, NativeSetupState(records=(previous,)), operation=f"{kind}-create")
    calls = []

    def retire(self, invocation):
        assert self.retiring and self.config_secret_refs() == ()
        assert invocation.secrets == {}
        assert deletion.removed == []
        calls.append(invocation)
        if outcome == "interrupted":
            invocation.checkpoint(previous.claims[1:])
            raise SSHError("interrupted cleanup")
        if outcome == "success":
            invocation.checkpoint(())

    monkeypatch.setattr(ShellIntegration, "user_init" if kind == "agent" else "workspace_init", retire)
    if outcome == "unavailable":

        def disabled(*args):
            raise StateError("integration unavailable")

        monkeypatch.setattr("agentworks.harness_setup.dispatch.ensure_harness_integration_enabled", disabled)
    if outcome == "changed":
        monkeypatch.setattr("agentworks.harness_setup.dispatch.destination_id", lambda *a, **kw: "b" * 64)

    def delete() -> None:
        if kind == "agent":
            delete_agent(db, deletion.config, name=name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        else:
            delete_workspace(db, deletion.config, name, yes=True, interaction=TtyInteractionPolicy.REFUSE)

    if outcome == "success":
        delete()
        assert deletion.removed == [kind]
        assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is None
        assert read_native_setup(db, kind, name).records == ()
    else:
        with pytest.raises((StateError, SSHError)):
            delete()
        assert deletion.removed == []
        assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is not None
        (retained,) = read_native_setup(db, kind, name).records
        assert retained.pending_cleanup and not retained.complete
        assert retained.claims == (previous.claims[1:] if outcome == "interrupted" else previous.claims)
    assert len(calls) == (0 if outcome in {"unavailable", "changed"} else 1)
    assert deletion.logs[0].closed


@pytest.mark.parametrize("outcome", ["success", "binding-failed", "delete-failed"])
def test_vm_deletion_requires_confirmed_native_destruction_for_its_whole_family(db, deletion, monkeypatch, outcome):
    for kind, name, component in [
        ("vm", "box", "admin"),
        ("agent", "agent", "agent"),
        ("workspace", "project", "workspace"),
    ]:
        write_native_setup(db, kind, name, NativeSetupState(records=(_record(component),)), operation=f"{kind}-create")
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
    if outcome == "success":
        delete_vm(db, deletion.config, "box", force=True, interaction=TtyInteractionPolicy.REFUSE)
        assert removed == ["box"]
        assert db.get_vm("box") is None and db.get_agent("agent") is None and db.get_workspace("project") is None
    else:
        with pytest.raises((StateError, SSHError)):
            delete_vm(db, deletion.config, "box", force=True, interaction=TtyInteractionPolicy.REFUSE)
        assert (
            db.get_vm("box") is not None
            and db.get_agent("agent") is not None
            and db.get_workspace("project") is not None
        )
        for kind, name in [("vm", "box"), ("agent", "agent"), ("workspace", "project")]:
            (record,) = read_native_setup(db, kind, name).records
            assert record.pending_cleanup and record.claims
    assert deletion.removed == []  # Native VM destruction needs no per-user provisioning.


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


def test_orphan_workspace_with_receipts_keeps_evidence_without_native_target(db, deletion):
    write_native_setup(
        db, "workspace", "project", NativeSetupState(records=(_record("workspace"),)), operation="workspace-create"
    )
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'box'")
    db._conn.commit()
    with pytest.raises(StateError):
        delete_workspace(db, deletion.config, "project", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_workspace("project") is not None
    assert read_native_setup(db, "workspace", "project").records[0].pending_cleanup
    assert deletion.removed == []


@pytest.mark.parametrize("kind,name", [("agent", "agent"), ("workspace", "project")])
def test_native_removal_failure_keeps_owner_without_recreating_retired_claims(db, deletion, monkeypatch, kind, name):
    write_native_setup(db, kind, name, NativeSetupState(records=(_record(kind),)), operation=f"{kind}-create")
    retired = []

    def retire(self, invocation):
        assert self.retiring
        invocation.checkpoint(())
        retired.append(self.name)

    monkeypatch.setattr(ShellIntegration, "user_init" if kind == "agent" else "workspace_init", retire)
    method = (
        "agentworks.agents.initializer.delete_agent_on_vm"
        if kind == "agent"
        else "agentworks.workspaces.backends.vm.delete_vm_workspace"
    )

    def failed(*args, **kwargs):
        raise SSHError("native removal failed")

    monkeypatch.setattr(method, failed)

    def delete() -> None:
        if kind == "agent":
            delete_agent(db, deletion.config, name=name, yes=True, interaction=TtyInteractionPolicy.REFUSE)
        else:
            delete_workspace(db, deletion.config, name, yes=True, interaction=TtyInteractionPolicy.REFUSE)

    with pytest.raises(SSHError):
        delete()
    assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is not None
    assert read_native_setup(db, kind, name).records == ()
    monkeypatch.setattr(method, lambda *a, **k: None)
    delete()
    assert retired == ["shell"]
    assert (db.get_agent(name) if kind == "agent" else db.get_workspace(name)) is None


@pytest.mark.parametrize("state", ["present", "absent", "home-remains", "unreachable"])
def test_agent_native_deletion_requires_account_and_home_removal(db, monkeypatch, state):
    from agentworks.agents.initializer import delete_agent_on_vm

    vm = db.insert_vm("vm", site="fixture", hostname="vm")
    target = Mock(spec=Transport)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith("getent"):
            return SSHResult(255 if state == "unreachable" else 0 if state == "present" else 2, "", "")
        if command.startswith("test") and state == "home-remains":
            raise SSHError("home remains")
        if command.startswith("userdel"):
            raise SSHError("account removal failed")
        return SSHResult(0, "", "")

    target.run.side_effect = run
    monkeypatch.setattr("agentworks.agents.initializer.transport", lambda *a, **k: target)
    if state == "absent":
        delete_agent_on_vm(vm, Mock(), "agt-a")
        assert not any(command.startswith("userdel") for command in commands)
    else:
        with pytest.raises(SSHError):
            delete_agent_on_vm(vm, Mock(), "agt-a")


def test_rehome_refuses_native_receipts_before_probes_and_preserves_destination(db, deletion, monkeypatch):
    from agentworks.workspaces.manager import rehome_workspace

    previous = NativeSetupState(records=(_record("workspace"),))
    write_native_setup(db, "workspace", "project", previous, operation="workspace-create")
    mutation = Mock()
    monkeypatch.setattr("agentworks.workspaces.manager.rehome._rehome_vm", mutation)
    monkeypatch.setattr("agentworks.sessions.manager.ensure_pids_batch", lambda *a, **k: pytest.fail("session probe"))
    with pytest.raises(StateError):
        rehome_workspace(
            db,
            deletion.config,
            "project",
            target_path="/new/project",
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    mutation.assert_not_called()
    assert db.get_workspace("project").workspace_path == "/work/project"
    assert read_native_setup(db, "workspace", "project") == previous


def test_rehome_without_receipts_holds_its_vm_guard(db, deletion, monkeypatch):
    from agentworks.workspaces.manager import rehome_workspace

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
