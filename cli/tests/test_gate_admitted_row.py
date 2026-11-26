"""Commands that open the activation gate run their body on the VM row the gate admitted.

The gate reloads its node's row, and the re-check under the lock set compares
that row with the database. A body that ran on the row the command read
before the gate would act on a value the re-check never saw: here a rekey
or recover gives the VM a new address while the gate probes it, and the
body must reach the new address.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.agents import manager as agent_manager
from agentworks.errors import ExternalError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.workspaces import manager as workspace_manager
from tests.workspaces._lifecycle_support import Guest, seed
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database, VMRow

NEW_ADDRESS = "100.64.0.77"


class _Stop(Exception):
    """Ends the body once it has shown which row it runs on."""


def _readdressed_while_the_gate_probes(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's reachability probe finds the VM up after another command gave it a new address."""

    def reachable(host: str) -> bool:
        db.update_vm_tailscale("box", NEW_ADDRESS)
        return True

    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", reachable)


def test_agent_delete_removes_the_account_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached: list[str | None] = []

    def delete_on_vm(vm: VMRow, *args: object, **kwargs: object) -> None:
        reached.append(vm.tailscale_host)
        raise _Stop

    monkeypatch.setattr("agentworks.agents.initializer.delete_agent_on_vm", delete_on_vm)

    with pytest.raises(_Stop):
        agent_manager.delete_agent(db, make_config(), name="a1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert reached == [NEW_ADDRESS]


def test_workspace_repair_converges_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached: list[str | None] = []

    def admin_access(vm: VMRow, *args: object, **kwargs: object) -> object:
        reached.append(vm.tailscale_host)
        raise _Stop

    monkeypatch.setattr("agentworks.workspaces.backends.vm.admin_access", admin_access)

    with pytest.raises(_Stop):
        workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert reached == [NEW_ADDRESS]


def _record_group_change(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str | None]:
    reached: list[str | None] = []

    def change(vm: VMRow, *args: object, **kwargs: object) -> None:
        reached.append(vm.tailscale_host)
        raise _Stop

    monkeypatch.setattr(agent_grants, name, change)
    return reached


def test_grant_adds_the_membership_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_group_change(monkeypatch, "add_to_workspace_group")

    with pytest.raises(_Stop):
        agent_grants.grant_workspaces(
            db, make_config(), agent_name="a1", workspace_names=["ws1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert reached == [NEW_ADDRESS]


def test_revoke_removes_the_membership_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_group_change(monkeypatch, "remove_from_workspace_group")

    with pytest.raises(_Stop):
        agent_grants.revoke_workspaces(
            db, make_config(), agent_name="a1", workspace_names=["ws1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert reached == [NEW_ADDRESS]


def _record_vm(monkeypatch: pytest.MonkeyPatch, target: str, *, row: str = "vm") -> list[str | None]:
    """Stop the command at ``target``, recording the address of the VM row it was handed."""
    reached: list[str | None] = []

    def first_body_step(*args: object, **kwargs: object) -> None:
        vm = kwargs[row] if row in kwargs else args[0]
        reached.append(getattr(vm, "tailscale_host", None))
        raise _Stop

    monkeypatch.setattr(target, first_body_step)
    return reached


def test_agent_create_realizes_the_agent_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_vm(monkeypatch, "agentworks.agents.realize.realize_agent")

    with pytest.raises(_Stop):
        agent_manager.create_agent(db, make_config(), name="a1", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    assert reached == [NEW_ADDRESS]


def test_agent_reinit_sets_the_agent_up_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_vm(monkeypatch, "agentworks.agents.initializer.create_agent_on_vm")

    # Reinit reports a non-agentworks failure of its setup as an ExternalError.
    with pytest.raises(ExternalError) as stopped:
        agent_manager.reinit_agent(db, make_config(), name="a1", interaction=TtyInteractionPolicy.REFUSE)

    assert isinstance(stopped.value.__cause__, _Stop)
    assert reached == [NEW_ADDRESS]


def test_workspace_create_walks_its_target_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_vm(monkeypatch, "agentworks.workspaces.locks.walk_trees")

    with pytest.raises(_Stop):
        workspace_manager.create_workspace(
            db, make_config(), name="ws2", vm_name="box", interaction=TtyInteractionPolicy.REFUSE
        )

    assert reached == [NEW_ADDRESS]


def test_session_create_walks_its_new_workspace_at_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions import manager as session_manager

    seed(db)
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached = _record_vm(monkeypatch, "agentworks.workspaces.locks.walk_trees")

    with pytest.raises(_Stop):
        session_manager.create_session(
            db,
            make_config(),
            name="s1",
            new_workspace=True,
            workspace_name="ws2",
            admin=True,
            vm_name="box",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert reached == [NEW_ADDRESS]


def _vm_shell(db: Database, config: object) -> object:
    return vm_manager.shell_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def _vm_exec(db: Database, config: object) -> object:
    return vm_manager.exec_vm(db, config, "box", ["true"], interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def _vm_port_forward(db: Database, config: object) -> object:
    from agentworks.vms.manager.port_forward import port_forward_vm

    return port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def _vm_backup(db: Database, config: object) -> object:
    from agentworks.vms.backup import backup_vm

    return backup_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def _agent_shell(db: Database, config: object) -> object:
    return agent_manager.shell_agent(db, config, name="a1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def _agent_exec(db: Database, config: object) -> object:
    return agent_manager.exec_agent(db, config, name="a1", command=["true"], interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "command",
    [_vm_shell, _vm_exec, _vm_port_forward, _vm_backup, _agent_shell, _agent_exec],
    ids=["vm-shell", "vm-exec", "vm-port-forward", "vm-backup", "agent-shell", "agent-exec"],
)
def test_an_access_command_reaches_the_address_the_gate_admitted(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    command: Callable[[Database, object], object],
) -> None:
    from agentworks.db import ProvisioningStatus

    seed(db)
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    _readdressed_while_the_gate_probes(db, monkeypatch)
    reached: list[str | None] = []

    def vm_access(vm: VMRow, *args: object, **kwargs: object) -> object:
        reached.append(vm.tailscale_host)
        raise _Stop

    monkeypatch.setattr("agentworks.execution.vm_access", vm_access)

    with pytest.raises(_Stop):
        command(db, make_config())

    assert reached == [NEW_ADDRESS]
