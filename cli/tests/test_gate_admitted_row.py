"""Commands that open the activation gate run their body on the VM row the gate admitted.

The gate reloads its node's row, and the re-check under the guard compares
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
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.workspaces import manager as workspace_manager
from tests.workspaces._lifecycle_support import Guest, seed
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
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
