"""Behavioral contract for schema-directed raw-value merging."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import Discriminator, Field, FiniteFloat, field_validator
from pydantic.types import AllowInfNan

from agentworks.schema import (
    AgwModel,
    AgwRootModel,
    MergeStrategy,
    ScalarShorthand,
    StructuralUnion,
    UnionScalarShorthand,
    merge_contract_error,
    merge_model,
)
from agentworks.value_provenance import LayerContribution, LayerContributionKind


class _Nested(AgwModel):
    count: int
    labels: list[str] = Field(default_factory=list)


class _DefaultMerge(AgwModel):
    nested: _Nested
    values: dict[str, int] = Field(default_factory=dict)
    replacement: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)


def test_objects_recurse_lists_dedupe_and_marked_lists_replace() -> None:
    previous = {
        "nested": {"count": 1, "labels": ["first"]},
        "values": {"left": 1},
        "replacement": ["old"],
    }
    incoming = {
        "nested": {"count": 2, "labels": ["first", "second"]},
        "values": {"right": 2},
        "replacement": [],
    }

    value, operations = merge_model(_DefaultMerge, previous, incoming)

    assert value == {
        "nested": {"count": 2, "labels": ["first", "second"]},
        "values": {"left": 1, "right": 2},
        "replacement": [],
    }
    assert LayerContribution.contribution("nested", "labels", 0) in operations
    assert LayerContribution.replacement("nested", "labels", 1) in operations
    assert LayerContribution.reset_prefix("replacement") in operations
    assert LayerContribution.replacement("replacement") in operations
    assert previous["nested"] == {"count": 1, "labels": ["first"]}
    assert incoming["nested"] == {"count": 2, "labels": ["first", "second"]}


class _ReplaceBlock(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE

    retained_if_merged: str | None = None


class _BlockHolder(AgwModel):
    block: _ReplaceBlock


def test_model_replacement_discards_the_complete_previous_subtree() -> None:
    value, operations = merge_model(
        _BlockHolder,
        {"block": {"retained_if_merged": "old"}},
        {"block": {}},
    )

    assert value == {"block": {}}
    assert operations == (
        LayerContribution.reset_prefix("block"),
        LayerContribution.replacement("block"),
    )


class _ReplaceRootObject(AgwRootModel[dict[str, str]]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


def test_a_direct_root_object_replacement_resets_the_root_node() -> None:
    incoming: dict[str, str] = {}

    value, operations = merge_model(_ReplaceRootObject, {"retained_if_merged": "old"}, incoming)

    assert value is incoming
    assert operations == (
        LayerContribution.reset_prefix(),
        LayerContribution.replacement(),
    )


class _RootPayload(AgwModel):
    left: str | None = None
    right: str | None = None


class _InnerObjectRoot(AgwRootModel[_RootPayload]):
    pass


class _OuterObjectRoot(AgwRootModel[_InnerObjectRoot]):
    pass


def test_nested_root_models_are_transparent_to_runtime_object_merging() -> None:
    value, _operations = merge_model(
        _OuterObjectRoot,
        {"left": "old"},
        {"right": "new"},
    )

    assert value == {"left": "old", "right": "new"}


class _ReplaceArm(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE

    kind: Literal["replace"]
    left: str | None = None
    right: str | None = None


class _OtherArm(AgwModel):
    kind: Literal["other"]
    value: str


class _UnionHolder(AgwModel):
    ordinary: Annotated[_ReplaceArm | _OtherArm, Discriminator("kind")]
    merged: Annotated[
        _ReplaceArm | _OtherArm,
        Discriminator("kind"),
        MergeStrategy.MERGE,
    ]
    replaced: Annotated[
        _ReplaceArm | _OtherArm,
        Discriminator("kind"),
        MergeStrategy.REPLACE,
    ]


_ROOT_UNION = Annotated[_ReplaceArm | _OtherArm, Discriminator("kind")]


class _MergingUnionRoot(AgwRootModel[_ROOT_UNION]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


class _ReplacingUnionRoot(AgwRootModel[_ROOT_UNION]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


class _RepeatedMergingUnionRoot(AgwRootModel[_MergingUnionRoot]):
    pass


class _RepeatedReplacingUnionRoot(AgwRootModel[_ReplacingUnionRoot]):
    pass


class _RootStrategyOverrides(AgwModel):
    merge_replacing_root: Annotated[_ReplacingUnionRoot, MergeStrategy.MERGE]
    replace_merging_root: Annotated[_MergingUnionRoot, MergeStrategy.REPLACE]


class _MixedScalarObjectRoot(AgwRootModel[_RootPayload | str]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


class _ScalarCapableRootArm(AgwModel):
    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    kind: Literal["scalar-capable"]
    value: str


class _DirectScalarShorthandModel(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE
    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str


class _ScalarCapableTaggedRoot(
    AgwRootModel[
        Annotated[
            _ScalarCapableRootArm,
            UnionScalarShorthand(discriminator="kind", arm=_ScalarCapableRootArm),
        ]
    ]
):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


def test_union_precedence_allows_only_a_same_arm_explicit_merge() -> None:
    previous = {
        "ordinary": {"kind": "replace", "left": "old"},
        "merged": {"kind": "replace", "left": "old"},
        "replaced": {"kind": "replace", "left": "old"},
    }
    incoming = {
        "ordinary": {"kind": "replace", "right": "new"},
        "merged": {"kind": "replace", "right": "new"},
        "replaced": {"not-even-a-tag": object()},
    }

    value, _operations = merge_model(_UnionHolder, previous, incoming)

    assert isinstance(value, dict)
    assert value["ordinary"] == {"kind": "replace", "right": "new"}
    assert value["merged"] == {"kind": "replace", "left": "old", "right": "new"}
    assert value["replaced"] is incoming["replaced"]


def test_union_root_merge_overrides_a_same_selected_arms_replacement() -> None:
    previous = {"kind": "replace", "left": "old"}
    incoming = {"kind": "replace", "right": "new"}

    direct, _ = merge_model(_MergingUnionRoot, previous, incoming)
    repeated, _ = merge_model(_RepeatedMergingUnionRoot, previous, incoming)

    expected = {"kind": "replace", "left": "old", "right": "new"}
    assert direct == expected
    assert repeated == expected
    assert merge_contract_error(_MergingUnionRoot) is None
    assert merge_contract_error(_RepeatedMergingUnionRoot) is None


def test_union_root_replace_wins_before_same_arm_dispatch() -> None:
    previous = {"kind": "other", "value": "old", "future": "discarded"}
    incoming = {"kind": "other", "value": "new"}

    direct, direct_operations = merge_model(_ReplacingUnionRoot, previous, incoming)
    repeated, repeated_operations = merge_model(_RepeatedReplacingUnionRoot, previous, incoming)

    assert direct is incoming
    assert repeated is incoming
    assert (
        direct_operations
        == repeated_operations
        == (
            LayerContribution.reset_prefix(),
            LayerContribution.replacement(),
        )
    )


def test_a_containing_field_strategy_precedes_a_union_root_strategy() -> None:
    previous = {
        "merge_replacing_root": {"kind": "replace", "left": "old"},
        "replace_merging_root": {"kind": "replace", "left": "old"},
    }
    incoming = {
        "merge_replacing_root": {"kind": "replace", "right": "new"},
        "replace_merging_root": {"kind": "replace", "right": "new"},
    }

    value, _operations = merge_model(_RootStrategyOverrides, previous, incoming)

    assert value == {
        "merge_replacing_root": {"kind": "replace", "left": "old", "right": "new"},
        "replace_merging_root": {"kind": "replace", "right": "new"},
    }


def test_different_or_unselectable_union_arms_replace_whole_values() -> None:
    previous = {
        "ordinary": {"kind": "replace", "left": "old"},
        "merged": {"kind": "replace", "left": "old"},
        "replaced": {"kind": "replace"},
    }
    incoming = {
        "ordinary": {"kind": "other", "value": "new"},
        "merged": {"unknown": "raw"},
        "replaced": {"kind": "other", "value": "new"},
    }

    value, operations = merge_model(_UnionHolder, previous, incoming)

    assert value == incoming
    assert LayerContribution.reset_prefix("ordinary") in operations
    assert LayerContribution.reset_prefix("merged") in operations


def test_union_root_merge_preserves_the_same_arm_gate() -> None:
    incoming = {"kind": "other", "value": "new"}

    value, operations = merge_model(
        _MergingUnionRoot,
        {"kind": "replace", "left": "old"},
        incoming,
    )

    assert value is incoming
    assert operations == (
        LayerContribution.reset_prefix(),
        LayerContribution.replacement(),
    )


class _CollapsedArm(AgwModel):
    kind: Literal["only"]
    left: str | None = None
    right: str | None = None


class _CollapsedDispatchHolder(AgwModel):
    value: Annotated[_CollapsedArm, Discriminator("kind")]


def test_a_collapsed_discriminated_arm_still_requires_its_tag_before_merging() -> None:
    incoming = {"value": {"right": "new"}}

    value, operations = merge_model(
        _CollapsedDispatchHolder,
        {"value": {"kind": "only", "left": "old"}},
        incoming,
    )

    assert value == incoming
    assert LayerContribution.reset_prefix("value") in operations


def test_a_collapsed_discriminated_arm_merges_when_both_tags_select_it() -> None:
    value, _operations = merge_model(
        _CollapsedDispatchHolder,
        {"value": {"kind": "only", "left": "old"}},
        {"value": {"kind": "only", "right": "new"}},
    )

    assert value == {"value": {"kind": "only", "left": "old", "right": "new"}}


class _StructuralOne(AgwModel):
    one: str


class _StructuralTwo(AgwModel):
    two: str


class _StructuralHolder(AgwModel):
    value: Annotated[
        _StructuralOne | _StructuralTwo,
        StructuralUnion(),
        MergeStrategy.MERGE,
    ]


class _StructuralUnionRoot(AgwRootModel[Annotated[_StructuralOne | _StructuralTwo, StructuralUnion()]]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


class _MixedStructuralRoot(AgwRootModel[Annotated[_StructuralOne | str, StructuralUnion()]]):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


class _StructuralShorthandArm(AgwModel):
    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="short_value")

    short_value: str


class _StructuralObjectArm(AgwModel):
    object_value: str


class _StructuralShorthandRoot(
    AgwRootModel[
        Annotated[
            _StructuralShorthandArm | _StructuralObjectArm,
            StructuralUnion(),
        ]
    ]
):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE


def test_structural_union_selection_obeys_the_same_arm_gate() -> None:
    same, _ = merge_model(
        _StructuralHolder,
        {"value": {"one": "old"}},
        {"value": {"one": "new"}},
    )
    changed, _ = merge_model(
        _StructuralHolder,
        {"value": {"one": "old"}},
        {"value": {"two": "new"}},
    )

    assert same == {"value": {"one": "new"}}
    assert changed == {"value": {"two": "new"}}


def test_model_policy_accepts_only_complete_object_union_root_domains() -> None:
    assert merge_contract_error(_StructuralUnionRoot) is None
    assert merge_contract_error(_DirectScalarShorthandModel) is not None
    assert merge_contract_error(_MixedScalarObjectRoot) is not None
    assert merge_contract_error(_ScalarCapableTaggedRoot) is not None
    assert merge_contract_error(_MixedStructuralRoot) is not None
    assert merge_contract_error(_StructuralShorthandRoot) is not None


class _UnknownKeyHolder(AgwModel):
    known: dict[str, int] = Field(default_factory=dict)


def test_unknown_schema_conflicts_replace_without_runtime_shape_merging() -> None:
    value, operations = merge_model(
        _UnknownKeyHolder,
        {"known": {}, "future": {"left": 1}},
        {"future": {"right": 2}},
    )

    assert value == {"known": {}, "future": {"right": 2}}
    assert LayerContribution.reset_prefix("future") in operations


def test_wrong_shapes_and_non_string_merge_keys_replace_without_interpretation() -> None:
    non_string = {1: "later"}
    wrong_shape, _ = merge_model(_DefaultMerge, {"nested": {}}, ["wrong"])
    non_string_result, operations = merge_model(
        _UnknownKeyHolder,
        {"known": {"old": 1}},
        {"known": non_string},
    )

    assert wrong_shape == ["wrong"]
    assert non_string_result == {"known": non_string}
    assert operations == (
        LayerContribution.reset_prefix("known"),
        LayerContribution.replacement("known"),
    )


class _EqualityBomb:
    def __eq__(self, other: object) -> bool:
        raise AssertionError("authored equality must not run")


class _ListHolder(AgwModel):
    values: list[object] = Field(default_factory=list)


def test_append_dedupe_is_type_sensitive_structural_and_total() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    previous = {"values": [True, 1, 1.0, {"a": [1], "b": 2}, float("nan"), _EqualityBomb(), cyclic]}
    incoming = {"values": [True, 1, 1.0, {"b": 2, "a": [1]}, float("nan"), _EqualityBomb(), cyclic]}

    value, operations = merge_model(_ListHolder, previous, incoming)
    assert isinstance(value, dict)
    values = value["values"]

    assert len(values) == 10
    assert [operation.path for operation in operations if operation.kind is LayerContributionKind.CONTRIBUTION] == [
        ("values", 0),
        ("values", 1),
        ("values", 2),
        ("values", 3),
    ]


class _Recursive(AgwModel):
    child: _Recursive | None = None


def test_a_cycle_in_a_recursive_object_terminates_at_the_cycle_boundary() -> None:
    previous: dict[str, object] = {}
    previous["child"] = previous
    incoming: dict[str, object] = {}
    incoming["child"] = incoming

    value, operations = merge_model(_Recursive, previous, incoming)

    assert type(value) is dict
    assert value["child"] is incoming
    assert LayerContribution.reset_prefix("child") in operations


class _BadScalarMerge(AgwModel):
    value: Annotated[str, MergeStrategy.MERGE]


class _BadListElement(AgwModel):
    values: list[Annotated[str, MergeStrategy.REPLACE]]


class _BadMapKey(AgwModel):
    values: dict[Annotated[str, MergeStrategy.REPLACE], str]


class _BadUnionArm(AgwModel):
    value: Annotated[str, MergeStrategy.REPLACE] | int


class _UnsafeList(AgwModel):
    values: list[object]


class _SafeFloatList(AgwModel):
    values: list[FiniteFloat | None]


class _NoneList(AgwModel):
    values: list[None]


class _UnsafeFloatList(AgwModel):
    values: list[Annotated[float, AllowInfNan(False), AllowInfNan(True)]]


class _ReplacementEscape(AgwModel):
    values: Annotated[list[dict[int, object]], MergeStrategy.REPLACE]


class _BadMergedMap(AgwModel):
    values: dict[int, str]


class _DuplicateStrategy(AgwModel):
    values: Annotated[
        list[str],
        MergeStrategy.APPEND_DEDUPE,
        MergeStrategy.REPLACE,
    ]


class _BadModelStrategy(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.APPEND_DEDUPE

    value: str


class _NormallyReplacedUnsafeBlock(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE

    values: list[object]


class _DefaultReplacementBoundary(AgwModel):
    child: _NormallyReplacedUnsafeBlock


class _MergeOverrideReadsChildren(AgwModel):
    child: Annotated[_NormallyReplacedUnsafeBlock, MergeStrategy.MERGE]


class _InnerUnsafeRoot(AgwRootModel[_NormallyReplacedUnsafeBlock]):
    pass


class _OuterUnsafeRoot(AgwRootModel[_InnerUnsafeRoot]):
    pass


class _MergeOverrideThroughRoots(AgwModel):
    child: Annotated[_OuterUnsafeRoot, MergeStrategy.MERGE]


def test_registration_refuses_invalid_or_unsafe_strategy_contracts() -> None:
    assert merge_contract_error(_BadScalarMerge) is not None
    assert merge_contract_error(_BadListElement) is not None
    assert merge_contract_error(_BadMapKey) is not None
    assert merge_contract_error(_BadUnionArm) is not None
    assert merge_contract_error(_UnsafeList) is not None
    assert merge_contract_error(_UnsafeFloatList) is not None
    assert merge_contract_error(_BadMergedMap) is not None
    assert merge_contract_error(_DuplicateStrategy) is not None
    assert merge_contract_error(_BadModelStrategy) is not None


def test_registration_accepts_the_finite_carrier_and_replacement_escape() -> None:
    assert merge_contract_error(_SafeFloatList) is None
    assert merge_contract_error(_NoneList) is None
    assert merge_contract_error(_ReplacementEscape) is None
    assert merge_contract_error(_DefaultReplacementBoundary) is None


def test_a_field_merge_override_validates_children_hidden_by_model_replacement() -> None:
    assert merge_contract_error(_MergeOverrideReadsChildren) is not None
    assert merge_contract_error(_MergeOverrideThroughRoots) is not None


class _UntypedBelowReplacement(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE

    values: list
    mapping: dict


class _ActiveUntypedContainers(AgwModel):
    values: list
    mapping: dict


def test_untyped_containers_are_safe_only_below_an_effective_replacement() -> None:
    assert merge_contract_error(_UntypedBelowReplacement) is None
    assert merge_contract_error(_ActiveUntypedContainers) is not None


def test_mapping_shape_classification_terminates_on_root_model_cycles() -> None:
    class RootCycle(AgwRootModel[Any]):
        merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.MERGE

    RootCycle.model_fields["root"].annotation = RootCycle

    assert merge_contract_error(RootCycle) is not None


class _AliasedBelowReplacement(AgwModel):
    value: str = Field(validation_alias="accepted-name")


class _AliasedReplacementHolder(AgwModel):
    child: Annotated[_AliasedBelowReplacement, MergeStrategy.REPLACE]


def test_validation_aliases_are_refused_below_replacement_boundaries() -> None:
    assert merge_contract_error(_AliasedReplacementHolder) is not None


class _SerializationAliasHolder(AgwModel):
    value: str = Field(serialization_alias="rendered-name")


def test_serialization_only_aliases_remain_valid() -> None:
    assert merge_contract_error(_SerializationAliasHolder) is None


class _ValidatorWidenedList(AgwModel):
    values: list[str]

    @field_validator("values", mode="before")
    @classmethod
    def _accept_bombs(cls, value: object) -> object:
        if type(value) is list:
            return ["accepted" if isinstance(item, _EqualityBomb) else item for item in value]
        return value


def test_validator_widening_does_not_run_or_enlarge_runtime_equality() -> None:
    first = _EqualityBomb()
    second = _EqualityBomb()
    assert merge_contract_error(_ValidatorWidenedList) is None

    value, operations = merge_model(
        _ValidatorWidenedList,
        {"values": [first]},
        {"values": [second]},
    )

    assert isinstance(value, dict)
    assert len(value["values"]) == 2
    assert value["values"][0] is first
    assert value["values"][1] is second
    assert LayerContribution.replacement("values", 1) in operations
