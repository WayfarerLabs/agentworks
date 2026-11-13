"""Native npm settings at declaration and effective-template boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict
from agentworks.instance_specs import parse_instance_spec, parse_vm_instance_specs
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin


def test_npm_defaults_and_omitted_agent_values() -> None:
    admin = AdminConfig()
    declared = AgentTemplate(name="base")
    agent = resolve_from_dict({"base": declared}, "base")
    assert (declared.npm_enabled, declared.npm_packages, declared.npm_min_release_age_days) == (None, None, None)
    for template in (admin, agent):
        assert template.npm_enabled is True
        assert template.npm_packages == []
        assert template.npm_min_release_age_days == 7


def test_npm_settings_inherit_and_instance_overlay_can_disable() -> None:
    templates = {
        "base": AgentTemplate(
            name="base", npm_packages=["@scope/tool@^1.2.0"], npm_min_release_age_days=14
        ),
        "child": AgentTemplate(name="child", inherits=["base"]),
    }
    agent = resolve_from_dict(templates, "child")
    assert agent.npm_packages == ["@scope/tool@^1.2.0"]
    assert agent.npm_min_release_age_days == 14
    overlay = parse_instance_spec("agent", '{"npm_enabled":false,"npm_min_release_age_days":0}')
    assert overlay is not None
    agent = resolve_from_dict(templates, "child", overlay=overlay.declaration)
    assert agent.npm_enabled is False
    assert agent.npm_min_release_age_days == 0
    assert agent.npm_packages == ["@scope/tool@^1.2.0"]


def test_admin_npm_instance_overlay_retains_zero_age() -> None:
    overlays = parse_vm_instance_specs(None, '{"npm_packages":["tool"],"npm_min_release_age_days":0}')
    assert overlays is not None and overlays.admin is not None
    admin = resolve_admin({}, overlay=overlays.admin).value
    assert admin.npm_packages == ["tool"]
    assert admin.npm_min_release_age_days == 0


@pytest.mark.parametrize("model", [AdminConfig, AgentTemplate])
@pytest.mark.parametrize("age", [-1, "7d", True, 1.5])
def test_invalid_npm_age_is_rejected(model, age) -> None:
    with pytest.raises(ValidationError):
        model(name="invalid", npm_min_release_age_days=age)


@pytest.mark.parametrize("model", [AdminConfig, AgentTemplate])
@pytest.mark.parametrize("enabled", ["false", "yes", 0])
def test_npm_toggle_is_strict(model, enabled) -> None:
    with pytest.raises(ValidationError):
        model(name="invalid", npm_enabled=enabled)


@pytest.mark.parametrize("kind", ["agent-template", "admin-template"])
def test_emitted_schema_exposes_npm_settings(kind: str) -> None:
    from agentworks.manifests.emit import document_schema

    schema = document_schema(kind)
    properties = schema["properties"]["spec"]["properties"]
    assert {"npm_enabled", "npm_packages", "npm_min_release_age_days"} <= properties.keys()
