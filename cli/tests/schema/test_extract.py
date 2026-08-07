"""Tests for ``extract_references``: the walk, parity, and FR18.

Totality lives in its own file (``test_extract_totality.py``) because it
is a property over generated inputs rather than a statement about one
shape.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from agentworks.schema import AgwModel, AgwRootModel, RefOwner, SecretRef, extract_references
from agentworks.schema.reference import ConfigReference, RefRelationship

from ._fixture_models import (
    AwsLike,
    AzureLike,
    CatalogLike,
    DiamondLike,
    FieldDiscriminatedSite,
    GithubLike,
    MappingRoot,
    MultiArmMarked,
    NeverResolved,
    NumericallyTaggedSite,
    OptionalUnionSite,
    ProxmoxLike,
    RenamedArmSite,
    ResolvesToUnbuildable,
    ScalarOrBlockLike,
    SelfReferential,
    SelfReferentialUnion,
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


# An explicit ``null`` is treated as an omission, which is a DIVERGENCE
# from three shipped validators rather than parity with them, so it is
# pinned with the other three in
# ``test_an_explicit_null_diverges_from_three_shipped_validators``.


# The two branches ``_scalar_edge`` can reject a written value on: not a
# string at all, and a string with nothing in it. Every other malformed
# value the walk could be handed reaches one of these two, so the
# totality suite is where the rest of the garbage belongs.
@pytest.mark.parametrize("value", [8, ""])
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


def test_a_recursive_model_walks_finite_data_all_the_way_down() -> None:
    """Recursion in the TYPE is not a cycle in the DATA.

    This blob nests three levels and ends. Pydantic validates every one of
    them, so a walker that stopped at the first repeated type would leave
    two secrets out of the dependency graph with nothing reported: the
    resource would be missing at runtime, in a run that finalized clean.
    """
    blob = {"secret": "top", "child": {"secret": "nested", "child": {"secret": "deeper"}}}
    assert names(extract_references(SelfReferential, blob, OWNER)) == ["top", "nested", "deeper"]


def test_data_reachable_from_itself_terminates() -> None:
    """The one input that cannot terminate on its own, which a YAML anchor
    can write (``child: *self``). The edges it does name are still
    extracted; only the second visit to the same value is cut."""
    blob: dict[str, object] = {"secret": "top"}
    blob["child"] = blob

    assert names(extract_references(SelfReferential, blob, OWNER)) == ["top"]


def test_a_data_cycle_through_two_values_terminates() -> None:
    """A cycle the guard cannot see by looking at one value: two tables
    that name each other."""
    outer: dict[str, object] = {"secret": "outer"}
    inner: dict[str, object] = {"secret": "inner", "child": outer}
    outer["child"] = inner

    assert names(extract_references(SelfReferential, outer, OWNER)) == ["outer", "inner"]


def test_deeply_nested_finite_data_neither_truncates_nor_overflows() -> None:
    """Depth is the operator's to choose, and the totality contract has no
    exception for "too deep". A recursive walk raises ``RecursionError``
    here well before the bottom, which is a raised exception from a
    function documented never to raise."""
    depth = 5_000
    blob: dict[str, object] = {"secret": "bottom"}
    for _ in range(depth):
        blob = {"child": blob}

    assert names(extract_references(SelfReferential, blob, OWNER)) == ["bottom"]


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


def test_an_omitted_list_has_no_default_identity() -> None:
    assert extract_references(TemplateLike, {}, OWNER) == ()


# --- collections of models --------------------------------------------


def test_every_element_of_a_list_of_models_is_walked() -> None:
    blob = {"accounts": [{"secret": "first"}, {"secret": "second"}]}
    assert names(extract_references(CatalogLike, blob, OWNER)) == ["first", "second"]


def test_every_value_of_a_table_of_models_is_walked() -> None:
    blob = {"accounts_by_name": {"prod": {"secret": "prod-secret"}, "dev": {"secret": "dev-secret"}}}
    assert names(extract_references(CatalogLike, blob, OWNER)) == ["prod-secret", "dev-secret"]


def test_every_value_of_a_table_of_names_becomes_an_edge() -> None:
    blob = {"extra_secrets": {"one": "first", "two": "second", "bad": 8}}
    assert names(extract_references(CatalogLike, blob, OWNER)) == ["first", "second"]


def test_a_tuple_of_names_is_a_sequence_like_a_list() -> None:
    assert names(extract_references(CatalogLike, {"templates": ["base", "other"]}, OWNER)) == ["base", "other"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # A sequence handed the two values that are ITERABLE without being
        # a sequence, which is what ``_elements_of`` has to refuse by shape
        # rather than by truthiness: a bare string would otherwise
        # decompose into characters and a table would hand over its keys.
        ("templates", "base"),
        ("templates", {"one": "base"}),
        # And a MAPPING handed the sequence, which is the mirror: a list is
        # not a table, and reading its entries as values would emit an edge
        # per element of a field that declares none.
        ("extra_secrets", ["one", "two"]),
    ],
)
def test_a_collection_that_is_not_one_contributes_nothing(field: str, value: object) -> None:
    # Fields whose ELEMENTS are marked, so a refusal that leaked would
    # emit edges rather than quietly walking blocks that name nothing.
    assert extract_references(CatalogLike, {field: value}, OWNER) == ()


def test_malformed_elements_are_skipped_and_the_rest_still_extract() -> None:
    blob = {"accounts": [{"secret": "kept"}, "not a table", None, 8, {"secret": ""}, {"secret": "also-kept"}]}
    assert names(extract_references(CatalogLike, blob, OWNER)) == ["kept", "also-kept"]


def test_a_recursive_model_walks_a_finite_collection_all_the_way_down() -> None:
    blob = {"secret": "top", "children": [{"secret": "nested", "children": [{"secret": "deeper"}]}]}
    assert names(extract_references(SelfReferential, blob, OWNER)) == ["top", "nested", "deeper"]


def test_a_data_cycle_through_a_collection_terminates() -> None:
    """The guard has to hold on both routes into a model: the field and
    the collection element."""
    blob: dict[str, object] = {"secret": "top"}
    blob["children"] = [blob]

    assert names(extract_references(SelfReferential, blob, OWNER)) == ["top"]


# --- discriminated unions ---------------------------------------------


def test_the_arm_the_raw_tag_names_is_walked() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(extract_references(SiteLike, blob, OWNER)) == ["lab-token"]


def test_an_arm_that_names_nothing_contributes_nothing() -> None:
    assert extract_references(SiteLike, {"platform": {"name": "lima", "vm_host": "h"}}, OWNER) == ()


def test_an_arms_own_templated_default_applies() -> None:
    assert names(extract_references(SiteLike, {"platform": {"name": "proxmox"}}, OWNER)) == ["proxmox-token"]


@pytest.mark.parametrize("tag", [8, "unregistered"])
def test_a_tag_naming_no_arm_contributes_nothing(tag: object) -> None:
    # ``_arm_block``'s two rejections: a tag that is not a name at all,
    # and a name no arm answers to.
    assert extract_references(SiteLike, {"platform": {"name": tag}}, OWNER) == ()


@pytest.mark.parametrize("model_cls", [SiteLike, FieldDiscriminatedSite, OptionalUnionSite])
def test_every_legal_union_spelling_walks_the_same_way(model_cls: type[AgwModel]) -> None:
    # Pydantic validates all three identically, so a lookup that missed
    # one would build a silently wrong graph rather than fail.
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(extract_references(model_cls, blob, OWNER)) == ["lab-token"]


def test_an_arm_answering_to_two_tags_is_reachable_by_both() -> None:
    for tag in ("aws-ec2", "ec2"):
        blob = {"platform": {"name": tag, "access_key_secret": "key"}}
        assert names(extract_references(RenamedArmSite, blob, OWNER)) == ["key"], tag


def test_a_union_tagged_by_a_non_name_has_no_addressable_arm() -> None:
    # A documented boundary: every discriminator in this framework is a
    # capability or kind name.
    blob = {"thing": {"version": 1, "token_secret": "x"}}
    assert extract_references(NumericallyTaggedSite, blob, OWNER) == ()


def test_a_marker_inside_a_multi_arm_union_is_still_found() -> None:
    assert names(extract_references(MultiArmMarked, {"secret": "named"}, OWNER)) == ["named"]
    assert names(extract_references(MultiArmMarked, {}, OWNER)) == ["multi-arm-secret"]
    assert extract_references(MultiArmMarked, {"secret": 8}, OWNER) == ()


def test_a_union_with_no_discriminator_has_no_addressable_arm() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert extract_references(UndiscriminatedSite, blob, OWNER) == ()


# --- untagged scalar-or-block unions ----------------------------------
#
# No tag addresses these, so the value's own shape does: a table is the
# block, a scalar is one of the scalar members. The last two cases are
# the two edges of that rule, one per direction: a table stops naming the
# block once another member could accept one, and a scalar never names it
# even where the block would read a bare value.


def test_a_union_written_as_a_table_walks_its_block() -> None:
    blob = {"mapping": {"secret": "named-in-the-arm"}}
    assert names(extract_references(ScalarOrBlockLike, blob, OWNER)) == ["named-in-the-arm"]


def test_a_union_written_as_a_scalar_contributes_nothing() -> None:
    # The other arm entirely, and the operator wrote a name that belongs
    # to the backend rather than to agentworks.
    assert extract_references(ScalarOrBlockLike, {"mapping": "op://vault/item"}, OWNER) == ()


def test_an_untagged_union_held_by_a_collection_is_read_per_element() -> None:
    blob = {
        "mappings": {"a": {"secret": "in-a-table"}, "b": "a scalar element"},
        "mapping_list": ["another scalar", {"secret": "in-a-list"}],
    }
    assert names(extract_references(ScalarOrBlockLike, blob, OWNER)) == ["in-a-table", "in-a-list"]


def test_a_union_block_reachable_from_itself_terminates() -> None:
    blob: dict[str, object] = {"name": "anchored"}
    blob["child"] = blob

    assert extract_references(SelfReferentialUnion, blob, OWNER) == ()


def test_a_union_that_also_offers_a_bare_table_addresses_no_arm() -> None:
    """A table is ambiguous the moment a non-model member accepts one.

    Pydantic settles it by trying the arms, which this walker runs before
    and cannot do. Naming the block anyway would invent an edge: the blob
    below validates as the plain table, so ``git-token-prod`` would be a
    secret the operator never asked for, and finalize would refuse the
    config over a resource nobody wrote.
    """

    class Ambiguous(AgwModel):
        thing: dict[str, str] | GithubLike | None = None

    assert extract_references(Ambiguous, {"thing": {"api_url": "https://example"}}, OWNER) == ()


def test_a_scalar_is_not_read_as_a_root_model_arm() -> None:
    """The reason the walk tests the value's shape itself rather than
    leaving it to the block: an ordinary model reads its fields out of a
    mapping and a ROOT model reads the blob directly, so the string below
    would be extracted as the root model's own marked value."""

    class Rooted(AgwRootModel[Annotated[str, SecretRef(usage="a rooted secret")]]):
        """A root model whose root IS the marked scalar."""

    class HoldsRooted(AgwModel):
        thing: str | Rooted | None = None

    assert extract_references(HoldsRooted, {"thing": "a plain string"}, OWNER) == ()


# --- surfaces that are not mapping-shaped -----------------------------


@pytest.mark.parametrize("blob", [None, "op://vault/item", ["a"]])
def test_a_blob_that_is_not_a_table_contributes_nothing(blob: object) -> None:
    # Refused by SHAPE rather than by truthiness, which is what the
    # non-empty list is here to say: a walk guarded by ``if not blob``
    # would go on to read keys off it.
    assert extract_references(GithubLike, blob, OWNER) == ()


def test_a_bare_scalar_root_names_nothing() -> None:
    # Every shipped backend mapping: an env var name, an ``op://``
    # reference. Neither is an agentworks Resource.
    assert extract_references(StringRoot, "op://vault/item", OWNER) == ()


def test_a_model_rooted_root_model_carries_its_roots_references() -> None:
    # The alternative would render the marked field in a generated
    # sample and never emit its edge, which is the silent-missing-edge
    # outcome, reached by a different route.
    assert names(extract_references(MappingRoot, {"token": "t"}, OWNER)) == ["t"]
    assert names(extract_references(MappingRoot, {}, OWNER)) == ["git-token-prod"]
    assert extract_references(MappingRoot, "not a table", OWNER) == ()


@pytest.mark.parametrize("model_cls", [NeverResolved, ResolvesToUnbuildable])
def test_a_model_that_cannot_be_built_contributes_nothing_rather_than_raising(model_cls: type[AgwModel]) -> None:
    # Two different failures, and the second is the one that used to
    # escape: pydantic's own ``raise_errors=False`` covers an annotation
    # that never resolves, not one that resolves to a type it cannot
    # build a schema for.
    assert extract_references(model_cls, {"secret": "named"}, OWNER) == ()


# --- parity with the shipped hand-rolled derivations -------------------


# Parity with ``capabilities/git_credential/base.py::token_dependency`` is
# the marked-scalar section at the top of this file: ``GithubLike`` IS the
# git-credential shape, so the three cases that derivation has (a written
# name, an omission falling back to the template, a malformed value naming
# nothing) are already pinned there one apiece.


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


# --- the one deliberate divergence from shipped behavior --------------


@pytest.mark.parametrize(
    ("model_cls", "blob", "expected"),
    [
        (AzureLike, {"service_principal": {"secret": None}}, "azure-client-secret"),
        (AwsLike, {"credentials": {"access_key_secret": None}}, "aws-secret-access-key"),
        (ProxmoxLike, {"token_secret": None}, "proxmox-token"),
        (GithubLike, {"token": None}, "git-token-prod"),
    ],
)
def test_an_explicit_null_diverges_from_three_shipped_validators(
    model_cls: type[AgwModel],
    blob: dict[str, object],
    expected: str,
) -> None:
    """An explicitly null reference field resolves to the default here.

    This is a DIVERGENCE, not parity, and the only one: azure, aws, and
    proxmox all read the key with ``config.get(key, DEFAULT)``, so an
    explicit ``null`` reaches their guard as a malformed value and omits
    the edge, and azure's validator additionally raises telling the
    operator to omit the key instead. Git-credential's
    ``token_dependency`` already treats absent and null alike, which is
    the behavior adopted here.

    Taken deliberately: extraction and validation have to derive the same
    name or the graph edge and the validated instance would disagree, and
    treating null as "I did not set this" is the reading an operator
    means. It is an operator-visible change and belongs in the step 2.3
    break note; it is a test rather than only a doc line so the decision
    is one someone can find and overturn.
    """
    assert names(extract_references(model_cls, blob, OWNER)) == [expected]


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
