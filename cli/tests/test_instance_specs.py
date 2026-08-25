"""Strict instance specs, shared layer provenance, and atomic owner composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.agents.templates import resolve_live_template_with_provenance as resolve_live_agent
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.errors import StateError, ValidationError
from agentworks.instance_specs import parse_instance_spec, refuse_orphan_creation_state, replace_agent_overlay
from agentworks.resources.inheritance import LayerSourceKind
from agentworks.resources.registry import Registry
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict_with_provenance as resolve_session
from agentworks.sessions.templates import resolve_live_template_with_provenance as resolve_live_session
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm
from agentworks.vms.templates import resolve_live_template_with_provenance as resolve_live_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import resolve_from_dict_with_provenance as resolve_workspace
from agentworks.workspaces.templates import resolve_live_template_with_provenance as resolve_live_workspace
from agentworks.workspaces.templates import resolve_live_tmuxinator

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.mark.parametrize("value", ["", "null", "[]", "1", '"x"'])
def test_spec_requires_a_nonempty_json_object(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec("vm", value)


@pytest.mark.parametrize(
    "value",
    [
        '{"cpus":1,"cpus":2}',
        '{"env":{"A":"x","A":"y"}}',
        '{"cpus":NaN}',
        '{"cpus":Infinity}',
        '{"cpus":1e400}',
        '{"cpus":1} trailing',
        '{"env":{"A":null}}',
    ],
)
def test_spec_rejects_json_extensions_and_null_recursively(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec("vm", value)


def test_deep_recursive_null_is_a_typed_validation_error() -> None:
    value = '{"env":{"A":' + "[" * 900 + "null" + "]" * 900 + "}}"

    with pytest.raises(ValidationError):
        parse_instance_spec("vm", value)


def test_huge_json_integer_is_a_typed_validation_error() -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec("vm", '{"cpus":' + "1" * 5000 + "}")


@pytest.mark.parametrize(
    "field",
    ["kind", "name", "inherits", "description", "framework", "metadata", "declared_at", "origin", "source"],
)
def test_spec_refuses_framework_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec("workspace", f'{{"{field}":"secret-value"}}')


def test_spec_accepts_surrounding_whitespace_and_canonicalizes_fields() -> None:
    overlay = parse_instance_spec("vm", ' \n { "memory": 16, "cpus": 8 } \t ')

    assert overlay.payload == VersionedPayload(1, {"cpus": 8, "memory": 16})
    assert overlay.fields == ("cpus", "memory")


def test_validation_error_does_not_echo_plaintext_environment_value() -> None:
    plaintext = "do-not-print-this-value"

    with pytest.raises(ValidationError) as caught:
        parse_instance_spec("agent", f'{{"env":{{"TOKEN":{{"unexpected":"{plaintext}"}}}}}}')

    assert plaintext not in str(caught.value)


def test_empty_object_is_a_typed_but_unpersisted_layer() -> None:
    overlay = parse_instance_spec("session", "{}")

    assert overlay.payload.value == {}
    assert isinstance(overlay.declaration, SessionTemplate)


@pytest.mark.parametrize("kind", ["vm", "workspace", "agent", "session"])
def test_create_refuses_an_orphan_desired_overlay(db: Database, kind: str) -> None:
    db.instance_state.put_desired_overlay(kind, "orphan", VersionedPayload(1, {"env": {}}))  # type: ignore[arg-type]

    with pytest.raises(StateError):
        refuse_orphan_creation_state(db, kind, "orphan")  # type: ignore[arg-type]


def test_create_refuses_orphan_applied_state(db: Database) -> None:
    db.instance_state.replace_applied_slices(
        "vm",
        "orphan",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {"cpus": "template"})},
    )

    with pytest.raises(StateError):
        refuse_orphan_creation_state(db, "vm", "orphan")


def test_all_domain_folds_append_the_instance_layer() -> None:
    vm_overlay = cast("VMTemplate", parse_instance_spec("vm", '{"cpus":8,"apt":["git"]}').declaration)
    workspace_overlay = cast(
        "WorkspaceTemplate",
        parse_instance_spec("workspace", '{"tmuxinator":false}').declaration,
    )
    agent_overlay = cast(
        "AgentTemplate",
        parse_instance_spec("agent", '{"shell":"zsh","mise_packages":["node@22"]}').declaration,
    )
    session_overlay = cast(
        "SessionTemplate",
        parse_instance_spec("session", '{"env":{"MODE":"instance"}}').declaration,
    )

    vm = resolve_vm(
        {"base": VMTemplate(name="base", cpus=6, apt=["git"])},
        "base",
        overlay=vm_overlay,
        instance_name="v1",
    )
    workspace = resolve_workspace({}, overlay=workspace_overlay, instance_name="w1")
    agent = resolve_agent({}, overlay=agent_overlay, instance_name="a1")
    session = resolve_session({}, overlay=session_overlay, instance_name="s1")

    assert vm.value.cpus == 8
    assert vm.value.apt == ["git"]
    assert [source.kind for source in vm.provenance[("apt", "git")]] == [
        LayerSourceKind.TEMPLATE,
        LayerSourceKind.INSTANCE,
    ]
    assert vm.provenance[("cpus",)][-1].resource_kind == "vm"
    assert workspace.value.tmuxinator is False
    assert workspace.provenance[("tmuxinator",)][-1].resource_kind == "workspace"
    assert agent.value.shell == "zsh"
    assert agent.provenance[("mise_packages", "node@22")][-1].resource_kind == "agent"
    assert session.value.env["MODE"].model_dump() == {"value": "instance"}
    assert session.provenance[("env", "MODE")][-1].resource_kind == "session"


def test_all_live_domain_resolvers_expose_stored_instance_provenance(db: Database) -> None:
    registry = Registry.empty()
    overlays = {
        "vm": parse_instance_spec("vm", '{"cpus":8}'),
        "workspace": parse_instance_spec("workspace", '{"tmuxinator":false}'),
        "agent": parse_instance_spec("agent", '{"shell":"zsh"}'),
        "session": parse_instance_spec("session", '{"env":{"MODE":"instance"}}'),
    }
    for kind, overlay in overlays.items():
        db.instance_state.put_desired_overlay(kind, f"{kind}-1", overlay.payload)  # type: ignore[arg-type]

    resolutions = (
        (resolve_live_vm(db, registry, "vm-1", None), ("cpus",)),
        (resolve_live_workspace(db, registry, "workspace-1", None), ("tmuxinator",)),
        (resolve_live_agent(db, registry, "agent-1", None), ("shell",)),
        (resolve_live_session(db, registry, "session-1", None), ("env", "MODE")),
    )
    for resolution, path in resolutions:
        assert resolution.provenance[path][-1].kind is LayerSourceKind.INSTANCE


def test_live_tmuxinator_preserves_missing_base_compatibility_and_honors_explicit_overlay(
    db: Database,
) -> None:
    registry = Registry.empty()

    assert resolve_live_tmuxinator(db, registry, "copied", "copied") is True

    overlay = parse_instance_spec("workspace", '{"tmuxinator":false}')
    db.instance_state.put_desired_overlay("workspace", "copied", overlay.payload)
    assert resolve_live_tmuxinator(db, registry, "copied", "copied") is False


def test_owner_and_overlay_insert_rollback_together(db: Database) -> None:
    with pytest.raises(RuntimeError, match="rollback"), db.transaction():
        db.insert_vm("v1", site="lima-local", hostname="v1")
        db.instance_state.put_desired_overlay("vm", "v1", VersionedPayload(1, {"cpus": 8}))
        raise RuntimeError("rollback")

    assert db.get_vm("v1") is None
    assert db.instance_state.get_desired_overlay("vm", "v1") is None


def test_agent_template_and_overlay_replacement_rollback_together(db: Database) -> None:
    db.insert_vm("v1", site="lima-local", hostname="v1")
    db.insert_agent("a1", "v1", "agt-a1", template="old")
    old = parse_instance_spec("agent", '{"shell":"bash"}')
    db.instance_state.put_desired_overlay("agent", "a1", old.payload)
    replacement = parse_instance_spec("agent", '{"shell":"zsh"}')

    with pytest.raises(RuntimeError, match="rollback"), db.transaction():
        db.update_agent_template("a1", "new")
        replace_agent_overlay(db, "a1", replacement, supplied=True)
        raise RuntimeError("rollback")

    assert db.get_agent("a1").template == "old"  # type: ignore[union-attr]
    assert db.instance_state.get_desired_overlay("agent", "a1").payload == old.payload  # type: ignore[union-attr]
