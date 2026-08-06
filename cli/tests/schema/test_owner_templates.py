"""The owner template resolves at validation as well as at extraction.

One declaration (the marker's ``default_template``) feeds both, and the
last test here is the one that matters: the validated instance and the
graph edge must name the SAME secret, or a resource would be validated
against one name and resolved against another.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworks.errors import StateError
from agentworks.schema import RefOwner, extract_references, validation_context

from ._fixture_models import AzureLike, CredsLike, GithubLike, SiteLike, UnmarkedLike

OWNER = RefOwner(kind="git-credential", name="prod")
CONTEXT = validation_context(OWNER)


def test_an_omitted_templated_field_resolves_from_the_owner() -> None:
    assert GithubLike.model_validate({}, context=CONTEXT).token == "git-token-prod"


def test_an_operator_written_value_wins() -> None:
    assert GithubLike.model_validate({"token": "custom"}, context=CONTEXT).token == "custom"


def test_an_explicit_null_resolves_like_an_omission() -> None:
    # Same call extraction makes, so the two cannot disagree on the name.
    # This is the validation half of the deliberate divergence pinned in
    # test_extract.py: azure's shipped validator RAISES on
    # ``secret: null``, telling the operator to omit the key, where the
    # model resolves it to the default instead.
    assert GithubLike.model_validate({"token": None}, context=CONTEXT).token == "git-token-prod"
    site = AzureLike.model_validate(
        {"region": "eastus", "service_principal": {"client_id": "c", "tenant_id": "t", "secret": None}},
        context=validation_context(RefOwner(kind="vm-site", name="lab")),
    )
    assert site.service_principal is not None
    assert site.service_principal.secret == "azure-client-secret"


def test_a_nested_models_templated_field_resolves_too() -> None:
    blob = {"region": "eastus", "service_principal": {"client_id": "c", "tenant_id": "t"}}
    site = AzureLike.model_validate(blob, context=validation_context(RefOwner(kind="vm-site", name="lab")))
    assert site.service_principal is not None
    assert site.service_principal.secret == "azure-client-secret"


def test_a_union_arms_templated_field_resolves_too() -> None:
    site = SiteLike.model_validate({"platform": {"name": "proxmox"}}, context=CONTEXT)
    assert getattr(site.platform, "token_secret", None) == "proxmox-token"


def test_a_model_with_no_templated_field_ignores_the_context() -> None:
    # Context-free validation stays legal for the models that do not need
    # an owner, which is most of them.
    assert UnmarkedLike.model_validate({"name": "a"}).name == "a"


def test_a_provided_value_needs_no_owner() -> None:
    assert GithubLike.model_validate({"token": "custom"}).token == "custom"


def test_a_missing_owner_is_a_framework_bug_not_an_operator_mistake() -> None:
    # A StateError, never a ConfigError: a call site forgot the context,
    # and blaming the operator for our omission would be a lie.
    with pytest.raises(StateError) as exc:
        GithubLike.model_validate({})
    assert "token" in str(exc.value)
    assert "validation_context" in str(exc.value)


def test_untemplated_fields_are_still_required() -> None:
    # Filling is per marker, not per model: the templated field resolves
    # while its untemplated siblings get pydantic's ordinary
    # required-field errors.
    with pytest.raises(ValidationError) as exc:
        AzureLike.model_validate({"service_principal": {}}, context=CONTEXT)
    missing = {error["loc"] for error in exc.value.errors()}
    assert missing == {("region",), ("service_principal", "client_id"), ("service_principal", "tenant_id")}


def test_a_templated_field_is_not_required_in_emitted_schema() -> None:
    """A field the model FILLS is not a field an operator must write, and
    emitted schema has to say so or an editor red-underlines the very
    omission this mechanism exists to resolve. Pydantic computes
    ``required`` from the declared field, which knows nothing about the
    before-validator, so ``AgwModel`` corrects it where the filling
    happens.

    Nested models and union arms too, since the filling reaches them.
    """
    assert "token" not in GithubLike.model_json_schema().get("required", ())

    azure = AzureLike.model_json_schema()
    assert azure["required"] == ["region"]
    assert azure["$defs"]["PrincipalLike"]["required"] == ["client_id", "tenant_id"]

    arm = SiteLike.model_json_schema()["$defs"]["ProxmoxArm"]
    assert "token_secret" not in arm.get("required", ())


def test_an_untemplated_reference_field_stays_required_in_emitted_schema() -> None:
    """The correction is per MARKER, exactly like the filling: a
    reference field with nothing to default to is still the operator's to
    write."""
    assert CredsLike.model_json_schema()["required"] == ["secret"]


def test_validation_and_extraction_derive_the_same_name() -> None:
    blob: dict[str, object] = {}
    validated = GithubLike.model_validate(blob, context=CONTEXT)
    (extracted,) = extract_references(GithubLike, blob, OWNER)
    assert validated.token == extracted.name
