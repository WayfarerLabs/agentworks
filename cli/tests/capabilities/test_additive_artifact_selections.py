"""Selection inheritance at native setup and session configuration boundaries."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.instance_specs import decode_stored_overlay, parse_instance_spec, parse_vm_instance_specs
from agentworks.manifests.emit import document_schema
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict_with_provenance as resolve_session
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from tests.manifests._specs import decode

SESSION_SELECTIONS = [
    ("claude-code", ["session-skill-plugin", "session-agent-definitions"]),
    ("codex", ["session-home-skills", "session-agent-config"]),
    ("grok-build", ["session-home-skills"]),
]


@pytest.mark.parametrize("integration,names", SESSION_SELECTIONS)
@pytest.mark.parametrize("instance", [False, True])
@pytest.mark.parametrize("mode", ["omitted", "empty", "append", "clear", "select-again"])
def test_session_selection_inheritance_preserves_null_and_extra_args_replacement(integration, names, instance, mode):
    parent = SessionTemplate.model_validate(
        {
            "name": "base",
            "harness_integration": {
                "name": integration,
                "enabled_workarounds": names[:1],
                "extra_args": ["--parent", "parent value"],
            },
        }
    )
    rows = {"base": parent}
    selected = "base"
    if mode == "select-again":
        rows["clear"] = SessionTemplate.model_validate(
            {
                "name": "clear",
                "inherits": ["base"],
                "harness_integration": {"name": integration, "enabled_workarounds": None},
            }
        )
        selected = "clear"
    config: dict[str, object] = {"name": integration, "extra_args": ["--child", "same", "same"]}
    if mode != "omitted":
        config["enabled_workarounds"] = None if mode == "clear" else [] if mode == "empty" else names
    authored = {"harness_integration": config}
    if instance:
        overlay = parse_instance_spec("session", json.dumps(authored))
        assert overlay.payload.value == authored
        result = resolve_session(rows, selected, overlay=overlay.declaration, instance_name="running")
    else:
        rows["child"] = SessionTemplate.model_validate({"name": "child", "inherits": [selected], **authored})
        result = resolve_session(rows, "child")
    effective = result.value.harness_integration_config
    expected = None if mode == "clear" else names if mode in ("append", "select-again") else names[:1]
    assert effective["enabled_workarounds"] == expected
    assert effective["extra_args"] == ["--child", "same", "same"]
    if mode == "select-again":
        path = ("harness_integration_config", "enabled_workarounds", 0)
        from agentworks.value_provenance import longest_prefix_value

        layers = longest_prefix_value(result.provenance, path)
        assert layers is not None and [layer.name for layer in layers] == ["running" if instance else "child"]


@pytest.mark.parametrize("integration", ["claude-code", "codex"])
@pytest.mark.parametrize(
    "model,resolve,admin,instance",
    [
        (AgentTemplate, resolve_agent, False, False),
        (AgentTemplate, resolve_agent, False, True),
        (AdminConfig, resolve_admin, True, True),
    ],
)
@pytest.mark.parametrize("field", ["marketplaces", "plugins"])
@pytest.mark.parametrize(
    "mode,incoming,expected",
    [
        ("omitted", None, ["parent", "shared"]),
        ("empty", [], ["parent", "shared"]),
        ("append", ["shared", "child", "child"], ["parent", "shared", "child"]),
        ("clear", None, None),
        ("select-again", ["shared", "child"], ["shared", "child"]),
    ],
)
def test_native_user_selection_inheritance_and_null_reset(
    integration, model, resolve, admin, instance, field, mode, incoming, expected
):
    other = "plugins" if field == "marketplaces" else "marketplaces"
    parent = model.model_validate(
        {"name": "base", "harness_integrations": {integration: {field: ["parent", "shared"], other: ["kept"]}}}
    )
    rows = {"base": parent}
    selected = "base"
    if mode == "select-again":
        clear = model.model_validate({"name": "clear", "harness_integrations": {integration: {field: None}}})
        if admin:
            rows["base"] = resolve(rows, "base", overlay=clear, instance_name="cleared").value
        else:
            rows["clear"] = clear.model_copy(update={"inherits": ["base"]})
            selected = "clear"
    selection = {} if mode == "omitted" else {field: incoming}
    authored = {"harness_integrations": {integration: selection}}
    if admin:
        parsed_vm = parse_vm_instance_specs(None, json.dumps(authored))
        assert parsed_vm is not None and parsed_vm.payload.value["admin"] == authored
        overlay = parsed_vm.admin
    elif instance:
        parsed = parse_instance_spec("agent", json.dumps(authored))
        assert parsed.payload.value == authored
        overlay_agent = parsed.declaration
    else:
        rows["child"] = model.model_validate({"name": "child", "inherits": [selected], **authored})
    if instance:
        result = resolve(rows, selected, overlay=overlay if admin else overlay_agent, instance_name="running")
    else:
        result = resolve(rows, "child")
    config = result.value.harness_integrations[integration].config
    assert config[field] == expected and config[other] == ["kept"]
    assert NativeUserConfig.model_validate(config).model_dump()[field] == expected


@pytest.mark.parametrize("field", ["marketplaces", "plugins"])
@pytest.mark.parametrize("values", [["same", "same"], [" "], ["nul\x00entry"]])
def test_native_config_rejects_ambiguous_entries_before_setup(field, values):
    with pytest.raises(ValidationError):
        NativeUserConfig.model_validate({field: values})


@pytest.mark.parametrize("integration,names", SESSION_SELECTIONS)
def test_session_generated_schema_and_instance_parser_accept_null_selections(integration, names):
    spec = {"harness_integration": {"name": integration, "enabled_workarounds": None}}
    document = {
        "apiVersion": "agentworks/v1",
        "kind": "session-template",
        "metadata": {"name": "fixture"},
        "spec": spec,
    }
    Draft202012Validator(document_schema("session-template")).validate(document)
    assert decode("session-template", "fixture", spec).harness_integration.config["enabled_workarounds"] is None
    assert parse_instance_spec("session", json.dumps(spec)).payload.value == spec


@pytest.mark.parametrize("integration", ["claude-code", "codex"])
@pytest.mark.parametrize("kind", ["agent-template", "admin-template"])
def test_native_generated_schema_and_instance_parser_accept_null_selections(integration, kind):
    spec = {"harness_integrations": {integration: {"marketplaces": None, "plugins": None}}}
    document = {"apiVersion": "agentworks/v1", "kind": kind, "metadata": {"name": "fixture"}, "spec": spec}
    Draft202012Validator(document_schema(kind)).validate(document)
    assert (
        decode(kind, "fixture", spec).harness_integrations[integration].config
        == spec["harness_integrations"][integration]
    )
    if kind == "admin-template":
        parsed = parse_vm_instance_specs(None, json.dumps(spec))
        assert parsed is not None and parsed.payload.value["admin"] == spec
    else:
        assert parse_instance_spec("agent", json.dumps(spec)).payload.value == spec


@pytest.mark.parametrize(
    "kind,spec",
    [
        *(
            ("session", {"harness_integration": {"name": name, "enabled_workarounds": None}})
            for name, _names in SESSION_SELECTIONS
        ),
        *(
            (kind, {"harness_integrations": {name: {"marketplaces": None, "plugins": None}}})
            for kind in ("agent", "admin")
            for name in ("claude-code", "codex")
        ),
    ],
)
def test_stored_native_and_session_overlays_retain_explicit_nulls(db, kind, spec):
    if kind == "admin":
        overlays = parse_vm_instance_specs(None, json.dumps(spec))
        assert overlays is not None
        payload = overlays.payload
    else:
        payload = parse_instance_spec(kind, json.dumps(spec)).payload
    owner = "vm" if kind == "admin" else kind
    db.instance_state.put_desired_overlay(owner, "fixture", payload)
    record = db.instance_state.get_desired_overlay(owner, "fixture")
    assert record is not None
    assert decode_stored_overlay(record).payload == payload
