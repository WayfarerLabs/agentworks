"""Declaration, inheritance, and graph boundaries for scoped artifact bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import JsonValue, ValidationError

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


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize("selection", [None, {}, {"bundles": ["child"]}, {"bundles": []}])
def test_bundle_selection_inherits_or_replaces_with_correct_reference_provenance(
    kind: str,
    model: type[Any],
    resolve: Callable[..., Any],
    selection: dict[str, object] | None,
) -> None:
    parent = model(name="base", artifacts=ArtifactsConfig(bundles=["parent", "second"]))
    authored = {} if selection is None else {"artifacts": selection}
    child = model.model_validate({"name": "child", "inherits": ["base"], **authored})
    # No harness is necessary for core bundle references, including sessions.
    context = FinalizeContext(rows={kind: {"base": parent, "child": child}})
    refs = [ref for ref in child.dependencies(context) if ref.kind == "artifact-bundle"]
    expected = ["parent", "second"] if not selection else selection["bundles"]
    assert [ref.name for ref in refs] == expected
    expected_owner = (kind, "base" if not selection else "child")
    assert all(ref.declared_by == expected_owner for ref in refs)
    if kind == "session-template":
        from agentworks.schema import CapabilityBlock

        parent = parent.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    layered = resolve({"base": parent, "child": child}, "child")
    assert layered.value.artifacts.bundles == expected
    assert artifact_references(layered.value.artifacts, (kind, "child"), layered.provenance) == tuple(refs)


@pytest.mark.parametrize("selection", [None, {}, {"bundles": []}, {"bundles": ["instance"]}])
def test_admin_instance_layer_preserves_or_replaces_template_selection(selection: dict[str, object] | None) -> None:
    template = AdminConfig(name="admin", artifacts=ArtifactsConfig(bundles=["base"]))
    authored = {} if selection is None else {"artifacts": selection}
    overlay = AdminConfig.model_validate({"name": "machine", **authored})
    layered = resolve_admin({"admin": template}, "admin", overlay=overlay, instance_name="machine")
    expected = ["base"] if not selection else selection["bundles"]
    assert layered.value.artifacts.bundles == expected
    refs = artifact_references(layered.value.artifacts, ("vm", "machine"), layered.provenance)
    expected_owner = ("admin-template", "admin") if not selection else ("vm", "machine")
    assert all(ref.declared_by == expected_owner for ref in refs)


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize("selection", [{}, {"bundles": []}, {"bundles": ["instance"]}])
def test_instance_overlays_replace_bundle_selection_and_publish_instance_provenance(
    kind: str,
    model: type[Any],
    resolve: Callable[..., Any],
    selection: dict[str, JsonValue],
) -> None:
    from agentworks.instance_overlay_codec import decode_overlay_model, encode_overlay_model
    from agentworks.schema import CapabilityBlock

    instance_kind = kind.removesuffix("-template")
    template = model(name="base", artifacts=ArtifactsConfig(bundles=["team"]))
    if kind == "session-template":
        template = template.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    overlay = decode_overlay_model(model, instance_kind, {"artifacts": selection})
    assert encode_overlay_model(overlay, instance_kind) == {"artifacts": selection}
    layered = resolve({"base": template}, "base", overlay=overlay, instance_name="running")
    expected = ["team"] if not selection else selection["bundles"]
    assert layered.value.artifacts.bundles == expected
    refs = artifact_references(layered.value.artifacts, (instance_kind, "running"), layered.provenance)
    assert [ref.name for ref in refs] == expected
    owner = (kind, "base") if not selection else (instance_kind, "running")
    assert all(ref.source == (instance_kind, "running") and ref.declared_by == owner for ref in refs)


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
