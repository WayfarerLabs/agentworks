"""Direct artifact authoring across schema, inheritance, and instance boundaries."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentworks.artifacts.declarations import ArtifactsConfig
from agentworks.errors import ValidationError as SpecValidationError
from agentworks.instance_specs import decode_stored_overlay, parse_instance_spec, parse_vm_instance_specs
from agentworks.manifests.emit import document_schema
from agentworks.resources.graph import FinalizeContext
from agentworks.schema import CapabilityBlock
from agentworks.vms.admin import AdminConfig
from agentworks.vms.admin_templates import resolve_from_dict_with_provenance as resolve_admin
from tests.artifacts.test_declarations import KINDS

MAPS = {
    "hints": {"context": {"text": "Setup context"}},
    "rules": {"style": {"source": "file::style.md"}},
    "skills": {"review": {"source": "file::skills/review"}},
    "agents": {"reviewer": {"source": "file::reviewer.md"}},
    "mcp_servers": {"tools": {"definition": {"type": "stdio", "command": "fixture-command"}}},
}


@pytest.mark.parametrize("kind", [row[0] for row in KINDS] + ["admin-template"])
def test_direct_definitions_reuse_all_five_strict_bundle_maps_in_generated_schema(kind):
    validator = Draft202012Validator(document_schema(kind))
    document = {
        "apiVersion": "agentworks/v1",
        "kind": kind,
        "metadata": {"name": "fixture"},
        "spec": {"artifacts": {"declare": MAPS}},
    }
    validator.validate(document)
    document["spec"]["artifacts"]["bundles"] = None
    validator.validate(document)
    document["spec"]["artifacts"]["declare"] = None
    validator.validate(document)
    document["spec"]["artifacts"]["declare"] = {"bundles": ["nested"]}
    assert list(validator.iter_errors(document))


@pytest.mark.parametrize(
    "definitions",
    [
        {"name": "named"},
        {"inherits": ["parent"]},
        {"bundles": ["nested"]},
        {"unknown": {}},
        {"hints": {"Upper": {"text": "bad name"}}},
        {"hints": {"context": {"text": "x", "source": "file::x"}}},
        {"rules": {"style": {"text": "x", "unknown": True}}},
        {"skills": {"review": {"source": "file::review", "preserve_bytes": ["../outside"]}}},
        {"agents": {"reviewer": {"text": "no source"}}},
        {"mcp_servers": {"tools": {"definition": {"type": "unknown"}}}},
        *({kind: {"removed": None}} for kind in MAPS),
    ],
)
def test_direct_structure_rejects_envelopes_unknown_fields_and_entry_tombstones(definitions):
    with pytest.raises(ValidationError):
        ArtifactsConfig.model_validate({"declare": definitions})


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
def test_direct_type_maps_merge_across_template_layers_and_replace_whole_specs(kind, model, resolve):
    base = model.model_validate(
        {
            "name": "base",
            "artifacts": {
                "bundles": ["team"],
                "declare": {
                    "hints": {"same": {"source": "file::missing"}, "kept": {"text": "kept"}},
                    "rules": {"same": {"text": "rule"}},
                },
            },
        }
    )
    if kind == "session-template":
        base = base.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    child = model.model_validate(
        {"name": "child", "inherits": ["base"], "artifacts": {"declare": {"hints": {"same": {"text": "child"}}}}}
    )
    grand = model.model_validate({"name": "grand", "inherits": ["child"], "artifacts": {"declare": {}}})
    rows = {row.name: row for row in (base, child, grand)}
    result = resolve(rows, "grand")
    direct = result.value.artifacts.declare
    assert direct.hints["same"].text == "child" and direct.hints["same"].source is None
    assert list(direct.hints) == ["same", "kept"]
    assert direct.rules["same"].text == "rule"
    layers = result.provenance[("artifacts", "declare", "hints", "same")]
    assert layers[-1].name == "child"
    refs = grand.dependencies(FinalizeContext(rows={kind: rows}))
    assert [(ref.kind, ref.name) for ref in refs if ref.kind == "artifact-bundle"] == [("artifact-bundle", "team")]
    assert not any(ref.name in {"same", "kept", "missing"} for ref in refs)


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
@pytest.mark.parametrize(
    "selection,expected_direct,expected_bundles",
    [
        ({}, True, ["team"]),
        ({"declare": {}}, True, ["team"]),
        ({"declare": None}, False, ["team"]),
        ({"bundles": []}, True, ["team"]),
        ({"bundles": [], "declare": None}, False, ["team"]),
        ({"bundles": None}, True, None),
        ({"bundles": None, "declare": None}, False, None),
    ],
)
def test_instance_overlay_inherit_and_clear_are_independent(
    kind, model, resolve, selection, expected_direct, expected_bundles
):
    base = model.model_validate(
        {"name": "base", "artifacts": {"bundles": ["team"], "declare": {"hints": {"note": {"text": "inherited"}}}}}
    )
    if kind == "session-template":
        base = base.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    instance_kind = kind.removesuffix("-template")
    overlay = parse_instance_spec(instance_kind, json.dumps({"artifacts": selection}))
    assert overlay.payload.value == {"artifacts": selection}
    value = resolve({"base": base}, "base", overlay=overlay.declaration, instance_name="running").value
    assert bool(value.artifacts.declare) is expected_direct
    assert value.artifacts.bundles == expected_bundles


@pytest.mark.parametrize(
    "selection",
    [{}, {"declare": {}}, {"declare": None}, {"bundles": []}, {"bundles": None}, {"bundles": None, "declare": None}],
)
def test_admin_overlay_preserves_direct_clear_through_composite_vm_codec(selection):
    base = AdminConfig.model_validate(
        {"name": "base", "artifacts": {"bundles": ["team"], "declare": {"hints": {"note": {"text": "base"}}}}}
    )
    overlays = parse_vm_instance_specs(None, json.dumps({"artifacts": selection}))
    assert overlays is not None
    assert overlays.payload.value["admin"] == {"artifacts": selection}
    value = resolve_admin({"base": base}, "base", overlay=overlays.admin).value
    assert bool(value.artifacts.declare) is ("declare" not in selection or selection["declare"] is not None)
    assert value.artifacts.bundles == (None if "bundles" in selection and selection["bundles"] is None else ["team"])


@pytest.mark.parametrize(("kind", "model", "resolve"), KINDS, ids=[row[0] for row in KINDS])
def test_template_direct_clear_then_redeclare_does_not_revive_ancestor_entries(kind, model, resolve):
    base = model.model_validate({"name": "base", "artifacts": {"declare": {"hints": {"old": {"text": "old"}}}}})
    if kind == "session-template":
        base = base.model_copy(update={"harness_integration": CapabilityBlock.of("shell")})
    clear = model.model_validate({"name": "clear", "inherits": ["base"], "artifacts": {"declare": None}})
    child = model.model_validate(
        {"name": "child", "inherits": ["clear"], "artifacts": {"declare": {"hints": {"new": {"text": "new"}}}}}
    )
    result = resolve({row.name: row for row in (base, clear, child)}, "child")
    assert list(result.value.artifacts.declare.hints) == ["new"]
    assert not any(path[:4] == ("artifacts", "declare", "hints", "old") for path in result.provenance)


@pytest.mark.parametrize("kind", ["vm", "agent", "workspace", "session"])
@pytest.mark.parametrize(
    "raw",
    [{"artifacts": None}, {"artifacts": {"declare": {"hints": {"note": None}}}}],
)
def test_artifact_clears_do_not_allow_null_configs_or_entry_tombstones(kind, raw):
    with pytest.raises(SpecValidationError):
        parse_instance_spec(kind, json.dumps(raw))


@pytest.mark.parametrize("kind", ["vm", "agent", "workspace", "session"])
@pytest.mark.parametrize("selection", [{"bundles": None}, {"declare": None}, {"bundles": None, "declare": None}])
def test_stored_instance_codec_retains_independent_clears(db, kind, selection):
    raw = {"artifacts": selection}
    if kind == "vm":
        overlays = parse_vm_instance_specs(json.dumps(raw), json.dumps(raw))
        assert overlays is not None
        payload = overlays.payload
    else:
        payload = parse_instance_spec(kind, json.dumps(raw)).payload
    db.instance_state.put_desired_overlay(kind, "fixture", payload)
    record = db.instance_state.get_desired_overlay(kind, "fixture")
    assert record is not None
    assert decode_stored_overlay(record).payload == payload
