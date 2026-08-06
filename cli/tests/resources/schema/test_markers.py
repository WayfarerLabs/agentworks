"""Tests for the reference markers and their ``x-agw-ref`` encoding.

The round-trip assertions matter more than they look: emitted JSON
Schema and the field-reference stream are SIBLING derivations from one
authored marker, and these tests are half of what keeps them from
drifting (the other half lives in ``test_fields.py``).
"""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import Discriminator, Field

from agentworks.errors import StateError
from agentworks.resources.reference import RefRelationship
from agentworks.resources.schema import (
    REF_SCHEMA_KEY,
    AgwModel,
    RefOwner,
    ResourceRef,
    SecretRef,
)

OWNER = RefOwner(kind="git-credential", name="prod")


class Principal(AgwModel):
    """The nested block a marked field can live in."""

    secret: Annotated[str, SecretRef(usage="the client secret", default_template="azure-client-secret")]


class LimaArm(AgwModel):
    """The lima arm of the fixture union."""

    name: Literal["lima"]
    vm_host: str | None = None


class ProxmoxArm(AgwModel):
    """The proxmox arm of the fixture union."""

    name: Literal["proxmox"]
    token_secret: Annotated[str, SecretRef(usage="the Proxmox API token", default_template="proxmox-token")]


class Marked(AgwModel):
    """Every marked field shape in one model."""

    token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
    template: Annotated[str, ResourceRef(kind="vm-template", usage="the base image")]
    inherits: list[
        Annotated[
            str,
            ResourceRef(kind="vm-template", usage="a parent template", relationship=RefRelationship.INHERITS),
        ]
    ] = Field(default_factory=list)
    principal: Principal | None = None
    platform: Annotated[LimaArm | ProxmoxArm, Discriminator("name")] | None = None


def test_secret_ref_defaults_its_kind() -> None:
    assert SecretRef(usage="u").kind == "secret"
    assert ResourceRef(kind="vm-template", usage="u").kind == "vm-template"


def test_markers_default_to_a_uses_relationship() -> None:
    assert SecretRef(usage="u").relationship is RefRelationship.USES


def test_render_default_substitutes_the_owner() -> None:
    marker = SecretRef(usage="u", default_template="{owner_kind}-token-{owner_name}")
    assert marker.render_default(OWNER) == "git-credential-token-prod"


def test_render_default_is_none_without_a_template() -> None:
    assert SecretRef(usage="u").render_default(OWNER) is None


def test_a_constant_template_is_just_a_template_with_no_placeholder() -> None:
    assert SecretRef(usage="u", default_template="azure-client-secret").render_default(OWNER) == "azure-client-secret"


def test_owner_display_reproduces_the_shipped_owner_string() -> None:
    assert OWNER.display == "git-credential/prod"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("git-token-{repo}", "unknown placeholder {repo}"),
        ("git-token-{owner_name:d}", "format spec 'd'"),
        ("git-token-{owner_name!r}", "conversion !r"),
        ("git-token-{}", "positional placeholder {}"),
        ("git-token-{0}", "positional placeholder {0}"),
        ("git-token-{owner_name.upper}", "unknown placeholder {owner_name.upper}"),
        ("git-token-{", "malformed reference default_template"),
    ],
)
def test_a_template_the_extractor_could_not_render_is_refused_at_construction(template: str, expected: str) -> None:
    # Loud at import of the module declaring the model, which is what
    # lets extraction promise it never raises.
    with pytest.raises(StateError) as exc:
        SecretRef(usage="u", default_template=template)
    assert expected in str(exc.value)


def test_the_whole_vocabulary_is_accepted() -> None:
    marker = SecretRef(usage="u", default_template="{owner_kind}/{owner_name}")
    assert marker.default_template == "{owner_kind}/{owner_name}"


def test_schema_extension_carries_all_four_keys() -> None:
    assert SecretRef(usage="the auth token", default_template="git-token-{owner_name}").schema_extension() == {
        "kind": "secret",
        "usage": "the auth token",
        "default_template": "git-token-{owner_name}",
        "relationship": "uses",
    }


def test_a_marker_with_no_template_still_emits_the_key() -> None:
    assert ResourceRef(kind="vm-template", usage="the base image").schema_extension() == {
        "kind": "vm-template",
        "usage": "the base image",
        "default_template": None,
        "relationship": "uses",
    }


def test_a_scalar_marker_reaches_emitted_json_schema() -> None:
    schema = Marked.model_json_schema()
    assert schema["properties"]["token"][REF_SCHEMA_KEY] == {
        "kind": "secret",
        "usage": "the auth token",
        "default_template": "git-token-{owner_name}",
        "relationship": "uses",
    }
    assert schema["properties"]["template"][REF_SCHEMA_KEY]["kind"] == "vm-template"


def test_a_list_marker_reaches_the_item_schema() -> None:
    items = Marked.model_json_schema()["properties"]["inherits"]["items"]
    assert items[REF_SCHEMA_KEY]["relationship"] == "inherits"


def test_a_nested_model_carries_its_marker_in_defs() -> None:
    defs = Marked.model_json_schema()["$defs"]
    assert defs["Principal"]["properties"]["secret"][REF_SCHEMA_KEY]["default_template"] == "azure-client-secret"


def test_each_union_arm_carries_its_own_markers() -> None:
    defs = Marked.model_json_schema()["$defs"]
    assert REF_SCHEMA_KEY not in defs["LimaArm"]["properties"]["vm_host"]
    assert defs["ProxmoxArm"]["properties"]["token_secret"][REF_SCHEMA_KEY]["default_template"] == "proxmox-token"


def test_the_extension_key_is_ignored_by_conforming_validators() -> None:
    # Not a behavior test so much as a statement of the contract: the
    # key is under the reserved ``x-`` prefix, so schema-aware editor
    # tooling shows it without validating against it.
    assert REF_SCHEMA_KEY.startswith("x-")
