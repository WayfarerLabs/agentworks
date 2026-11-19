"""Declaration, inheritance, and graph boundaries for scoped artifact bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import resolve_from_dict_with_provenance as resolve_agent
from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.declarations import ArtifactsConfig, artifact_references
from agentworks.resources.graph import FinalizeContext
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict_with_provenance as resolve_session
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import resolve_from_dict_with_provenance as resolve_vm
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import resolve_from_dict_with_provenance as resolve_workspace
from tests.manifests._specs import decode

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize("artifact_type", ["hint", "rule"])
@pytest.mark.parametrize("content", [{}, {"text": "x", "source": "x.md"}, {"text": " "}, {"source": ""}])
def test_context_artifacts_require_exactly_one_nonblank_input(artifact_type: str, content: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ArtifactBundle.model_validate({"name": "team", artifact_type + "s": {"one": content}})


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "skill", "text": "not a directory"},
        {"type": "agent"},
        {"type": "rule", "text": "context", "unknown": True},
        {"type": "mcp", "source": "server.json"},
        {"type": "skill", "source": "skills/review", "preserve_bytes": ["../outside"]},
        {"type": "skill", "source": "skills/review", "preserve_bytes": ["/absolute"]},
    ],
)
def test_invalid_artifact_declarations_are_rejected(entry: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArtifactBundle.model_validate(
            {
                "name": "team",
                str(entry["type"]) + "s": {"one": {key: value for key, value in entry.items() if key != "type"}},
            }
        )


def test_bundle_manifest_preserves_type_content_and_entry_order() -> None:
    maps: dict[str, dict[str, dict[str, object]]] = {
        "hints": {"note": {"text": "A setup note"}},
        "rules": {"conventions": {"source": "file::rules.md"}},
        "skills": {
            "review": {
                "source": "git::https://example.com/repo.git//skills/review?ref=v1",
                "preserve_bytes": ["fixtures/**"],
            }
        },
        "agents": {"reviewer": {"source": "file::reviewer.md"}},
    }
    bundle = decode("artifact-bundle", "team", maps)
    assert isinstance(bundle, ArtifactBundle)
    for kind, entries in maps.items():
        assert list(getattr(bundle, kind)) == list(entries)
        for name, expected in entries.items():
            assert getattr(bundle, kind)[name].model_dump(exclude_unset=True) == expected


@pytest.mark.parametrize("bundles", [["team", "team"], [" "]])
def test_bundle_selection_rejects_duplicates_and_blank_names(bundles: list[str]) -> None:
    with pytest.raises(ValidationError):
        ArtifactsConfig(bundles=bundles)


KINDS = [
    ("vm-template", VMTemplate, resolve_vm),
    ("agent-template", AgentTemplate, resolve_agent),
    ("workspace-template", WorkspaceTemplate, resolve_workspace),
    ("session-template", SessionTemplate, resolve_session),
]


SELECTIONS: list[tuple[dict[str, object] | None, list[str] | None, list[str]]] = [
    (None, ["parent", "shared"], ["base", "base"]),
    ({}, ["parent", "shared"], ["base", "base"]),
    ({"bundles": []}, ["parent", "shared"], ["base", "base"]),
    ({"bundles": ["shared", "child"]}, ["parent", "shared", "child"], ["base", "child", "child"]),
    ({"bundles": None}, None, []),
]


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize("selection,expected,owners", SELECTIONS)
@pytest.mark.parametrize("instance", [False, True])
def test_bundle_selections_append_in_order_and_preserve_reference_provenance(
    kind: str, model: type[Any], resolve: Callable[..., Any], selection, expected, owners, instance: bool
) -> None:
    import json

    from agentworks.instance_specs import parse_instance_spec
    from agentworks.schema import CapabilityBlock

    parent = model(name="base", artifacts=ArtifactsConfig(bundles=["parent", "shared"]))
    if kind == "session-template":
        parent = parent.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    authored = {} if selection is None else {"artifacts": selection}
    instance_kind = kind.removesuffix("-template")
    child = model.model_validate({"name": "child", "inherits": ["base"], **authored})
    if instance:
        overlay = parse_instance_spec(instance_kind, json.dumps(authored))
        assert overlay.payload.value == authored
        layered = resolve({"base": parent}, "base", overlay=overlay.declaration, instance_name="child")
        source = (instance_kind, "child")
    else:
        context = FinalizeContext(rows={kind: {"base": parent, "child": child}})
        dependencies = tuple(ref for ref in child.dependencies(context) if ref.kind == "artifact-bundle")
        layered = resolve({"base": parent, "child": child}, "child")
        source = (kind, "child")
    assert layered.value.artifacts.bundles == expected
    refs = artifact_references(layered.value.artifacts, source, layered.provenance)
    assert [ref.name for ref in refs] == (expected or [])
    assert [ref.declared_by for ref in refs] == [(source[0] if owner == "child" else kind, owner) for owner in owners]
    assert all(ref.source == source for ref in refs)
    if not instance:
        assert refs == dependencies
    if expected is None:
        assert not any(path[:2] == ("artifacts", "bundles") and len(path) > 2 for path in layered.provenance)


@pytest.mark.parametrize("selection,expected,owners", SELECTIONS)
def test_admin_instance_bundle_selections_share_additive_and_null_semantics(selection, expected, owners):
    import json

    from agentworks.instance_specs import parse_vm_instance_specs

    template = AdminConfig(name="base", artifacts=ArtifactsConfig(bundles=["parent", "shared"]))
    authored = {} if selection is None else {"artifacts": selection}
    overlay = parse_vm_instance_specs(None, json.dumps(authored))
    if authored:
        assert overlay is not None and overlay.payload.value["admin"] == authored
    else:
        assert overlay is None
    layered = resolve_admin(
        {"base": template}, "base", overlay=None if overlay is None else overlay.admin, instance_name="child"
    )
    assert layered.value.artifacts.bundles == expected
    refs = artifact_references(layered.value.artifacts, ("vm", "child"), layered.provenance)
    assert [ref.name for ref in refs] == (expected or [])
    assert [ref.declared_by for ref in refs] == [
        ("admin-template" if owner == "base" else "vm", owner) for owner in owners
    ]


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize("instance", [False, True])
def test_bundle_null_then_selection_never_revives_ancestor_references(kind, model, resolve, instance):
    from agentworks.schema import CapabilityBlock

    base = model(name="base", artifacts=ArtifactsConfig(bundles=["old"]))
    if kind == "session-template":
        base = base.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    clear = model.model_validate({"name": "clear", "inherits": ["base"], "artifacts": {"bundles": None}})
    child = model.model_validate({"name": "child", "inherits": ["clear"], "artifacts": {"bundles": ["new"]}})
    rows = {row.name: row for row in (base, clear, child)}
    cleared = resolve(rows, "clear")
    assert cleared.value.artifacts.bundles is None
    assert artifact_references(cleared.value.artifacts, (kind, "clear"), cleared.provenance) == ()
    if instance:
        overlay = model.model_validate({"name": "overlay", "artifacts": {"bundles": ["new"]}})
        result = resolve(rows, "clear", overlay=overlay, instance_name="running")
        source = (kind.removesuffix("-template"), "running")
    else:
        result = resolve(rows, "child")
        source = (kind, "child")
    assert result.value.artifacts.bundles == ["new"]
    refs = artifact_references(result.value.artifacts, source, result.provenance)
    assert [(ref.name, ref.declared_by) for ref in refs] == [("new", source)]
    from agentworks.value_provenance import longest_prefix_value

    layers = longest_prefix_value(result.provenance, ("artifacts", "bundles", 0))
    assert layers is not None and [layer.name for layer in layers] == [source[1]]


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize("authored,expected", [({}, None), ({"bundles": []}, []), ({"bundles": None}, None)])
def test_bundle_layers_after_null_stay_empty(kind, model, resolve, authored, expected):
    from agentworks.schema import CapabilityBlock

    base = model(name="base", artifacts=ArtifactsConfig(bundles=["old"]))
    if kind == "session-template":
        base = base.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    clear = model.model_validate({"name": "clear", "inherits": ["base"], "artifacts": {"bundles": None}})
    child = model.model_validate({"name": "child", "inherits": ["clear"], "artifacts": authored})
    result = resolve({row.name: row for row in (base, clear, child)}, "child")
    assert result.value.artifacts.bundles == expected
    assert artifact_references(result.value.artifacts, (kind, "child"), result.provenance) == ()


@pytest.mark.parametrize("declared", [False, True])
def test_bundle_reference_uses_ordinary_registry_miss_policy(declared: bool) -> None:
    from agentworks.errors import ConfigError
    from agentworks.origin import Origin
    from tests.conftest import registry_with_shell

    registry = registry_with_shell()
    origin = Origin.built_in(source="fixture")
    registry.add(
        "agent-template", "dev", AgentTemplate(name="dev", artifacts=ArtifactsConfig(bundles=["team"])), origin
    )
    if declared:
        # Loading declarations never tries to acquire this nonexistent source.
        registry.add(
            "artifact-bundle",
            "team",
            ArtifactBundle.model_validate(
                {
                    "name": "team",
                    "skills": {"review": {"source": "missing/directory"}},
                }
            ),
            origin,
        )
        registry.finalize()
        assert [(ref.kind, ref.name) for ref in registry.graph.edges_of("agent-template", "dev")] == [
            ("artifact-bundle", "team")
        ]
    else:
        with pytest.raises(ConfigError) as caught:
            registry.finalize()
        assert "artifact-bundle" in str(caught.value)
        assert "team" in str(caught.value)
