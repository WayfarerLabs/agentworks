"""Facet selection stays coherent through registration and every config reader."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, cast

import pytest
from pydantic import BaseModel, Field, ValidationError

from agentworks.capabilities.config import (
    capability_config_model,
    capability_config_references,
    capability_config_union,
    offered_model,
    validate_capability_config,
)
from agentworks.capabilities.descriptor import FACETS, Facet, HostSurface, descriptor_for
from agentworks.errors import ConfigError, StateError
from agentworks.manifests.reference import implementation_reference, kind_reference
from agentworks.manifests.spec_model import spec_model
from agentworks.plugins import Plugin, seated_plugin
from agentworks.plugins.base import PluginError
from agentworks.schema import AgwModel, CapabilityBlock, RefOwner, SecretRef
from agentworks.sessions.template import SessionTemplate
from tests.plugins._fixtures import ConformingHarnessIntegration

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = RefOwner(kind="session-template", name="facet-test")


class SessionConfig(AgwModel):
    name: Literal["facet-test"]
    session_token: Annotated[str, SecretRef(usage="session token")] = "session-secret"
    args: list[str] = Field(default_factory=list)


class UserConfig(AgwModel):
    name: Literal["facet-test"]
    user_token: Annotated[str, SecretRef(usage="user token")] = "user-secret"


class FacetHarness(ConformingHarnessIntegration):
    name = "facet-test"
    description = "A harness with different config at two facets"
    config_model: ClassVar[type[BaseModel]] = SessionConfig

    @classmethod
    def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
        if facet == "session":
            return cls.config_model
        return UserConfig if facet == "user" else None


@pytest.fixture
def seated() -> Iterator[None]:
    with seated_plugin(Plugin(name="facet-fixture", capabilities={"harness-integration": (FacetHarness,)})):
        yield


def test_each_facet_validates_and_extracts_its_own_fields(seated: None) -> None:
    for facet, token in (("user", "user-secret"), ("session", "session-secret")):
        config = {"name": "facet-test"}
        value = validate_capability_config(kind="harness-integration", facet=facet, config=config, owner=OWNER)
        assert isinstance(value, UserConfig if facet == "user" else SessionConfig)
        refs = capability_config_references(kind="harness-integration", facet=facet, config=config, owner=OWNER)
        assert [ref.name for ref in refs] == [token]
    with pytest.raises(ConfigError):
        validate_capability_config(
            kind="harness-integration", facet="session", config={"name": "facet-test", "user_token": "x"}, owner=OWNER
        )


def test_no_config_is_closed_and_distinct_from_unknown_implementation(seated: None) -> None:
    assert offered_model(FacetHarness, facet="vm") is None
    assert capability_config_model("harness-integration", "missing", facet="vm") is None
    value = validate_capability_config(
        kind="harness-integration", facet="vm", config={"name": "facet-test"}, owner=OWNER
    )
    assert value is not None and value.model_dump() == {"name": "facet-test"}
    with pytest.raises(ConfigError):
        validate_capability_config(
            kind="harness-integration", facet="vm", config={"name": "facet-test", "args": []}, owner=OWNER
        )
    with pytest.raises(StateError):
        capability_config_model("harness-integration", "facet-test")


def test_registration_selection_is_shared_with_constructor_and_secret_extraction() -> None:
    class Stateful(FacetHarness):
        calls: ClassVar[Counter] = Counter()

        @classmethod
        def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
            cls.calls[facet] += 1
            if facet == "session":
                return SessionConfig if cls.calls[facet] == 1 else UserConfig
            return super().config_for(facet)

    with seated_plugin(Plugin(name="stateful-facets", capabilities={"harness-integration": (Stateful,)})):
        assert Stateful.calls == Counter(dict.fromkeys(FACETS, 1))
        instance = Stateful(
            "facet-test",
            {},
            session_name="test",
            vm_name="vm",
            workspace_name="ws",
            workspace_path="/ws",
            target=None,
            admin=True,
            state={},
        )
        assert isinstance(instance.config, SessionConfig)
        assert [ref.name for ref in instance.config_secret_refs()] == ["session-secret"]
        capability_config_union("harness-integration", facet="session")
        implementation_reference("harness-integration", "facet-test")
        assert Stateful.calls == Counter(dict.fromkeys(FACETS, 1))


def test_union_tracks_facet_declaration_replacement_and_registry_restoration(
    seated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = capability_config_union("harness-integration", facet="session")
    user = capability_config_union("harness-integration", facet="user")
    assert original is not user
    with monkeypatch.context() as changes:
        changes.setattr(FacetHarness, "config_model", UserConfig)
        changed = capability_config_union("harness-integration", facet="session")
        assert changed is not original
        assert capability_config_model("harness-integration", "facet-test", facet="session") is UserConfig
    assert capability_config_union("harness-integration", facet="session") is original

    class Replacement(FacetHarness):
        config_model = UserConfig

    registry = descriptor_for("harness-integration").registry()
    with monkeypatch.context() as changes:
        changes.setitem(registry, "facet-test", Replacement)
        assert capability_config_union("harness-integration", facet="session") is not original
    assert capability_config_union("harness-integration", facet="session") is original


@pytest.mark.parametrize("failure", ["invalid", "raising", "noncallable", "old-contract"])
def test_public_registration_refuses_bad_facet_contracts(failure: str) -> None:
    class Bad(FacetHarness):
        @classmethod
        def config_for(cls, facet: Facet | None = None) -> type[BaseModel] | None:
            if facet == "workspace":
                if failure == "raising":
                    raise RuntimeError("fixture")
                if failure == "invalid":
                    return cast("type[BaseModel]", object)
            return super().config_for(facet)

    if failure == "noncallable":
        Bad.config_for = None  # type: ignore[assignment]
    if failure == "old-contract":
        Bad.contract_version = 3
    with (
        pytest.raises(PluginError),
        seated_plugin(Plugin(name="bad-facets", capabilities={"harness-integration": (Bad,)})),
    ):
        pass


def test_reference_root_has_all_answers_and_resource_field_only_session(seated: None) -> None:
    fields = {entry.name: entry for entry in implementation_reference("harness-integration", "facet-test").spec}
    assert set(fields) == set(FACETS)
    assert {entry.name for entry in fields["vm"].children} == {"name"}
    assert {entry.name for entry in fields["user"].children} == {"name", "user_token"}
    session = next(entry for entry in kind_reference("session-template").spec if entry.name == "harness_integration")
    arm = next(arm for arm in session.alternatives if arm.name == "facet-test")
    assert arm.target == "harness-integration/facet-test"
    model = spec_model("session-template")
    model.model_validate({"name": "host", "harness_integration": {"name": "facet-test", "session_token": "x"}})
    with pytest.raises(ValidationError):
        model.model_validate({"name": "host", "harness_integration": {"name": "facet-test", "user_token": "x"}})


def test_multiple_hosts_project_list_and_singular_facets(seated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities import descriptor as descriptors
    from agentworks.manifests.decode import _hosted_capability_references
    from agentworks.manifests.envelope import Document
    from agentworks.resources.kind import KIND_REGISTRY
    from agentworks.source_location import synthesized

    class Host(SessionTemplate):
        user_integrations: list[CapabilityBlock] = Field(default_factory=list)

    harness = descriptor_for("harness-integration")
    hosts = (*harness.manifest_sections, HostSurface("session-template", "user_integrations", "user", "list"))
    updated = replace(harness, manifest_sections=hosts)
    table = tuple(updated if d is harness else d for d in descriptors.capability_descriptors())
    monkeypatch.setattr(descriptors, "capability_descriptors", lambda: table)
    monkeypatch.setitem(KIND_REGISTRY, "session-template", replace(KIND_REGISTRY["session-template"], model=Host))
    model = spec_model("session-template")
    value = model.model_validate(
        {
            "name": "host",
            "harness_integration": {"name": "facet-test", "session_token": "s"},
            "user_integrations": [{"name": "facet-test", "user_token": "u"}],
        }
    )
    assert value.user_integrations[0].root.user_token == "u"
    with pytest.raises(ValidationError):
        model.model_validate({"name": "host", "user_integrations": [{"name": "facet-test", "session_token": "wrong"}]})

    raw = Host(
        name="host",
        user_integrations=[
            CapabilityBlock(name="facet-test", user_token="first"),
            CapabilityBlock(name="facet-test", user_token="second"),
        ],
        harness_integration=CapabilityBlock(name="facet-test", session_token="session"),
    )
    doc = Document(
        kind="session-template",
        name="host",
        description=None,
        expires=None,
        spec={},
        location=synthesized(),
    )
    refs = _hosted_capability_references(raw, doc, OWNER)
    assert [ref.name for ref in refs] == ["session", "first", "second"]
    fields = {entry.name: entry for entry in kind_reference("session-template").spec}
    [element] = fields["user_integrations"].children
    assert any(arm.target == "harness-integration/facet-test" for arm in element.alternatives)
