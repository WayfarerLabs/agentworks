"""Strict instance specs, shared layer provenance, and atomic owner composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.agents.templates import resolve_live_template_with_provenance as resolve_live_agent
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.declared_resource import DeclaredResource
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS, UnsupportedOverlayFieldsError
from agentworks.instance_specs import (
    UnsupportedStoredOverlayError,
    decode_stored_overlay,
    decode_stored_vm_overlays,
    parse_instance_spec,
    parse_vm_instance_specs,
    persist_vm_creation_overlays,
    refuse_orphan_creation_state,
    replace_agent_overlay,
)
from agentworks.resources.inheritance import LayerSourceKind
from agentworks.resources.registry import Registry
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict_with_provenance as resolve_session
from agentworks.sessions.templates import resolve_live_template_with_provenance as resolve_live_session
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm
from agentworks.vms.templates import resolve_live_template_with_provenance as resolve_live_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import resolve_from_dict_with_provenance as resolve_workspace
from agentworks.workspaces.templates import resolve_live_template_with_provenance as resolve_live_workspace
from agentworks.workspaces.templates import resolve_live_tmuxinator
from agentworks.workspaces.templates import resolve_template as resolve_workspace_template

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


def test_deep_session_capability_config_is_a_typed_validation_error() -> None:
    nested = "[" * 300 + "1" + "]" * 300

    with pytest.raises(ValidationError) as caught:
        parse_instance_spec("session", f'{{"harness_integration":{{"name":"shell","extra":{nested}}}}}')

    assert caught.value.entity_kind == "session"


def test_overlay_exclusions_follow_declared_resource_metadata() -> None:
    assert frozenset(DeclaredResource.model_fields) <= OVERLAY_EXCLUDED_FIELDS
    assert OVERLAY_EXCLUDED_FIELDS.isdisjoint({"cpus", "harness_integration", "shell", "tmuxinator"})


@pytest.mark.parametrize(
    "field",
    [
        "apiVersion",
        "kind",
        "name",
        "inherits",
        "description",
        "expires",
        "framework",
        "metadata",
        "source",
        "spec",
        "declared_at",
        "origin",
    ],
)
def test_spec_refuses_framework_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec("workspace", f'{{"{field}":"secret-value"}}')


def test_spec_accepts_surrounding_whitespace_and_canonicalizes_fields() -> None:
    overlay = parse_instance_spec("vm", ' \n { "memory": 16, "cpus": 8 } \t ')

    assert overlay.payload == VersionedPayload(1, {"cpus": 8, "memory": 16})
    assert overlay.fields == ("cpus", "memory")


def test_vm_specs_canonicalize_two_independently_typed_components() -> None:
    overlays = parse_vm_instance_specs(
        '{"cpus":8}',
        '{"shell":"zsh","env":{"TOKEN":{"secret":"admin-token"}}}',
    )

    assert overlays is not None
    assert overlays.payload == VersionedPayload(
        1,
        {
            "vm": {"cpus": 8},
            "admin": {"env": {"TOKEN": {"secret": "admin-token"}}, "shell": "zsh"},
        },
    )
    assert overlays.fields == ("admin.env", "admin.shell", "vm.cpus")


def test_empty_vm_and_admin_specs_are_one_absent_desired_decision() -> None:
    overlays = parse_vm_instance_specs("{}", "{}")

    assert overlays is not None and overlays.is_empty
    assert overlays.payload.value == {"vm": {}, "admin": {}}


def test_empty_vm_and_admin_specs_are_not_persisted(db: Database) -> None:
    overlays = parse_vm_instance_specs("{}", "{}")

    assert persist_vm_creation_overlays(db, "vm-1", overlays) is None
    assert db.instance_state.get_desired_overlay("vm", "vm-1") is None


def test_validation_error_does_not_echo_plaintext_environment_value() -> None:
    plaintext = "do-not-print-this-value"

    with pytest.raises(ValidationError) as caught:
        parse_instance_spec("agent", f'{{"env":{{"TOKEN":{{"unexpected":"{plaintext}"}}}}}}')

    assert plaintext not in str(caught.value)


def test_unknown_desired_field_is_strict_and_classified_as_unsupported() -> None:
    plaintext = "do-not-print-this-value"

    with pytest.raises(UnsupportedOverlayFieldsError) as caught:
        parse_instance_spec("agent", f'{{"future_field":"{plaintext}"}}')

    assert plaintext not in str(caught.value)


def test_stored_unknown_field_and_payload_version_are_unsupported_state(db: Database) -> None:
    records = (
        db.instance_state.put_desired_overlay("agent", "future-field", VersionedPayload(1, {"future_field": True})),
        db.instance_state.put_desired_overlay(
            "agent",
            "future-and-invalid",
            VersionedPayload(1, {"future_field": True, "env": {"TOKEN": {"unexpected": "value"}}}),
        ),
        db.instance_state.put_desired_overlay(
            "agent",
            "future-and-invalid-root-sibling",
            VersionedPayload(1, {"future_field": True, "shell": 5}),
        ),
        db.instance_state.put_desired_overlay("agent", "future-version", VersionedPayload(2, {"shell": "zsh"})),
    )

    for record in records:
        with pytest.raises(UnsupportedStoredOverlayError) as caught:
            decode_stored_overlay(record)
        assert caught.value.hint is not None


def test_stored_nested_unknown_field_is_unsupported_state(db: Database) -> None:
    record = db.instance_state.put_desired_overlay(
        "workspace",
        "future-nested-field",
        VersionedPayload(1, {"env": {"TOKEN": {"secret": "token", "future_option": True}}}),
    )

    with pytest.raises(UnsupportedStoredOverlayError):
        decode_stored_overlay(record)


def test_malformed_stored_overlay_remains_generic_broken_state(db: Database) -> None:
    plaintext = "do-not-print-this-value"
    record = db.instance_state.put_desired_overlay(
        "agent",
        "malformed",
        VersionedPayload(1, {"env": {"TOKEN": {"unexpected": plaintext}}}),
    )

    with pytest.raises(StateError) as caught:
        decode_stored_overlay(record)

    assert type(caught.value) is StateError
    assert plaintext not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        {"vm": {"future_vm_field": True}, "admin": {"shell": 5}},
        {"vm": {"cpus": "many"}, "admin": {"future_admin_field": True}},
    ],
)
def test_stored_vm_future_component_dominates_malformed_sibling(
    db: Database,
    value: dict[str, object],
) -> None:
    record = db.instance_state.put_desired_overlay("vm", "mixed", VersionedPayload(1, value))  # type: ignore[arg-type]

    with pytest.raises(UnsupportedStoredOverlayError):
        decode_stored_vm_overlays(record)


@pytest.mark.parametrize(
    "value",
    [
        {"vm": {}},
        {"vm": [], "admin": {}},
        {"vm": {}, "admin": "invalid"},
    ],
)
def test_malformed_stored_vm_composite_is_broken_state(
    db: Database,
    value: dict[str, object],
) -> None:
    record = db.instance_state.put_desired_overlay("vm", "malformed", VersionedPayload(1, value))  # type: ignore[arg-type]

    with pytest.raises(StateError) as caught:
        decode_stored_vm_overlays(record)

    assert type(caught.value) is StateError


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


def test_admin_final_layer_uses_scalar_map_and_unique_append_semantics() -> None:
    overlays = parse_vm_instance_specs(
        None,
        """{
          "shell":"zsh",
          "git_credentials":["shared","instance"],
          "user_install_commands":["instance-command"],
          "env":{"SHARED":"instance","INSTANCE":"yes"}
        }""",
    )
    assert overlays is not None and overlays.admin is not None

    resolution = resolve_admin(
        {
            "ops": AdminConfig(
                name="ops",
                shell="bash",
                git_credentials=["base", "shared"],
                user_install_commands=["base-command"],
                env={"SHARED": "base", "BASE": "yes"},
            )
        },
        "ops",
        overlay=overlays.admin,
        instance_name="vm-1",
    )

    assert resolution.value.shell == "zsh"
    assert resolution.value.git_credentials == ["base", "shared", "instance"]
    assert resolution.value.user_install_commands == ["base-command", "instance-command"]
    assert resolution.value.env["BASE"].value == "yes"
    assert resolution.value.env["SHARED"].value == "instance"
    assert resolution.provenance[("git_credentials", "shared")][-1].resource_kind == "vm"
    assert resolution.provenance[("env", "SHARED")][-1].resource_kind == "vm"


def test_admin_partial_validation_defers_until_after_template_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.config import validation

    def require_template_age(packages: list[str], lockfile: str | None, install_before: str) -> None:
        if packages and install_before == "7d":
            raise ValueError("template-specific install age required")

    monkeypatch.setattr(validation, "check_mise_settings", require_template_age)
    overlays = parse_vm_instance_specs(None, '{"mise_packages":["node@22"]}')
    assert overlays is not None and overlays.admin is not None

    resolution = resolve_admin(
        {"ops": AdminConfig(name="ops", mise_install_before="30d")},
        "ops",
        overlay=overlays.admin,
        instance_name="vm-1",
    )

    assert resolution.value.mise_packages == ["node@22"]
    assert resolution.value.mise_install_before == "30d"


def test_admin_effective_validation_rejects_invalid_folded_declaration() -> None:
    overlays = parse_vm_instance_specs(None, '{"mise_install_before":"invalid"}')
    assert overlays is not None and overlays.admin is not None

    with pytest.raises(ValidationError) as caught:
        resolve_admin({}, overlay=overlays.admin, instance_name="vm-1")

    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "vm-1"


def test_all_live_domain_resolvers_expose_stored_instance_provenance(db: Database) -> None:
    registry = Registry.empty()
    overlays = {
        "vm": parse_vm_instance_specs('{"cpus":8}', None),
        "workspace": parse_instance_spec("workspace", '{"tmuxinator":false}'),
        "agent": parse_instance_spec("agent", '{"shell":"zsh"}'),
        "session": parse_instance_spec("session", '{"env":{"MODE":"instance"}}'),
    }
    for kind, overlay in overlays.items():
        assert overlay is not None
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


def test_live_workspace_overlay_survives_a_missing_selected_base(db: Database) -> None:
    overlay = parse_instance_spec("workspace", '{"env":{"OVERLAY_ONLY":"available"}}')
    db.instance_state.put_desired_overlay("workspace", "copied", overlay.payload)

    resolution = resolve_live_workspace(db, Registry.empty(), "copied", "removed-template")

    assert resolution.value.env["OVERLAY_ONLY"].value == "available"
    assert resolution.provenance[("env", "OVERLAY_ONLY")][-1].kind is LayerSourceKind.INSTANCE


def test_new_workspace_overlay_does_not_hide_an_unknown_selected_template() -> None:
    overlay = cast("WorkspaceTemplate", parse_instance_spec("workspace", '{"tmuxinator":false}').declaration)

    with pytest.raises(NotFoundError):
        resolve_workspace_template(Registry.empty(), "typo", overlay=overlay, instance_name="new-workspace")


def test_owner_and_overlay_insert_rollback_together(db: Database) -> None:
    overlays = parse_vm_instance_specs('{"cpus":8}', '{"shell":"zsh"}')
    assert overlays is not None
    with pytest.raises(RuntimeError, match="rollback"), db.transaction():
        db.insert_vm("v1", site="lima-local", hostname="v1")
        db.instance_state.put_desired_overlay("vm", "v1", overlays.payload)
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


@pytest.mark.parametrize("supplied", [False, True])
def test_agent_overlay_retain_and_nonempty_replace_remain_strict_for_unsupported_state(
    db: Database,
    supplied: bool,
) -> None:
    db.instance_state.put_desired_overlay("agent", "a1", VersionedPayload(1, {"future_field": True}))
    replacement = parse_instance_spec("agent", '{"shell":"zsh"}') if supplied else None

    with pytest.raises(UnsupportedStoredOverlayError):
        replace_agent_overlay(db, "a1", replacement, supplied=supplied)

    stored = db.instance_state.get_desired_overlay("agent", "a1")
    assert stored is not None and stored.payload.value == {"future_field": True}


def test_agent_overlay_clear_reports_valid_fields_but_not_unsupported_keys(db: Database) -> None:
    empty = parse_instance_spec("agent", "{}")
    valid = parse_instance_spec("agent", '{"shell":"zsh"}')
    db.instance_state.put_desired_overlay("agent", "a1", valid.payload)

    valid_outcome = replace_agent_overlay(db, "a1", empty, supplied=True)

    assert valid_outcome is not None and valid_outcome.fields == ("shell",)

    db.instance_state.put_desired_overlay("agent", "a1", VersionedPayload(1, {"future_field": True}))

    unsupported_outcome = replace_agent_overlay(db, "a1", empty, supplied=True)

    assert unsupported_outcome is not None and unsupported_outcome.fields == ()


def test_agent_overlay_clear_does_not_remove_generic_malformed_state(db: Database) -> None:
    db.instance_state.put_desired_overlay(
        "agent",
        "a1",
        VersionedPayload(1, {"env": {"TOKEN": {"unexpected": "value"}}}),
    )

    with pytest.raises(StateError) as caught:
        replace_agent_overlay(db, "a1", parse_instance_spec("agent", "{}"), supplied=True)

    assert type(caught.value) is StateError
    assert db.instance_state.get_desired_overlay("agent", "a1") is not None
