"""A stored record that needs an explicit replacement never takes down commands about other owners.

The live publisher projects such an owner identity-only, so every command
still loads its registry, each refused owner can be repaired in turn, and a
command acting on the refused owner itself refuses through its own decode.
Real config, registry and desired state; the platform's backend operations,
the on-VM mutation and the transports are fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents import initializer as agent_initializer
from agentworks.agents import manager as agent_manager
from agentworks.db import VersionedPayload, VMStatus
from agentworks.instance_specs import StoredSourceReferenceRefused
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import seed_vm_trust, stub_vm_ssh_identity
from tests.vm_access_support import install_access_recorder

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database, VMRow
    from agentworks.db.instance_state import JsonObject
    from tests.conftest import CapturedOutput

SECRET = "ghp-isolation-secret"
REFUSED: JsonObject = {"dotfiles_source": f"git::https://u:{SECRET}@example.com/dots.git"}


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)
    monkeypatch.setattr(agent_initializer, "create_new_agent_user", lambda *a, **k: None)
    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    install_access_recorder(monkeypatch)


def _vm(db: Database, name: str, *, admin: JsonObject | None = None) -> None:
    db.insert_vm(name, site="proxmox", hostname=name)
    db.update_vm_tailscale(name, "100.64.0.9")
    seed_vm_trust(name)
    if admin is not None:
        db.instance_state.put_desired_overlay("vm", name, VersionedPayload(2, {"vm": {}, "admin": admin}))


def _agent(db: Database, name: str, *, layer: JsonObject | None = None) -> None:
    db.insert_agent(name, "box", f"agt-{name}", template="default")
    if layer is not None:
        db.instance_state.put_desired_overlay("agent", name, VersionedPayload(1, layer))


def _reinit_agent(db: Database, config: object, name: str, spec: str | None = None) -> None:
    agent_manager.reinit_agent(db, config, name=name, spec=spec, interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def test_two_refused_agents_are_each_repaired_in_turn(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    _vm(db, "box")
    _agent(db, "a1", layer=REFUSED)
    _agent(db, "a2", layer=REFUSED)

    _reinit_agent(db, config, "a1", spec="")
    assert db.instance_state.get_desired_overlay("agent", "a1") is None
    with pytest.raises(StoredSourceReferenceRefused):
        _reinit_agent(db, config, "a2")

    _reinit_agent(db, config, "a2", spec='{"shell":"zsh"}')
    stored = db.instance_state.get_desired_overlay("agent", "a2")
    assert stored is not None and stored.payload.value == {"shell": "zsh"}
    assert SECRET not in str(captured_output.lines)


def test_a_refused_agent_is_repaired_beside_a_refused_vm(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
) -> None:
    """The VM side, repaired beside a refused agent, is in ``tests/vms/test_reinit_admin_options.py``."""
    config = make_config()
    _vm(db, "box")
    _vm(db, "other", admin=REFUSED)
    _agent(db, "a1", layer=REFUSED)

    _reinit_agent(db, config, "a1", spec="{}")

    assert db.instance_state.get_desired_overlay("agent", "a1") is None
    assert SECRET not in str(captured_output.lines)


def test_unrelated_commands_work_beside_a_refused_record(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
) -> None:
    from agentworks.bootstrap import load_request_registry

    config = make_config()
    _vm(db, "box", admin=REFUSED)
    _agent(db, "a1", layer=REFUSED)
    _agent(db, "a2")

    registry = load_request_registry(config, live_database=db)
    vm_manager.list_vms(db)
    agent_manager.delete_agent(db, config, name="a2", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    # The refused owners are published identity-only: their selections and owner edges, no layer references.
    agent = registry.lookup("agent", "a1")
    assert {(ref.kind, ref.name) for ref in agent.outbound} == {("vm", "box"), ("agent-template", "default")}
    assert db.get_agent("a2") is None
    assert SECRET not in str(captured_output.lines)


def test_deleting_the_refused_agent_removes_it_and_its_record(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
) -> None:
    """Delete never reads the agent's layer, so a refused one goes with its row."""
    config = make_config()
    _vm(db, "box")
    _agent(db, "a1", layer=REFUSED)

    agent_manager.delete_agent(db, config, name="a1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_agent("a1") is None
    assert db.instance_state.get_desired_overlay("agent", "a1") is None
    assert SECRET not in str(captured_output.lines)


def test_deleting_the_refused_vm_deletes_its_backend_and_removes_it(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The site binds, so the backend delete runs rather than a binding failure being reported."""
    deleted: list[str] = []

    def _delete(self: ProxmoxPlatform, row: VMRow, ctx: object) -> None:
        deleted.append(row.name)

    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "delete", _delete)
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)
    config = make_config()
    _vm(db, "box", admin=REFUSED)

    vm_manager.delete_vm(db, config, "box", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert deleted == ["box"]
    assert db.get_vm("box") is None
    assert db.instance_state.get_desired_overlay("vm", "box") is None
    assert captured_output.warnings == []
    assert SECRET not in str(captured_output.lines)


def test_a_layer_setting_only_a_refused_settings_source_is_published_identity_only(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output: CapturedOutput,
) -> None:
    """The layer inherits the rest of its settings from its template, so its own validation also misses them."""
    from agentworks.bootstrap import load_request_registry
    from tests.conftest import ManifestDoc
    from tests.orchestrated_fixtures import proxmox_site, write_operator_config

    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    base = {"harness_integrations": {"codex": {"settings": {"source": str(settings), "strategy": "replace"}}}}
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    config = write_operator_config(
        tmp_path,
        '[plugins]\nsystem = ["proxmox", "codex"]\n',
        manifests=[
            proxmox_site(),
            ManifestDoc("agent-template", "default", base),
            ManifestDoc("admin-template", "default", base),
        ],
    )
    partial: JsonObject = {
        "harness_integrations": {"codex": {"settings": {"source": f"u:{SECRET}@example.com:settings.json"}}}
    }
    _vm(db, "box")
    _vm(db, "other", admin=partial)
    _agent(db, "a1", layer=partial)

    registry = load_request_registry(config, live_database=db)

    assert {(ref.kind, ref.name) for ref in registry.lookup("agent", "a1").outbound} == {
        ("vm", "box"),
        ("agent-template", "default"),
    }
    assert {(ref.kind, ref.name) for ref in registry.lookup("vm", "other").outbound} == {
        ("vm-site", "proxmox"),
        ("vm-template", "default"),
        ("admin-template", "default"),
    }
    with pytest.raises(StoredSourceReferenceRefused):
        _reinit_agent(db, config, "a1")
    assert SECRET not in str(captured_output.lines)
