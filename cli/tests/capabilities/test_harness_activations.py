"""Explicit setup selection survives inheritance, overlays, and config binding."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar

import pytest
from pydantic import BaseModel

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.capabilities.descriptor import Facet
from agentworks.capabilities.harness_integration.activations import validate_activations
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import ConfigError, StateError
from agentworks.instance_specs import parse_instance_spec, parse_vm_instance_specs
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import FinalizeContext
from agentworks.resources.live_publish import project_agent_live_resource
from agentworks.resources.resolved_spec import project_resolved_spec
from agentworks.schema import AgwModel, CapabilityConfig, SecretRef
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import resolve_from_dict_with_provenance as resolve_workspace
from tests.plugins._fixtures import ConformingHarnessIntegration

if TYPE_CHECKING:
    from collections.abc import Iterator


class SetupConfig(AgwModel):
    token: Annotated[str, SecretRef(usage="fixture setup token")]


class SetupHarness(ConformingHarnessIntegration):
    name = "setup-fixture"
    description = "A fixture requiring user setup config"
    selections: ClassVar[int] = 0

    @classmethod
    def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
        cls.selections += 1
        return SetupConfig if facet == "user" else super().config_for(facet)


@pytest.fixture
def seated() -> Iterator[None]:
    with seated_plugin(Plugin(name="setup-fixture", capabilities={"harness-integration": (SetupHarness,)})):
        yield


@pytest.mark.parametrize(
    ("model", "resolve", "kind", "facet"),
    [
        (VMTemplate, resolve_vm, "vm-template", "vm"),
        (AgentTemplate, resolve_agent, "agent-template", "user"),
        (WorkspaceTemplate, resolve_workspace, "workspace-template", "workspace"),
    ],
)
def test_setup_maps_inherit_merge_and_project(model, resolve, kind, facet) -> None:
    parent = model(name="base", harness_integrations={"shell": CapabilityConfig(), "codex": CapabilityConfig()})
    child = model(name="child", inherits=["base"])
    layered = resolve({"base": parent, "child": child}, "child")
    assert list(layered.value.harness_integrations) == ["shell", "codex"]
    projection = project_resolved_spec(layered, ResourceIdentity(kind, "child"))
    assert projection.spec["harness_integrations"] == {"shell": {}, "codex": {}}
    paths = {entry.path: entry.sources for entry in projection.provenance}
    assert paths[("harness_integrations", "codex")][-1].resource_name == "base"
    ctx = FinalizeContext(rows={kind: {"base": parent, "child": child}})
    refs = [ref for ref in child.dependencies(ctx) if ref.kind == "harness-integration"]
    assert [ref.name for ref in refs] == ["shell", "codex"]
    assert all(ref.source == (kind, "child") and ref.declared_by == (kind, "base") for ref in refs)
    for blocks in ({"shell": CapabilityConfig()}, {}):
        overlay = model(name="overlay", harness_integrations=blocks)
        result = resolve({"base": parent}, "base", overlay=overlay, instance_name="instance")
        assert result.value.harness_integrations == parent.harness_integrations
    assert resolve({}, None).value.harness_integrations == {}


def test_admin_empty_overlay_inherits_its_explicit_user_activations() -> None:
    template = AdminConfig(name="admin", harness_integrations={"shell": CapabilityConfig()})
    parsed = parse_vm_instance_specs(None, '{"harness_integrations": {}}')
    assert parsed is not None and parsed.admin is not None
    layered = resolve_admin({"admin": template}, "admin", overlay=parsed.admin, instance_name="vm")
    assert layered.value.harness_integrations == template.harness_integrations
    assert template.harness_integrations == {"shell": CapabilityConfig()}


@pytest.mark.parametrize("kind", ["vm", "agent", "workspace"])
def test_instance_overlay_keeps_name_only_enablement_and_explicit_empty(kind) -> None:
    parsed = parse_instance_spec(kind, '{"harness_integrations": {"shell": {}}}')
    assert parsed.payload.value == {"harness_integrations": {"shell": {}}}
    cleared = parse_instance_spec(kind, '{"harness_integrations": {}}')
    assert cleared.payload.value == {"harness_integrations": {}}


def test_effective_validation_rejects_wrong_facet_fields_and_inner_selectors() -> None:
    for config in ({"command": "x"}, {"name": "codex"}):
        with pytest.raises(ConfigError):
            validate_activations(
                {"shell": CapabilityConfig.model_validate(config)},
                facet="vm",
                source=("vm-template", "vm"),
                provenance={},
            )


def test_user_config_secrets_retain_declaration_ownership_and_overlay_validation(seated: None) -> None:
    base = AgentTemplate(
        name="base", harness_integrations={"setup-fixture": CapabilityConfig.model_validate({"token": "base-token"})}
    )
    child = AgentTemplate(name="child", inherits=["base"])
    ctx = FinalizeContext(rows={"agent-template": {"base": base, "child": child}})
    child.validate_config(ctx)
    refs = [ref for ref in child.dependencies(ctx) if ref.kind == "secret"]
    assert [(ref.name, ref.source, ref.declared_by) for ref in refs] == [
        ("base-token", ("agent-template", "child"), ("agent-template", "base")),
    ]
    invalid = AgentTemplate(
        name="overlay", harness_integrations={"setup-fixture": CapabilityConfig.model_validate({"token": 42})}
    )
    layered = resolve_agent({"base": base}, "base", overlay=invalid, instance_name="a")
    with pytest.raises(ConfigError) as caught:
        project_agent_live_resource(name="a", vm_name=None, template_name="base", layered=layered)
    assert "harness_integrations.setup-fixture" in str(caught.value)
    assert "agent/a" in str(caught.value)


def test_setup_binding_uses_actual_owner_and_requires_no_session(seated: None) -> None:
    bound = SetupHarness.for_setup(
        owner_kind="agent-template", owner_name="owner", facet="user", config={"token": "secret"}
    )
    assert isinstance(bound.config, SetupConfig)
    assert not bound.retiring
    refs = bound.config_secret_refs()
    assert [(ref.name, ref.source) for ref in refs] == [("secret", ("agent-template", "owner"))]
    with pytest.raises(StateError):
        _ = bound.state
    with pytest.raises(StateError):
        _ = bound._session_name


def test_retirement_never_selects_or_validates_removed_config(seated: None) -> None:
    previous = SetupHarness.selections
    retired = SetupHarness.for_setup(owner_kind="admin-template", owner_name="owner", facet="user", config=None)
    assert retired.retiring
    assert SetupHarness.selections == previous
    assert retired.config_secret_refs() == ()
    with pytest.raises(StateError):
        _ = retired.config
    with pytest.raises(StateError):
        retired._config_as(SetupConfig)


def test_active_empty_setup_is_distinct_from_retirement() -> None:
    active = ShellIntegration.for_setup(owner_kind="vm-template", owner_name="owner", facet="vm", config={})
    assert not active.retiring
    assert active._require_config().model_dump() == {}
    assert active.config_secret_refs() == ()


@pytest.mark.parametrize("enabled", [False, True])
def test_setup_activation_participates_in_capability_enablement(enabled: bool) -> None:
    from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled
    from agentworks.capabilities.harness_integration.kinds import HarnessIntegrationEntry
    from agentworks.origin import Origin
    from agentworks.resources.graph import DisabledMark
    from agentworks.resources.registry import Registry

    registry = Registry.empty()
    origin = Origin.built_in(source="fixture")
    registry.add("harness-integration", "shell", HarnessIntegrationEntry("shell", origin), origin)
    template = AgentTemplate(name="agent", harness_integrations={"shell": CapabilityConfig()})
    registry.add("agent-template", "agent", template, origin)
    marks = {} if enabled else {("harness-integration", "shell"): DisabledMark(reason="fixture", source="fixture")}
    registry.finalize(enablement_sources=[lambda resources: marks])
    assert registry.graph.is_ready("agent-template", "agent")
    if enabled:
        ensure_harness_integration_enabled(registry, "shell")
    else:
        with pytest.raises(StateError):
            ensure_harness_integration_enabled(registry, "shell")


@pytest.mark.parametrize(
    ("model", "resolve", "kind"),
    [
        (VMTemplate, resolve_vm, "vm-template"),
        (AgentTemplate, resolve_agent, "agent-template"),
        (WorkspaceTemplate, resolve_workspace, "workspace-template"),
    ],
)
def test_map_entries_merge_through_facet_models_and_keep_field_owners(model, resolve, kind) -> None:
    from pydantic import Field

    from agentworks.capabilities.harness_integration.activations import activation_references
    from agentworks.schema import MergeStrategy

    class Nested(AgwModel):
        tokens: dict[str, Annotated[str, SecretRef(usage="nested token")]] = Field(default_factory=dict)
        commands: list[str] = Field(default_factory=list)
        replace_items: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)

    class MergeConfig(AgwModel):
        settings: Nested = Field(default_factory=Nested)

    class MergeHarness(ConformingHarnessIntegration):
        name = "merge-fixture"
        description = "Config merging fixture"

        @classmethod
        def config_for(cls, facet: Facet | None = None) -> type[BaseModel]:
            return MergeConfig

    parent = model.model_validate(
        {
            "name": "base",
            "harness_integrations": {
                "merge-fixture": {
                    "settings": {
                        "tokens": {"left": "base-token", "shared": "old-token"},
                        "commands": ["base"],
                        "replace_items": ["old"],
                    }
                },
                "shell": {},
            },
        }
    )
    child = model.model_validate(
        {
            "name": "child",
            "inherits": ["base"],
            "harness_integrations": {
                "merge-fixture": {
                    "settings": {
                        "tokens": {"shared": "child-token"},
                        "commands": ["base", "child"],
                        "replace_items": ["new"],
                    }
                }
            },
        }
    )
    facet = {"vm-template": "vm", "agent-template": "user", "workspace-template": "workspace"}[kind]
    with seated_plugin(Plugin(name="merge-fixture", capabilities={"harness-integration": (MergeHarness,)})):
        layered = resolve({"base": parent, "child": child}, "child")
        assert list(layered.value.harness_integrations) == ["merge-fixture", "shell"]
        assert layered.value.harness_integrations["merge-fixture"].config == {
            "settings": {
                "tokens": {"left": "base-token", "shared": "child-token"},
                "commands": ["base", "child"],
                "replace_items": ["new"],
            }
        }
        refs = activation_references(
            layered.value.harness_integrations, facet=facet, source=(kind, "child"), provenance=layered.provenance
        )
        assert {ref.name: ref.declarer for ref in refs if ref.kind == "secret"} == {
            "base-token": (kind, "base"),
            "child-token": (kind, "child"),
        }
        # A sibling config edit must not transfer ownership of an inherited error.
        invalid_parent = parent.model_copy(
            update={
                "harness_integrations": {
                    "merge-fixture": CapabilityConfig.model_validate({"settings": {"tokens": {"left": 42}}})
                }
            }
        )
        invalid = resolve({"base": invalid_parent, "child": child}, "child")
        with pytest.raises(ConfigError) as error:
            validate_activations(
                invalid.value.harness_integrations, facet=facet, source=(kind, "child"), provenance=invalid.provenance
            )
        assert f"{kind}/base.harness_integrations.merge-fixture" in str(error.value)
        assert "settings.tokens.left" in str(error.value)


@pytest.mark.parametrize("model", [VMTemplate, AgentTemplate, WorkspaceTemplate, AdminConfig])
@pytest.mark.parametrize("value", [[], [{"name": "shell"}], {"": {}}, {"shell": None}])
def test_authored_activations_require_nonempty_keys_and_config_tables(model, value) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        model.model_validate({"name": "test", "harness_integrations": value})
