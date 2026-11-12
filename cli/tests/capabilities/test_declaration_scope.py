"""Instance-only config values are checked at their declaring boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import Field

from agentworks.capabilities.config import validate_capability_config, validate_own_config
from agentworks.errors import ConfigError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel, InstanceOnly, RefOwner
from agentworks.schema.declaration_scope import validate_declaration_scope
from agentworks.source_location import SourceLocation
from tests.plugins._fixtures import ConformingVMPlatform


class Child(AgwModel):
    adopted: Annotated[str | None, InstanceOnly()] = None


class Ordinary(AgwModel):
    kind: Literal["ordinary"] = "ordinary"
    value: str


class Scoped(Child):
    kind: Literal["scoped"] = "scoped"


class ScopeConfig(Child):
    name: Literal["scope-fixture"] = "scope-fixture"
    child: Child | None = None
    children: list[Child] = Field(default_factory=list)
    named: dict[str, Child] = Field(default_factory=dict)
    choice: Annotated[Ordinary | Scoped, Field(discriminator="kind")] | None = None
    default_child: Child = Child(adopted="default")


class ScopePlatform(ConformingVMPlatform):
    name: ClassVar[str] = "scope-fixture"
    description: ClassVar[str] = "Fixture for declaration scope"
    config_model: ClassVar[type[AgwModel]] = ScopeConfig


@pytest.mark.parametrize("kind", ["session-template", "vm-template", "agent-template"])
@pytest.mark.parametrize("value", ["conversation", None])
def test_templates_reject_authored_instance_only_fields(kind: str, value: object) -> None:
    with seated_plugin(Plugin(name="scope-fixture", capabilities={"vm-platform": (ScopePlatform,)})):
        with pytest.raises(ConfigError) as caught:
            validate_capability_config(
                kind="vm-platform",
                config={"name": "scope-fixture", "adopted": value},
                owner=RefOwner(kind=kind, name="parent"),
            )
        assert caught.value.entity_kind == kind
        assert caught.value.entity_name == "parent"


def test_omitted_fields_and_authored_instance_fields_are_accepted() -> None:
    with seated_plugin(Plugin(name="scope-fixture", capabilities={"vm-platform": (ScopePlatform,)})):
        assert isinstance(
            validate_capability_config(
                kind="vm-platform",
                config={"name": "scope-fixture"},
                owner=RefOwner(kind="vm-template", name="parent"),
            ),
            ScopeConfig,
        )
        accepted = validate_capability_config(
            kind="vm-platform",
            config={"name": "scope-fixture", "adopted": "conversation"},
            owner=RefOwner(kind="vm", name="instance"),
        )
        assert isinstance(accepted, ScopeConfig)
        assert accepted.adopted == "conversation"


def test_construction_checks_shape_without_rejecting_prevalidated_instance_config() -> None:
    result = validate_own_config(
        ScopePlatform,
        {"name": "scope-fixture", "adopted": "conversation"},
        owner=RefOwner(kind="vm-template", name="runtime-config-owner"),
    )
    assert isinstance(result, ScopeConfig)
    assert result.adopted == "conversation"


@pytest.mark.parametrize(
    ("authored", "path"),
    [
        ({"child": {"adopted": "conversation"}}, ("child", "adopted")),
        ({"children": [{"adopted": "conversation"}]}, ("children", 0, "adopted")),
        ({"named": {"first": {"adopted": "conversation"}}}, ("named", "first", "adopted")),
        ({"choice": {"kind": "scoped", "adopted": "conversation"}}, ("choice", "adopted")),
    ],
)
def test_nested_declarations_use_longest_prefix_provenance(
    authored: dict[str, object], path: tuple[str | int, ...]
) -> None:
    owner = RefOwner(kind="session", name="current")
    template = RefOwner(kind="session-template", name="parent")
    config = ScopeConfig.model_validate(authored)
    with pytest.raises(ConfigError) as caught:
        validate_declaration_scope(config, authored, owner=owner, provenance={path: template})
    assert caught.value.entity_kind == "session"
    assert caught.value.entity_name == "current"
    validate_declaration_scope(config, authored, owner=owner, provenance={(): template, path: owner})


def test_omitted_nested_defaults_do_not_become_declarations() -> None:
    raw = {"child": {}, "children": [{}], "named": {"first": {}}, "choice": {"kind": "ordinary", "value": "ok"}}
    validate_declaration_scope(
        ScopeConfig.model_validate(raw), raw, owner=RefOwner(kind="session-template", name="parent")
    )


def test_scope_error_preserves_source_location_and_declaring_owner() -> None:
    raw = {"adopted": None}
    owner = RefOwner(kind="session", name="current")
    template = RefOwner(kind="session-template", name="parent")
    with pytest.raises(ConfigError) as caught:
        validate_declaration_scope(
            Child.model_validate(raw),
            raw,
            owner=owner,
            provenance={(): template},
            location=SourceLocation(file=Path("scope.yaml"), line=13),
        )
    assert caught.value.entity_kind == owner.kind
    assert caught.value.entity_name == owner.name
    assert "scope.yaml:13:" in str(caught.value)
    assert template.display in str(caught.value)


def test_instance_only_marker_survives_into_json_schema() -> None:
    properties = Child.model_json_schema()["properties"]
    assert properties["adopted"]["x-agw-instance-only"] is True


@pytest.mark.parametrize("authored", [Child(adopted=None), {"child": Child(adopted="conversation")}])
def test_authored_model_instances_cannot_bypass_scope(authored: object) -> None:
    model = type(authored) if isinstance(authored, Child) else ScopeConfig
    validated = model.model_validate(authored)
    with pytest.raises(ConfigError):
        validate_declaration_scope(validated, authored, owner=RefOwner(kind="session-template", name="parent"))


def test_unset_model_instance_defaults_are_not_declarations() -> None:
    authored = Child()
    validate_declaration_scope(authored, authored, owner=RefOwner(kind="session-template", name="parent"))
