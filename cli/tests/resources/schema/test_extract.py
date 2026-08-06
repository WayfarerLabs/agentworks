"""Tests for ``extract_references``: the walk, parity, and FR18.

Totality lives in its own file (``test_extract_totality.py``) because it
is a property over generated inputs rather than a statement about one
shape.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from agentworks.resources.reference import ConfigReference, RefRelationship
from agentworks.resources.schema import AgwModel, RefOwner, SecretRef, extract_references

from ._fixture_models import (
    AwsLike,
    AzureLike,
    DiamondLike,
    FieldDiscriminatedSite,
    GithubLike,
    MappingRoot,
    ProxmoxLike,
    SelfReferential,
    SiteLike,
    StringRoot,
    TemplateLike,
    UndiscriminatedSite,
    UnmarkedLike,
)

OWNER = RefOwner(kind="git-credential", name="prod")


def names(refs: tuple[ConfigReference, ...]) -> list[str]:
    return [ref.name for ref in refs]


# --- marked scalars ---------------------------------------------------


def test_an_operator_written_name_wins() -> None:
    refs = extract_references(GithubLike, {"token": "custom-token"}, OWNER)
    assert refs == (ConfigReference(kind="secret", name="custom-token", usage="the auth token"),)


def test_an_omitted_field_falls_back_to_the_owner_template() -> None:
    assert names(extract_references(GithubLike, {}, OWNER)) == ["git-token-prod"]


def test_an_explicit_null_is_treated_as_omitted() -> None:
    assert names(extract_references(GithubLike, {"token": None}, OWNER)) == ["git-token-prod"]


@pytest.mark.parametrize("value", [8, "", True, [], {}, ["a"]])
def test_a_malformed_name_omits_the_edge(value: object) -> None:
    # The edge's identity is destroyed, so it is omitted rather than
    # guessed at; validation is where the shape error surfaces.
    assert extract_references(GithubLike, {"token": value}, OWNER) == ()


def test_a_marker_with_no_template_contributes_nothing_when_omitted() -> None:
    class NoDefault(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret")] | None = None

    assert extract_references(NoDefault, {}, OWNER) == ()


def test_an_unmarked_model_implies_nothing_at_any_depth() -> None:
    blob = {"name": "a", "port": 22, "nested": {"name": "lima", "vm_host": "h"}}
    assert extract_references(UnmarkedLike, blob, OWNER) == ()


# --- nested models ----------------------------------------------------


def test_a_nested_block_is_walked() -> None:
    blob = {"region": "eastus", "service_principal": {"client_id": "c", "secret": "sp-secret"}}
    assert names(extract_references(AzureLike, blob, OWNER)) == ["sp-secret"]


def test_a_nested_block_that_is_not_a_table_contributes_nothing() -> None:
    assert extract_references(AzureLike, {"service_principal": "nope"}, OWNER) == ()


def test_two_sibling_fields_of_one_nested_model_both_extract() -> None:
    # The path-scoped guard's regression test, kept as its own named
    # case because the failure it guards against is silent: an
    # accumulating visited set walks ``primary`` and skips ``fallback``,
    # building a graph that is missing an edge nothing reports.
    blob = {"primary": {"secret": "primary-secret"}, "fallback": {"secret": "fallback-secret"}}
    assert names(extract_references(DiamondLike, blob, OWNER)) == ["primary-secret", "fallback-secret"]


def test_a_self_referential_model_terminates() -> None:
    blob = {"secret": "top", "child": {"secret": "nested", "child": {"secret": "deeper"}}}
    # The path-scoped guard cuts the genuine cycle, which is the one case
    # that cannot terminate on its own.
    assert names(extract_references(SelfReferential, blob, OWNER)) == ["top"]


# --- marked lists -----------------------------------------------------


def test_a_marked_list_emits_one_edge_per_element() -> None:
    refs = extract_references(TemplateLike, {"inherits": ["base", "shared"]}, OWNER)
    assert names(refs) == ["base", "shared"]
    assert {ref.kind for ref in refs} == {"vm-template"}


def test_a_marked_list_carries_the_declared_relationship() -> None:
    refs = extract_references(TemplateLike, {"inherits": ["base"], "image": "ubuntu"}, OWNER)
    assert [ref.relationship for ref in refs] == [RefRelationship.INHERITS, RefRelationship.USES]


def test_a_marked_list_skips_the_elements_that_name_nothing() -> None:
    assert names(extract_references(TemplateLike, {"inherits": ["base", 8, "", None, "other"]}, OWNER)) == [
        "base",
        "other",
    ]


def test_a_marked_list_that_is_not_a_list_contributes_nothing() -> None:
    assert extract_references(TemplateLike, {"inherits": "base"}, OWNER) == ()


def test_an_omitted_list_has_no_default_identity() -> None:
    assert extract_references(TemplateLike, {}, OWNER) == ()


# --- discriminated unions ---------------------------------------------


def test_the_arm_the_raw_tag_names_is_walked() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(extract_references(SiteLike, blob, OWNER)) == ["lab-token"]


def test_an_arm_that_names_nothing_contributes_nothing() -> None:
    assert extract_references(SiteLike, {"platform": {"name": "lima", "vm_host": "h"}}, OWNER) == ()


def test_an_arms_own_templated_default_applies() -> None:
    assert names(extract_references(SiteLike, {"platform": {"name": "proxmox"}}, OWNER)) == ["proxmox-token"]


@pytest.mark.parametrize("tag", [None, 8, "unregistered", ""])
def test_a_tag_naming_no_arm_contributes_nothing(tag: object) -> None:
    assert extract_references(SiteLike, {"platform": {"name": tag}}, OWNER) == ()


def test_the_field_discriminator_spelling_walks_the_same_way() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(extract_references(FieldDiscriminatedSite, blob, OWNER)) == ["lab-token"]


def test_a_union_with_no_discriminator_has_no_addressable_arm() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert extract_references(UndiscriminatedSite, blob, OWNER) == ()


# --- surfaces that are not mapping-shaped -----------------------------


@pytest.mark.parametrize("blob", [None, "op://vault/item", 0, False, [], ["a"], object()])
def test_a_blob_that_is_not_a_table_contributes_nothing(blob: object) -> None:
    assert extract_references(GithubLike, blob, OWNER) == ()


def test_a_root_model_contributes_nothing() -> None:
    assert extract_references(StringRoot, "op://vault/item", OWNER) == ()
    assert extract_references(MappingRoot, {"token": "t"}, OWNER) == ()


def test_an_unresolvable_model_contributes_nothing_rather_than_raising() -> None:
    class Unresolvable(AgwModel):
        nested: NeverDefined  # type: ignore[name-defined]  # noqa: F821

    assert extract_references(Unresolvable, {"nested": {}}, OWNER) == ()


# --- parity with the shipped hand-rolled derivations -------------------


def test_parity_with_the_github_token_derivation() -> None:
    # capabilities/git_credential/base.py::token_dependency
    assert names(extract_references(GithubLike, {}, OWNER)) == ["git-token-prod"]
    assert names(extract_references(GithubLike, {"token": "override"}, OWNER)) == ["override"]
    assert extract_references(GithubLike, {"token": 8}, OWNER) == ()


def test_parity_with_the_azure_service_principal_derivation() -> None:
    # plugins/azure/platform.py::AzureVMPlatform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert extract_references(AzureLike, {"region": "eastus"}, site) == ()
    assert names(extract_references(AzureLike, {"service_principal": {"client_id": "c"}}, site)) == [
        "azure-client-secret"
    ]
    assert extract_references(AzureLike, {"service_principal": {"secret": ""}}, site) == ()


def test_parity_with_the_aws_credentials_derivation() -> None:
    # plugins/aws/platform.py::AwsEc2Platform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert extract_references(AwsLike, {"region": "us-east-1"}, site) == ()
    assert names(extract_references(AwsLike, {"credentials": {"access_key_id": "k"}}, site)) == [
        "aws-secret-access-key"
    ]
    assert extract_references(AwsLike, {"credentials": {"access_key_secret": 8}}, site) == ()


def test_parity_with_the_proxmox_token_derivation() -> None:
    # plugins/proxmox/platform.py::ProxmoxPlatform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert names(extract_references(ProxmoxLike, {"api_url": "https://pve"}, site)) == ["proxmox-token"]
    assert names(extract_references(ProxmoxLike, {"token_secret": "lab-token"}, site)) == ["lab-token"]
    assert extract_references(ProxmoxLike, {"token_secret": ""}, site) == ()


def test_the_usage_prose_is_carried_verbatim() -> None:
    # It ends up on the target's ReferenceEntry and in describe's
    # "Referenced by:" section, so it is operator-visible output.
    assert extract_references(ProxmoxLike, {}, OWNER)[0].usage == "the Proxmox API token"


# --- FR18: extraction is structural (issue #311) -----------------------


def test_renaming_a_marked_field_moves_the_extracted_reference() -> None:
    # The whole point of #311: the derivation lives in the model, so
    # nothing outside the class changes when the field does.
    class Renamed(AgwModel):
        pat: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]

    assert names(extract_references(Renamed, {"pat": "x"}, OWNER)) == ["x"]
    assert names(extract_references(Renamed, {}, OWNER)) == ["git-token-prod"]
    # And the old field name means nothing to the model now.
    assert names(extract_references(Renamed, {"token": "x"}, OWNER)) == ["git-token-prod"]


def test_adding_a_second_marked_field_adds_a_second_reference() -> None:
    class TwoSecrets(AgwModel):
        token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
        webhook_secret: Annotated[str, SecretRef(usage="the webhook signing secret")] | None = None

    assert names(extract_references(TwoSecrets, {"webhook_secret": "hook"}, OWNER)) == ["git-token-prod", "hook"]


def test_the_owner_kind_is_available_to_a_template() -> None:
    class KindTemplated(AgwModel):
        secret: Annotated[str, SecretRef(usage="u", default_template="{owner_kind}-{owner_name}")] | None = None

    assert names(extract_references(KindTemplated, {}, OWNER)) == ["git-credential-prod"]
