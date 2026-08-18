"""Structural contracts for the focused resource-show service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from pydantic import Field

from agentworks.bootstrap import build_registry
from agentworks.config import Config, load_config
from agentworks.db import Database
from agentworks.declared_resource import FRAMEWORK_FIELDS, METADATA_FIELDS, DeclaredResource
from agentworks.machine_output import JsonObject
from agentworks.origin import Origin
from agentworks.resources import KIND_REGISTRY, DatabaseLiveSource, Registry
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import DisabledMark, Readiness
from agentworks.resources.inspect import list_resources, resource_listing_data
from agentworks.resources.show import project_declaration, resource_show_data, show_resource
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse
from agentworks.vms.template import VMTemplate
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.resources.graph import DependencyState, EnablementSource
    from agentworks.resources.reference import ResourceReference


class _Color(StrEnum):
    BLUE = "blue"


class _Nested(AgwModel):
    observed_at: datetime
    labels: dict[str, list[int]]


class _ProjectionRow(DeclaredResource):
    inherits: list[str] = Field(default_factory=list)
    color: _Color = _Color.BLUE
    nested: _Nested
    tags: list[str] = Field(default_factory=list)
    optional: str | None = None


class _ReadinessRow(DeclaredResource):
    verdict: Literal["blocked", "unavailable"]

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness:
        del deps
        if self.verdict == "blocked":
            return Readiness.blocked("host check failed")
        return Readiness.unavailable("host check omitted")


@dataclass(frozen=True)
class _ReadinessKind:
    kind: str = "show-test"
    model: type[DeclaredResource] = _ReadinessRow
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    description: str = "Test kind"
    prose: TopicProse = TopicProse(title="Test", overview="Test")
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        del references
        raise AssertionError("error-policy kinds do not synthesize")


def _request_context(tmp_path: Path) -> tuple[Config, Registry]:
    config_path = write_cfg(
        tmp_path,
        ManifestDoc(
            "secret",
            "npm-token",
            {"hint": "rotate quarterly", "backend_mappings": {"env-var": "NPM_TOKEN"}},
            description="npm registry token",
        ),
    )
    config = load_config(config_path, warn_issues=False)
    return config, build_registry(config, probe_host_readiness=False)


def _show(config: Config, registry: Registry, identity: ResourceIdentity, tmp_path: Path):
    return show_resource(config, registry, identity, DatabaseLiveSource(tmp_path / "absent.db"))


def _readiness_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: Literal["blocked", "unavailable"],
    disabled: bool = False,
) -> Registry:
    kind = _ReadinessKind()
    monkeypatch.setitem(KIND_REGISTRY, kind.kind, kind)
    registry = Registry.empty()
    registry.add(
        kind.kind,
        verdict,
        _ReadinessRow(name=verdict, verdict=verdict),
        Origin.built_in(source="tests.resources.test_show"),
    )
    sources: Sequence[EnablementSource] = ()
    if disabled:
        sources = (lambda _resources: {(kind.kind, verdict): DisabledMark(reason="disabled for test", source="test")},)
    registry.finalize(enablement_sources=sources)
    return registry


def test_declaration_projection_uses_shared_fields_and_pydantic_json_mode() -> None:
    moment = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    row = _ProjectionRow(
        name="child",
        description="projection fixture",
        expires=moment,
        inherits=["base"],
        nested=_Nested(observed_at=moment, labels={"counts": [1, 2]}),
        origin=Origin.operator_declared(file=Path("resources.yaml"), line=7),
    )

    declaration = project_declaration("projection-test", row)

    assert list(declaration) == ["apiVersion", "kind", "metadata", "spec"]
    metadata = declaration["metadata"]
    spec = declaration["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    assert list(metadata) == ["name", "description", "expires"]
    assert list(spec) == ["inherits", "color", "nested", "tags"]
    assert metadata["expires"] == "2026-08-17T12:30:00Z"
    assert spec["color"] == "blue"
    assert spec["tags"] == []
    assert spec["nested"] == {
        "observed_at": "2026-08-17T12:30:00Z",
        "labels": {"counts": [1, 2]},
    }
    assert "optional" not in spec
    assert not (FRAMEWORK_FIELDS & set(metadata))
    assert not (FRAMEWORK_FIELDS & set(spec))
    json.dumps(declaration, allow_nan=False)


def test_framework_field_split_is_complete() -> None:
    assert not (METADATA_FIELDS & FRAMEWORK_FIELDS)
    assert set(DeclaredResource.model_fields) == METADATA_FIELDS | FRAMEWORK_FIELDS


def test_normalized_manifest_reconstructs_the_loaded_row() -> None:
    row = VMTemplate(
        name="dev",
        description="development VM",
        cpus=4,
        apt=["zsh"],
        origin=Origin.operator_declared(file=Path("vm-templates.yaml"), line=3),
    )
    declaration = project_declaration("vm-template", row)
    metadata = declaration["metadata"]
    spec = declaration["spec"]
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)

    reconstructed = VMTemplate.model_validate(
        {
            **metadata,
            **spec,
            "origin": row.origin,
            "declared_at": row.declared_at,
        }
    )

    assert reconstructed.model_dump(exclude=FRAMEWORK_FIELDS) == row.model_dump(exclude=FRAMEWORK_FIELDS)


def test_service_projects_declarable_and_capability_rows(tmp_path: Path) -> None:
    config, registry = _request_context(tmp_path)

    declarable = _show(config, registry, ResourceIdentity("secret", "npm-token"), tmp_path)
    capability = _show(config, registry, ResourceIdentity("vm-platform", "lima"), tmp_path)

    assert declarable.category == "declarable"
    assert declarable.summary.description == "npm registry token"
    assert declarable.declaration is not None
    assert declarable.declaration["spec"] == {
        "hint": "rotate quarterly",
        "backend_mappings": {"env-var": "NPM_TOKEN"},
    }
    assert declarable.readiness is not None
    assert declarable.readiness.is_ready
    assert capability.category == "capability"
    assert capability.declaration is None
    assert capability.readiness is not None
    assert not capability.readiness.is_available


def test_show_summary_is_the_exact_matching_list_row(tmp_path: Path) -> None:
    config, registry = _request_context(tmp_path)
    database_path = tmp_path / "state.db"
    database = Database(database_path)
    listing = list_resources(registry, database, include_disabled=True)
    database.close()
    identity = ResourceIdentity("secret", "npm-token")

    shown = show_resource(config, registry, identity, DatabaseLiveSource(database_path))
    list_row = next(row for row in listing.rows if (row.kind, row.name) == (identity.kind, identity.name))
    list_data = resource_listing_data(listing)["resources"]
    assert isinstance(list_data, list)
    list_rows = cast("list[JsonObject]", list_data)
    list_row_data = next(row for row in list_rows if row["kind"] == identity.kind and row["name"] == identity.name)
    show_data = resource_show_data(shown)["resource"]
    assert isinstance(show_data, dict)

    assert shown.summary == list_row
    assert list(show_data.items())[:8] == list(list_row_data.items())
    assert shown.summary.reference_count == len(shown.relationships.dependents)
    assert shown.used_by == ()
    assert shown.summary.used_by_count == len(shown.used_by)


def test_service_does_not_resolve_secrets_or_create_a_vm_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.secrets import orchestration, resolve

    config, registry = _request_context(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("resource show invoked an instrumented operation")

    monkeypatch.setattr(resolve, "resolve_batch", forbidden)
    monkeypatch.setattr(orchestration, "resolve_for_command", forbidden)
    monkeypatch.setattr(LimaPlatform, "create", forbidden)

    assert _show(config, registry, ResourceIdentity("secret", "npm-token"), tmp_path).declaration is not None
    assert _show(config, registry, ResourceIdentity("vm-platform", "lima"), tmp_path).declaration is None


@pytest.mark.parametrize(
    ("verdict", "is_ready", "is_available"),
    [("blocked", False, True), ("unavailable", False, False)],
)
def test_enabled_rows_retain_stored_readiness_facts(
    monkeypatch: pytest.MonkeyPatch,
    verdict: Literal["blocked", "unavailable"],
    is_ready: bool,
    is_available: bool,
) -> None:
    registry = _readiness_registry(monkeypatch, verdict=verdict)

    shown = show_resource(
        cast("Config", object()),
        registry,
        ResourceIdentity("show-test", verdict),
        DatabaseLiveSource(Path("/missing-show-test.db")),
    )

    assert shown.readiness is not None
    assert shown.readiness is registry.graph.readiness_of("show-test", verdict)
    assert shown.readiness.is_ready is is_ready
    assert shown.readiness.is_available is is_available
    assert shown.readiness.reason is not None
    assert shown.summary.disabled is False
    assert shown.summary.not_ready_reason == shown.readiness.reason
    assert shown.diagnostics == ()


def test_disabled_row_projects_null_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _readiness_registry(monkeypatch, verdict="blocked", disabled=True)

    shown = show_resource(
        cast("Config", object()),
        registry,
        ResourceIdentity("show-test", "blocked"),
        DatabaseLiveSource(Path("/missing-show-test.db")),
    )
    data = resource_show_data(shown)["resource"]

    assert shown.enablement.value == "disabled"
    assert shown.readiness is None
    assert shown.summary.disabled is True
    assert shown.summary.not_ready_reason is None
    assert isinstance(data, dict)
    assert data["readiness"] is None


def test_every_registered_declarable_row_projects_to_closed_json(tmp_path: Path) -> None:
    config, registry = _request_context(tmp_path)
    projected_kinds: set[str] = set()
    kinds_with_rows: set[str] = set()

    for kind, handler in KIND_REGISTRY.items():
        if handler.category != "declarable":
            continue
        rows = tuple(registry.iter_kind_items(kind))
        if not rows:
            continue
        kinds_with_rows.add(kind)
        projected_kinds.add(kind)
        for name, _row in rows:
            shown = _show(config, registry, ResourceIdentity(kind, name), tmp_path)
            assert shown.declaration is not None
            json.dumps(resource_show_data(shown), allow_nan=False)

    assert projected_kinds == kinds_with_rows


def test_secret_declaration_contains_lookup_configuration_not_values(tmp_path: Path) -> None:
    config, registry = _request_context(tmp_path)

    shown = _show(config, registry, ResourceIdentity("secret", "npm-token"), tmp_path)

    assert shown.declaration is not None
    spec = shown.declaration["spec"]
    assert isinstance(spec, dict)
    assert set(spec) == {"hint", "backend_mappings"}
    assert spec["backend_mappings"] == {"env-var": "NPM_TOKEN"}


def test_category_row_mismatch_fails_instead_of_projecting_partial_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _readiness_registry(monkeypatch, verdict="blocked")
    original = KIND_REGISTRY["show-test"]
    monkeypatch.setitem(
        KIND_REGISTRY,
        "show-test",
        _ReadinessKind(category="capability"),
    )

    with pytest.raises(AssertionError):
        show_resource(
            cast("Config", object()),
            registry,
            ResourceIdentity("show-test", "blocked"),
            DatabaseLiveSource(Path("/missing-show-test.db")),
        )

    assert original.category == "declarable"
