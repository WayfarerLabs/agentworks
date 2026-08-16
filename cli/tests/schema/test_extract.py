"""Tests for ``extract_references``: the walk, parity, and FR18.

Totality lives in its own file (``test_extract_totality.py``) because it
is a property over generated inputs rather than a statement about one
shape.

Every case runs through :func:`_extracted`, which is extraction as
production runs it: over the blob the boundary fill has already rendered
templated defaults into. The fill is the identity for a blob with
nothing to fill, so a case without a template reads as bare extraction,
and a case WITH one pins the pipeline the graph is actually built by.
"""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Discriminator, Field

from agentworks.schema import (
    AgwModel,
    AgwRootModel,
    RefOwner,
    ResourceRef,
    SecretRef,
    extract_references,
    filled_defaults,
)
from agentworks.schema.reference import ConfigReference, RefRelationship

from ._fixture_models import (
    AbstractCollectionLike,
    AwsLike,
    AzureLike,
    CatalogLike,
    DefaultedCollectionSite,
    DiamondLike,
    EveryArmMarkedCollectionSite,
    EveryArmMarkedSite,
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


def _extracted(model_cls: type[BaseModel], blob: object, owner: RefOwner = OWNER) -> tuple[ConfigReference, ...]:
    """The production pipeline: fill, then extract the filled blob. See
    the module docstring."""
    return extract_references(model_cls, filled_defaults(model_cls, blob, owner))


# --- marked scalars ---------------------------------------------------


def test_an_operator_written_name_wins() -> None:
    refs = _extracted(GithubLike, {"token": "custom-token"})
    assert refs == (ConfigReference(kind="secret", name="custom-token", usage="the auth token"),)


def test_an_omitted_field_falls_back_to_the_owner_template() -> None:
    assert names(_extracted(GithubLike, {})) == ["git-token-prod"]


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
    assert _extracted(GithubLike, {"token": value}) == ()


def test_a_marker_with_no_template_contributes_nothing_when_omitted() -> None:
    class NoDefault(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret")] | None = None

    assert _extracted(NoDefault, {}) == ()


# --- declared defaults ------------------------------------------------
#
# An absent field with a declared default is read as if the operator had
# written the default's value, because that is what validation does with
# it. The block and collection shapes are pinned by the completeness
# suite against the validated object; what belongs here is the scalar
# precedence, which that oracle's subset assertion cannot see, and the
# two default spellings that are NOT static.


def test_an_absent_marked_scalar_reads_as_its_declared_default() -> None:
    class Defaulted(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret")] = "declared-default"

    assert names(_extracted(Defaulted, {})) == ["declared-default"]
    assert names(_extracted(Defaulted, {"secret": "written"})) == ["written"]


def test_the_owner_template_outranks_a_declared_default() -> None:
    # The boundary fill answers an absent templated field before pydantic
    # could reach for the declared default, so the validated config
    # carries the rendered template and the edge must name the same secret.
    class Both(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret", default_template="tpl-{owner_name}")] = "declared-default"

    assert names(_extracted(Both, {})) == ["tpl-prod"]


def test_a_written_null_does_not_fall_back_to_the_declared_default() -> None:
    # Pydantic reserves a default for the ABSENT key: an explicit null
    # validates as null (or not at all), never as the default, so an edge
    # for the default here would name a secret the config does not carry.
    class Defaulted(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret")] | None = "declared-default"

    assert _extracted(Defaulted, {"secret": None}) == ()


def test_a_validated_data_factory_is_not_a_static_default() -> None:
    # A factory taking validated data computes the default FROM the
    # neighboring fields, so its value is not a property of the class and
    # its edges deliberately cannot be pre-computed; the honest answer is
    # no edge, stated in ``_absent_defaults``'s docstring rather than
    # papered over.
    class Dynamic(AgwModel):
        base: str = "b"
        secret: Annotated[str, SecretRef(usage="a secret")] = Field(
            default_factory=lambda data: f"{data['base']}-secret"
        )

    assert _extracted(Dynamic, {"base": "x"}) == ()


def test_a_raising_factory_contributes_nothing_rather_than_raising() -> None:
    # The one guarded region: author code, not the walk. Extraction is
    # total by contract, and a config leaning on this factory cannot
    # validate either.
    def boom() -> str:
        raise RuntimeError("author bug")

    class Broken(AgwModel):
        secret: Annotated[str, SecretRef(usage="a secret")] = Field(default_factory=boom)

    assert _extracted(Broken, {}) == ()


def test_an_unmarked_model_implies_nothing_at_any_depth() -> None:
    blob = {"name": "a", "port": 22, "nested": {"name": "lima", "vm_host": "h"}}
    assert _extracted(UnmarkedLike, blob) == ()


# --- nested models ----------------------------------------------------


def test_a_nested_block_is_walked() -> None:
    blob = {"region": "eastus", "service_principal": {"client_id": "c", "secret": "sp-secret"}}
    assert names(_extracted(AzureLike, blob)) == ["sp-secret"]


def test_a_nested_block_that_is_not_a_table_contributes_nothing() -> None:
    assert _extracted(AzureLike, {"service_principal": "nope"}) == ()


def test_two_sibling_fields_of_one_nested_model_both_extract() -> None:
    # The path-scoped guard's regression test, kept as its own named
    # case because the failure it guards against is silent: an
    # accumulating visited set walks ``primary`` and skips ``fallback``,
    # building a graph that is missing an edge nothing reports.
    blob = {"primary": {"secret": "primary-secret"}, "fallback": {"secret": "fallback-secret"}}
    assert names(_extracted(DiamondLike, blob)) == ["primary-secret", "fallback-secret"]


def test_a_recursive_model_walks_finite_data_all_the_way_down() -> None:
    """Recursion in the TYPE is not a cycle in the DATA.

    This blob nests three levels and ends. Pydantic validates every one of
    them, so a walker that stopped at the first repeated type would leave
    two secrets out of the dependency graph with nothing reported: the
    resource would be missing at runtime, in a run that finalized clean.
    """
    blob = {"secret": "top", "child": {"secret": "nested", "child": {"secret": "deeper"}}}
    assert names(_extracted(SelfReferential, blob)) == ["top", "nested", "deeper"]


def test_data_reachable_from_itself_terminates() -> None:
    """The one input that cannot terminate on its own, which a YAML anchor
    can write (``child: *self``). The edges it does name are still
    extracted; only the second visit to the same value is cut."""
    blob: dict[str, object] = {"secret": "top"}
    blob["child"] = blob

    assert names(_extracted(SelfReferential, blob)) == ["top"]


def test_a_data_cycle_through_two_values_terminates() -> None:
    """A cycle the guard cannot see by looking at one value: two tables
    that name each other."""
    outer: dict[str, object] = {"secret": "outer"}
    inner: dict[str, object] = {"secret": "inner", "child": outer}
    outer["child"] = inner

    assert names(_extracted(SelfReferential, outer)) == ["outer", "inner"]


def test_deeply_nested_finite_data_neither_truncates_nor_overflows() -> None:
    """Depth is the operator's to choose, and the totality contract has no
    exception for "too deep". A recursive walk raises ``RecursionError``
    here well before the bottom, which is a raised exception from a
    function documented never to raise."""
    depth = 5_000
    blob: dict[str, object] = {"secret": "bottom"}
    for _ in range(depth):
        blob = {"child": blob}

    assert names(_extracted(SelfReferential, blob)) == ["bottom"]


# --- marked lists -----------------------------------------------------


def test_a_marked_list_emits_one_edge_per_element() -> None:
    refs = _extracted(TemplateLike, {"inherits": ["base", "shared"]})
    assert names(refs) == ["base", "shared"]
    assert {ref.kind for ref in refs} == {"vm-template"}


def test_a_marked_list_carries_the_declared_relationship() -> None:
    refs = _extracted(TemplateLike, {"inherits": ["base"], "image": "ubuntu"})
    assert [ref.relationship for ref in refs] == [RefRelationship.INHERITS, RefRelationship.USES]


def test_a_marked_list_skips_the_elements_that_name_nothing() -> None:
    assert names(_extracted(TemplateLike, {"inherits": ["base", 8, "", None, "other"]})) == [
        "base",
        "other",
    ]


def test_an_omitted_list_has_no_default_identity() -> None:
    # No owner template applies at an element, and this model's declared
    # default is EMPTY, so absence contributes nothing. An omitted list
    # whose declared default holds names is the next test.
    assert _extracted(TemplateLike, {}) == ()


def test_an_omitted_list_contributes_what_its_declared_default_holds() -> None:
    # Validation answers the absent field with the default's elements, so
    # the names they carry are names the validated config really holds.
    # Outside the completeness oracle like every element marker, so the
    # expectation is spelled here.
    assert names(_extracted(DefaultedCollectionSite, {})) == ["default-collection-token"]


# --- collections of models --------------------------------------------


def test_every_element_of_a_list_of_models_is_walked() -> None:
    blob = {"accounts": [{"secret": "first"}, {"secret": "second"}]}
    assert names(_extracted(CatalogLike, blob)) == ["first", "second"]


def test_every_value_of_a_table_of_models_is_walked() -> None:
    blob = {"accounts_by_name": {"prod": {"secret": "prod-secret"}, "dev": {"secret": "dev-secret"}}}
    assert names(_extracted(CatalogLike, blob)) == ["prod-secret", "dev-secret"]


def test_every_value_of_a_table_of_names_becomes_an_edge() -> None:
    blob = {"extra_secrets": {"one": "first", "two": "second", "bad": 8}}
    assert names(_extracted(CatalogLike, blob)) == ["first", "second"]


def test_every_marked_mapping_key_becomes_an_edge_in_authored_order() -> None:
    class MappingKeys(AgwModel):
        lookups: dict[
            Annotated[str, ResourceRef(kind="secret-source", usage="a lookup source")],
            object,
        ]

    references = _extracted(MappingKeys, {"lookups": {"first": False, "second": ["ignored"]}})
    assert [(ref.kind, ref.name, ref.usage) for ref in references] == [
        ("secret-source", "first", "a lookup source"),
        ("secret-source", "second", "a lookup source"),
    ]


def test_a_tuple_of_names_is_a_sequence_like_a_list() -> None:
    assert names(_extracted(CatalogLike, {"templates": ["base", "other"]})) == ["base", "other"]


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
    assert _extracted(CatalogLike, {field: value}) == ()


def test_malformed_elements_are_skipped_and_the_rest_still_extract() -> None:
    blob = {"accounts": [{"secret": "kept"}, "not a table", None, 8, {"secret": ""}, {"secret": "also-kept"}]}
    assert names(_extracted(CatalogLike, blob)) == ["kept", "also-kept"]


def test_a_recursive_model_walks_a_finite_collection_all_the_way_down() -> None:
    blob = {"secret": "top", "children": [{"secret": "nested", "children": [{"secret": "deeper"}]}]}
    assert names(_extracted(SelfReferential, blob)) == ["top", "nested", "deeper"]


def test_a_data_cycle_through_a_collection_terminates() -> None:
    """The guard has to hold on both routes into a model: the field and
    the collection element."""
    blob: dict[str, object] = {"secret": "top"}
    blob["children"] = [blob]

    assert names(_extracted(SelfReferential, blob)) == ["top"]


def test_a_collection_spelled_as_an_abc_is_still_a_collection() -> None:
    """``Sequence[X]`` is ``list[X]`` to everything downstream.

    Pydantic accepts both, and the raw value is a list either way, so a
    classifier that knew only the concrete spelling would read every field
    here as an ordinary scalar and emit no edge at all. Silent, and a
    gating bypass rather than a cosmetic gap: the graph is built from
    these edges before anything validates.
    """
    blob = {
        "sequence_tokens": ["in-a-sequence"],
        "mutable_sequence_tokens": ["in-a-mutable-sequence"],
        "mapping_tokens": {"k": "in-a-mapping"},
        "mutable_mapping_tokens": {"k": "in-a-mutable-mapping"},
        "set_tokens": ["in-a-set"],
        "blocks": [{"token": "in-a-block"}],
    }
    assert names(_extracted(AbstractCollectionLike, blob)) == [
        "in-a-sequence",
        "in-a-mutable-sequence",
        "in-a-mapping",
        "in-a-mutable-mapping",
        "in-a-set",
        "in-a-block",
    ]


def test_an_abstract_mapping_addresses_its_values_not_its_keys() -> None:
    """``Mapping[K, V]`` has two type arguments and the marker is on the
    second. Classifying it as a sequence would read the KEY type, so a
    marked value would go unread while an unmarked key looked marked."""
    blob = {"mapping_tokens": {"a-key-that-is-not-a-secret": "the-value-that-is"}}
    assert names(_extracted(AbstractCollectionLike, blob)) == ["the-value-that-is"]


# --- discriminated unions ---------------------------------------------


def test_the_arm_the_raw_tag_names_is_walked() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(_extracted(SiteLike, blob)) == ["lab-token"]


def test_an_arm_that_names_nothing_contributes_nothing() -> None:
    assert _extracted(SiteLike, {"platform": {"name": "lima", "vm_host": "h"}}) == ()


def test_an_arms_own_templated_default_applies() -> None:
    assert names(_extracted(SiteLike, {"platform": {"name": "proxmox"}})) == ["proxmox-token"]


@pytest.mark.parametrize("tag", [8, "unregistered"])
def test_a_tag_naming_no_arm_contributes_nothing(tag: object) -> None:
    # ``_arm_block``'s two rejections: a tag that is not a name at all,
    # and a name no arm answers to.
    assert _extracted(SiteLike, {"platform": {"name": tag}}) == ()


@pytest.mark.parametrize("model_cls", [SiteLike, FieldDiscriminatedSite, OptionalUnionSite])
def test_every_legal_union_spelling_walks_the_same_way(model_cls: type[AgwModel]) -> None:
    # Pydantic validates all three identically, so a lookup that missed
    # one would build a silently wrong graph rather than fail.
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert names(_extracted(model_cls, blob)) == ["lab-token"]


def test_an_arm_answering_to_two_tags_is_reachable_by_both() -> None:
    for tag in ("aws-ec2", "ec2"):
        blob = {"platform": {"name": tag, "access_key_secret": "key"}}
        assert names(_extracted(RenamedArmSite, blob)) == ["key"], tag


def test_a_union_tagged_by_a_non_name_has_no_addressable_arm() -> None:
    # A documented boundary: every discriminator in this framework is a
    # capability or kind name.
    blob = {"thing": {"version": 1, "token_secret": "x"}}
    assert _extracted(NumericallyTaggedSite, blob) == ()


def test_a_marker_inside_a_multi_arm_union_is_still_found() -> None:
    assert names(_extracted(MultiArmMarked, {"secret": "named"})) == ["named"]
    assert names(_extracted(MultiArmMarked, {})) == ["multi-arm-secret"]
    assert _extracted(MultiArmMarked, {"secret": 8}) == ()


def test_a_union_with_no_discriminator_has_no_addressable_arm() -> None:
    blob = {"platform": {"name": "proxmox", "token_secret": "lab-token"}}
    assert _extracted(UndiscriminatedSite, blob) == ()


# --- a block that names no arm selects none of them --------------------
#
# The rule is one equality (``arm.tag == tag``) and the whole content of
# these tests is that nothing weakens it into a fallback. A blob that
# addresses no arm must contribute NOTHING, matching the under-report the
# ambiguous-union case already chose: extraction that invented the
# first-registered implementation's edges would put a Resource the
# operator never wrote into the dependency graph, and finalize would
# refuse a config over it.
#
# Asserted against a union in which EVERY arm names a Resource, so the
# claim is "no arm was selected" rather than "not that particular arm";
# see the fixture for why arm order cannot carry that weight.

#: Every way a raw block can fail to name an arm: the tag key absent,
#: written empty, written null, written as a name no arm answers to, and
#: written as something that is not a name at all.
_NAMES_NO_ARM: tuple[object, ...] = (
    {},
    {"name": None},
    {"name": ""},
    {"name": "unregistered"},
    {"name": 8},
    {"name": False},
    {"name": ["first-arm"]},
    {"name": {"first-arm": True}},
)


@pytest.mark.parametrize("block", _NAMES_NO_ARM, ids=range(len(_NAMES_NO_ARM)))
def test_a_block_naming_no_arm_contributes_nothing(block: object) -> None:
    assert _extracted(EveryArmMarkedSite, {"platform": block}) == ()


@pytest.mark.parametrize("block", _NAMES_NO_ARM, ids=range(len(_NAMES_NO_ARM)))
def test_a_collection_element_naming_no_arm_contributes_nothing(block: object) -> None:
    # The same question of the other walker: both select arms through one
    # call, and a fallback added to it would be reachable from either.
    blob = {"platforms": [block], "platforms_by_name": {"x": block}}
    assert _extracted(EveryArmMarkedCollectionSite, blob) == ()


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("first-arm", "first-arm-token"), ("second-arm", "second-arm-token")],
)
def test_selecting_any_arm_would_show(tag: str, expected: str) -> None:
    """Non-vacuity for the two tests above, which is the whole reason
    those fixtures exist.

    They assert an empty result, so they are worth nothing unless
    selecting an arm from blobs of that shape WOULD produce one. Every arm
    is checked, not just one, because "no arm was selected" is only
    established if each arm the walker could have fallen back to is one
    whose selection shows.
    """
    assert names(_extracted(EveryArmMarkedSite, {"platform": {"name": tag}})) == [expected]

    blob = {"platforms": [{"name": tag}], "platforms_by_name": {"x": {"name": tag}}}
    assert names(_extracted(EveryArmMarkedCollectionSite, blob)) == [expected, expected]


def test_an_arm_tagged_with_the_empty_string_is_still_addressable() -> None:
    """Why the rule is an equality rather than a truthiness test.

    Pydantic dispatches on ``Literal[""]`` like any other tag, so a guard
    reading "an empty tag names no arm" would drop a real edge, which is
    the silent failure this walker exists to avoid. What makes a block
    address no arm is that no arm answers to what it wrote, not that what
    it wrote looks empty.
    """

    class Nameless(AgwModel):
        name: Literal[""]
        token_secret: Annotated[str, SecretRef(usage="the nameless arm's token")] | None = None

    class Named(AgwModel):
        name: Literal["named"]

    class EmptyTagSite(AgwModel):
        platform: Annotated[Nameless | Named, Discriminator("name")]

    assert names(_extracted(EmptyTagSite, {"platform": {"name": "", "token_secret": "t"}})) == ["t"]
    assert _extracted(EmptyTagSite, {"platform": {"name": "other"}}) == ()


# --- untagged scalar-or-block unions ----------------------------------
#
# No tag addresses these, so the value's own shape does: a table is the
# block, a scalar is one of the scalar members. The last two cases are
# the two edges of that rule, one per direction: a table stops naming the
# block once another member could accept one, and a scalar never names it
# even where the block would read a bare value.


def test_a_union_written_as_a_table_walks_its_block() -> None:
    blob = {"mapping": {"secret": "named-in-the-arm"}}
    assert names(_extracted(ScalarOrBlockLike, blob)) == ["named-in-the-arm"]


def test_a_union_written_as_a_scalar_contributes_nothing() -> None:
    # The other arm entirely, and the operator wrote a name that belongs
    # to the backend rather than to agentworks.
    assert _extracted(ScalarOrBlockLike, {"mapping": "op://vault/item"}) == ()


def test_an_untagged_union_held_by_a_collection_is_read_per_element() -> None:
    blob = {
        "mappings": {"a": {"secret": "in-a-table"}, "b": "a scalar element"},
        "mapping_list": ["another scalar", {"secret": "in-a-list"}],
    }
    assert names(_extracted(ScalarOrBlockLike, blob)) == ["in-a-table", "in-a-list"]


def test_a_union_block_reachable_from_itself_terminates() -> None:
    blob: dict[str, object] = {"name": "anchored"}
    blob["child"] = blob

    assert _extracted(SelfReferentialUnion, blob) == ()


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

    assert _extracted(Ambiguous, {"thing": {"api_url": "https://example"}}) == ()


def test_a_scalar_is_not_read_as_a_root_model_arm() -> None:
    """The reason the walk tests the value's shape itself rather than
    leaving it to the block: an ordinary model reads its fields out of a
    mapping and a ROOT model reads the blob directly, so the string below
    would be extracted as the root model's own marked value."""

    class Rooted(AgwRootModel[Annotated[str, SecretRef(usage="a rooted secret")]]):
        """A root model whose root IS the marked scalar."""

    class HoldsRooted(AgwModel):
        thing: str | Rooted | None = None

    assert _extracted(HoldsRooted, {"thing": "a plain string"}) == ()


# --- surfaces that are not mapping-shaped -----------------------------


@pytest.mark.parametrize("blob", [None, "op://vault/item", ["a"]])
def test_a_blob_that_is_not_a_table_contributes_nothing(blob: object) -> None:
    # Refused by SHAPE rather than by truthiness, which is what the
    # non-empty list is here to say: a walk guarded by ``if not blob``
    # would go on to read keys off it.
    assert _extracted(GithubLike, blob) == ()


def test_a_bare_scalar_root_names_nothing() -> None:
    # Every shipped backend mapping: an env var name, an ``op://``
    # reference. Neither is an agentworks Resource.
    assert _extracted(StringRoot, "op://vault/item") == ()


def test_a_model_rooted_root_model_carries_its_roots_references() -> None:
    # The alternative would render the marked field in a generated
    # sample and never emit its edge, which is the silent-missing-edge
    # outcome, reached by a different route.
    assert names(_extracted(MappingRoot, {"token": "t"})) == ["t"]
    assert names(_extracted(MappingRoot, {})) == ["git-token-prod"]
    assert _extracted(MappingRoot, "not a table") == ()


@pytest.mark.parametrize("model_cls", [NeverResolved, ResolvesToUnbuildable])
def test_a_model_that_cannot_be_built_contributes_nothing_rather_than_raising(model_cls: type[AgwModel]) -> None:
    # Two different failures, and the second is the one that used to
    # escape: pydantic's own ``raise_errors=False`` covers an annotation
    # that never resolves, not one that resolves to a type it cannot
    # build a schema for.
    assert _extracted(model_cls, {"secret": "named"}) == ()


# --- parity with the shipped hand-rolled derivations -------------------


# Parity with ``capabilities/git_credential/base.py::token_dependency`` is
# the marked-scalar section at the top of this file: ``GithubLike`` IS the
# git-credential shape, so the three cases that derivation has (a written
# name, an omission falling back to the template, a malformed value naming
# nothing) are already pinned there one apiece.


def test_parity_with_the_azure_service_principal_derivation() -> None:
    # plugins/azure/platform.py::AzureVMPlatform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert _extracted(AzureLike, {"region": "eastus"}, site) == ()
    assert names(_extracted(AzureLike, {"service_principal": {"client_id": "c"}}, site)) == ["azure-client-secret"]
    assert _extracted(AzureLike, {"service_principal": {"secret": ""}}, site) == ()


def test_parity_with_the_aws_credentials_derivation() -> None:
    # plugins/aws/platform.py::AwsEc2Platform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert _extracted(AwsLike, {"region": "us-east-1"}, site) == ()
    assert names(_extracted(AwsLike, {"credentials": {"access_key_id": "k"}}, site)) == ["aws-secret-access-key"]
    assert _extracted(AwsLike, {"credentials": {"access_key_secret": 8}}, site) == ()


def test_parity_with_the_proxmox_token_derivation() -> None:
    # plugins/proxmox/platform.py::ProxmoxPlatform.dependencies
    site = RefOwner(kind="vm-site", name="lab")
    assert names(_extracted(ProxmoxLike, {"api_url": "https://pve"}, site)) == ["proxmox-token"]
    assert names(_extracted(ProxmoxLike, {"token_secret": "lab-token"}, site)) == ["lab-token"]
    assert _extracted(ProxmoxLike, {"token_secret": ""}, site) == ()


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
    assert names(_extracted(model_cls, blob)) == [expected]


def test_the_usage_prose_is_carried_verbatim() -> None:
    # It ends up on the target's ReferenceEntry, graph edge detail, and the
    # secret describe "Referenced by:" section, so it is operator-visible.
    assert _extracted(ProxmoxLike, {})[0].usage == "the Proxmox API token"


# --- FR18: extraction is structural (issue #311) -----------------------


def test_renaming_a_marked_field_moves_the_extracted_reference() -> None:
    # The whole point of #311: the derivation lives in the model, so
    # nothing outside the class changes when the field does.
    class Renamed(AgwModel):
        pat: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]

    assert names(_extracted(Renamed, {"pat": "x"})) == ["x"]
    assert names(_extracted(Renamed, {})) == ["git-token-prod"]
    # And the old field name means nothing to the model now.
    assert names(_extracted(Renamed, {"token": "x"})) == ["git-token-prod"]


def test_adding_a_second_marked_field_adds_a_second_reference() -> None:
    class TwoSecrets(AgwModel):
        token: Annotated[str, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
        webhook_secret: Annotated[str, SecretRef(usage="the webhook signing secret")] | None = None

    assert names(_extracted(TwoSecrets, {"webhook_secret": "hook"})) == ["git-token-prod", "hook"]


def test_the_owner_kind_is_available_to_a_template() -> None:
    class KindTemplated(AgwModel):
        secret: Annotated[str, SecretRef(usage="u", default_template="{owner_kind}-{owner_name}")] | None = None

    assert names(_extracted(KindTemplated, {})) == ["git-credential-prod"]
