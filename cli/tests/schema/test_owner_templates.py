"""The owner template resolves through one fill, at the boundary.

One declaration (the marker's ``default_template``) is rendered into the
blob by ``filled_defaults`` before validation or extraction reads it, and
the last test here is the one that matters: the validated instance and
the graph edge must name the SAME secret, or a resource would be
validated against one name and resolved against another. They cannot
disagree, because both read the one filled blob.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworks.schema import AgwModel, RefOwner, extract_references, filled_defaults
from tests._emitted_schema import accepts_null, ref_extension

from ._fixture_models import AzureLike, CredsLike, GithubLike, ProxmoxArm, SiteLike, UnmarkedLike

OWNER = RefOwner(kind="git-credential", name="prod")


def test_an_omitted_templated_field_resolves_from_the_owner() -> None:
    filled = filled_defaults(GithubLike, {}, OWNER)
    assert filled == {"token": "git-token-prod"}
    assert GithubLike.model_validate(filled).token == "git-token-prod"


def test_an_operator_written_value_wins() -> None:
    blob = {"token": "custom"}
    assert filled_defaults(GithubLike, blob, OWNER) is blob
    assert GithubLike.model_validate(blob).token == "custom"


def test_an_explicit_null_resolves_like_an_omission() -> None:
    # Same treatment extraction gets, since both read the filled blob, so
    # the two cannot disagree on the name. This is the fill half of the
    # deliberate divergence pinned in test_extract.py: azure's shipped
    # validator RAISES on ``secret: null``, telling the operator to omit
    # the key, where the fill resolves it to the default instead.
    assert filled_defaults(GithubLike, {"token": None}, OWNER) == {"token": "git-token-prod"}
    blob = {"region": "eastus", "service_principal": {"client_id": "c", "tenant_id": "t", "secret": None}}
    site = AzureLike.model_validate(filled_defaults(AzureLike, blob, RefOwner(kind="vm-site", name="lab")))
    assert site.service_principal is not None
    assert site.service_principal.secret == "azure-client-secret"


def test_a_nested_models_templated_field_resolves_too() -> None:
    blob = {"region": "eastus", "service_principal": {"client_id": "c", "tenant_id": "t"}}
    site = AzureLike.model_validate(filled_defaults(AzureLike, blob, RefOwner(kind="vm-site", name="lab")))
    assert site.service_principal is not None
    assert site.service_principal.secret == "azure-client-secret"


def test_a_union_arms_templated_field_resolves_too() -> None:
    site = SiteLike.model_validate(filled_defaults(SiteLike, {"platform": {"name": "proxmox"}}, OWNER))
    assert getattr(site.platform, "token_secret", None) == "proxmox-token"


def test_a_model_with_no_templated_field_is_left_untouched() -> None:
    # Copy-on-write: the common case (nothing to fill) hands back the
    # very object, so filling always is cheaper than remembering which
    # models need it.
    blob = {"name": "a"}
    assert filled_defaults(UnmarkedLike, blob, OWNER) is blob
    assert UnmarkedLike.model_validate(blob).name == "a"


def test_a_provided_value_needs_no_owner() -> None:
    assert GithubLike.model_validate({"token": "custom"}).token == "custom"


def test_an_unfilled_blob_fails_as_an_ordinary_missing_field() -> None:
    """A boundary that forgets the fill gets pydantic's plain
    required-field error, which is a statement about the blob's content.

    This is the standalone-construction property: the model itself needs
    no external context of any kind, so there is no framework plumbing to
    forget mid-validation and no state error naming it. The field really
    is required; the fill is what satisfies it for a document that omits
    it.
    """
    with pytest.raises(ValidationError) as exc:
        GithubLike.model_validate({})
    assert {error["loc"] for error in exc.value.errors()} == {("token",)}


def test_a_templated_arm_is_constructible_as_an_instance() -> None:
    """Building a model is a fact about its content alone, so an arm
    carrying a templated marker constructs like any other model, at class
    definition included. Under the context mechanism this raised a
    ``StateError`` at import of the declaring module, which is what made
    such an arm unusable as a default instance."""
    arm = ProxmoxArm(name="proxmox", token_secret="explicit")
    assert arm.token_secret == "explicit"

    class Defaulted(AgwModel):
        platform: ProxmoxArm = ProxmoxArm(name="proxmox", token_secret="explicit")

    assert Defaulted.model_validate({}).platform.token_secret == "explicit"


def test_untemplated_fields_are_still_required() -> None:
    # Filling is per marker, not per model: the templated field resolves
    # while its untemplated siblings get pydantic's ordinary
    # required-field errors.
    blob = filled_defaults(AzureLike, {"service_principal": {}}, OWNER)
    with pytest.raises(ValidationError) as exc:
        AzureLike.model_validate(blob)
    missing = {error["loc"] for error in exc.value.errors()}
    assert missing == {("region",), ("service_principal", "client_id"), ("service_principal", "tenant_id")}


def test_a_templated_field_is_not_required_in_emitted_schema() -> None:
    """A field the fill resolves is not a field an operator must write,
    and emitted schema has to say so or an editor red-underlines the very
    omission the mechanism exists to resolve. Pydantic computes
    ``required`` from the declared field, which knows nothing about the
    boundary fill, so ``AgwModel`` corrects it.

    Nested models and union arms too, since the fill reaches them.
    """
    assert "token" not in GithubLike.model_json_schema().get("required", ())

    azure = AzureLike.model_json_schema()
    assert azure["required"] == ["region"]
    assert azure["$defs"]["PrincipalLike"]["required"] == ["client_id", "tenant_id"]

    arm = SiteLike.model_json_schema()["$defs"]["ProxmoxArm"]
    assert "token_secret" not in arm.get("required", ())


def test_a_templated_field_is_nullable_in_emitted_schema() -> None:
    """The other half of the same rule, and the half that was missing.

    The fill treats an omission and an explicit ``null`` alike,
    deliberately, so a schema that drops the field from ``required`` and
    leaves it non-nullable still red-underlines ``token: null``: the same
    instruction spelled out, and one the loader accepts. Nested models
    and union arms too.
    """
    assert accepts_null(GithubLike.model_json_schema()["properties"]["token"])

    principal = AzureLike.model_json_schema()["$defs"]["PrincipalLike"]
    assert accepts_null(principal["properties"]["secret"])
    assert not accepts_null(principal["properties"]["client_id"])

    arm = SiteLike.model_json_schema()["$defs"]["ProxmoxArm"]
    assert accepts_null(arm["properties"]["token_secret"])


def test_a_widened_field_keeps_its_hover_text_and_its_marker() -> None:
    """The null arm must not bury what an editor shows. What DESCRIBES the
    property rides outside the ``anyOf`` and what CONSTRAINS one shape
    rides inside, which is byte for byte what pydantic emits for a
    natively optional field, so a widened property is indistinguishable
    from a declared one.

    The marker is on the describing side of that split, and a field's
    marker describes the field: it says what the omission this widening
    exists for resolves to, which is the one thing a hover on an omitted
    ``token`` should show.
    """
    token = GithubLike.model_json_schema()["properties"]["token"]
    assert token["title"] == "Token"
    constrained, null = token["anyOf"]
    assert null == {"type": "null"}
    assert constrained == {"type": "string"}
    marker = ref_extension(token)
    assert marker is not None
    assert marker["default_template"] == "git-token-{owner_name}"


def test_an_untemplated_reference_field_stays_required_in_emitted_schema() -> None:
    """The correction is per MARKER, exactly like the fill: a reference
    field with nothing to default to is still the operator's to write,
    and still not nullable."""
    schema = CredsLike.model_json_schema()
    assert schema["required"] == ["secret"]
    assert not accepts_null(schema["properties"]["secret"])


def test_validation_and_extraction_derive_the_same_name() -> None:
    # The agreement is structural: one fill runs, and both read its
    # output, so there is no second renderer to drift.
    filled = filled_defaults(GithubLike, {}, OWNER)
    validated = GithubLike.model_validate(filled)
    (extracted,) = extract_references(GithubLike, filled)
    assert validated.token == extracted.name
