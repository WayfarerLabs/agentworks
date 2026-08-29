"""Schema-directed raw-value merging and its static model contract.

The merger reads annotations only. It does not validate, construct defaults, or
invoke capability code, so malformed authored values remain available to the
existing final validation boundary.
"""

from __future__ import annotations

import math
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeGuard, Union, cast, get_args, get_origin

from pydantic import BaseModel, RootModel
from pydantic.fields import FieldInfo
from pydantic.types import AllowInfNan

from agentworks.schema._shape import (
    model_fields_of,
    spine_metadata,
    structural_arm_for,
    table_addresses_block,
)
from agentworks.schema.shorthand import scalar_shorthand_of
from agentworks.value_provenance import LayerContribution, ProvenancePath


class MergeStrategy(StrEnum):
    """The complete vocabulary a model can use to direct layer merging."""

    MERGE = "merge"
    APPEND_DEDUPE = "append-dedupe"
    REPLACE = "replace"


@dataclass(frozen=True)
class _Node:
    annotation: object
    field: FieldInfo | None = None
    strategy: MergeStrategy | None = None
    model_strategy: MergeStrategy | None = None


@dataclass(frozen=True)
class _Request:
    node: _Node
    previous: object
    incoming: object
    path: ProvenancePath
    active_pairs: frozenset[tuple[int, int]]


@dataclass(frozen=True)
class _AssembleObject:
    value: dict[str, object]
    keys: tuple[str, ...]
    operations: tuple[LayerContribution, ...]


@dataclass(frozen=True)
class _PreparedObject:
    assembly: _AssembleObject
    requests: tuple[_Request, ...]


def merge_model(
    model: type[BaseModel],
    previous: object,
    incoming: object,
    path: ProvenancePath = (),
) -> tuple[object, tuple[LayerContribution, ...]]:
    """Merge two raw values according to ``model`` without mutating either.

    The walk is iterative so a deeply nested or cyclic value from the open
    capability boundary cannot overflow Python's call stack before typed
    validation has a chance to report it.
    """
    work: list[_Request | _AssembleObject] = [_Request(_Node(model), previous, incoming, path, frozenset())]
    results: list[tuple[object, tuple[LayerContribution, ...]]] = []

    while work:
        frame = work.pop()
        if isinstance(frame, _AssembleObject):
            children = results[-len(frame.keys) :] if frame.keys else []
            if frame.keys:
                del results[-len(frame.keys) :]
            value = frame.value
            operations = list(frame.operations)
            for key, (child, child_operations) in zip(frame.keys, children, strict=True):
                value[key] = child
                operations.extend(child_operations)
            results.append((value, tuple(operations)))
            continue

        node = _root_node(frame.node)
        base = _base_annotation(node.annotation)
        if _is_union(base) or _has_discriminated_dispatch(node):
            result = _merge_union(frame, node)
            if isinstance(result, _Request):
                work.append(result)
            else:
                results.append(result)
            continue

        strategy = _effective_strategy(node, base)
        if strategy is MergeStrategy.REPLACE:
            results.append(_replacement(frame.incoming, frame.path))
            continue
        if strategy is MergeStrategy.APPEND_DEDUPE:
            results.append(_merge_list(frame.previous, frame.incoming, frame.path))
            continue
        if strategy is not MergeStrategy.MERGE:
            results.append(_replacement(frame.incoming, frame.path))
            continue

        prepared = _prepare_object(frame, node, base)
        if not isinstance(prepared, _PreparedObject):
            results.append(prepared)
            continue
        work.append(prepared.assembly)
        work.extend(reversed(prepared.requests))

    if len(results) != 1:
        raise AssertionError("schema merge left an inconsistent internal result stack")
    return results[0]


def _merge_union(
    frame: _Request,
    node: _Node,
) -> _Request | tuple[object, tuple[LayerContribution, ...]]:
    strategy = node.strategy or node.model_strategy
    if strategy is MergeStrategy.REPLACE:
        return _replacement(frame.incoming, frame.path)
    previous_arm = _selected_model(node, frame.previous)
    incoming_arm = _selected_model(node, frame.incoming)
    if previous_arm is None or incoming_arm is not previous_arm:
        return _replacement(frame.incoming, frame.path)
    override = MergeStrategy.MERGE if strategy is MergeStrategy.MERGE else None
    return _Request(
        _Node(previous_arm, strategy=override),
        frame.previous,
        frame.incoming,
        frame.path,
        frame.active_pairs,
    )


def _prepare_object(
    frame: _Request,
    node: _Node,
    base: object,
) -> tuple[object, tuple[LayerContribution, ...]] | _PreparedObject:
    if type(frame.previous) is not dict or type(frame.incoming) is not dict:
        return _replacement(frame.incoming, frame.path)
    if not _has_exact_string_keys(frame.previous) or not _has_exact_string_keys(frame.incoming):
        return _replacement(frame.incoming, frame.path)
    pair = (id(frame.previous), id(frame.incoming))
    if pair in frame.active_pairs:
        return _replacement(frame.incoming, frame.path)
    child_for = _object_children(base)
    if child_for is None:
        return _replacement(frame.incoming, frame.path)

    active = frame.active_pairs | {pair}
    value: dict[str, object] = dict(frame.previous)
    operations: list[LayerContribution] = []
    requests: list[_Request] = []
    request_keys: list[str] = []
    for key, incoming_child in frame.incoming.items():
        child_path = (*frame.path, key)
        if key not in frame.previous:
            value[key] = incoming_child
            operations.extend(_replacement_operations(child_path))
            continue
        child_node = child_for(key)
        if child_node is None:
            value[key] = incoming_child
            operations.extend(_replacement_operations(child_path))
            continue
        requests.append(
            _Request(
                child_node,
                frame.previous[key],
                incoming_child,
                child_path,
                frozenset(active),
            )
        )
        request_keys.append(key)
    return _PreparedObject(
        _AssembleObject(value, tuple(request_keys), tuple(operations)),
        tuple(requests),
    )


def _object_children(base: object) -> Callable[[str], _Node | None] | None:
    if _is_model(base) and not issubclass(base, RootModel):
        fields = model_fields_of(base)
        if fields is None:
            return None

        def model_child(key: str) -> _Node | None:
            field = fields.get(key)
            return None if field is None else _node_from_field(field)

        return model_child
    origin = get_origin(base)
    if origin in (dict, Mapping):
        args = get_args(base)
        if len(args) != 2:
            return None
        value_node = _node_from_annotation(args[1])
        return lambda _key: value_node
    return None


def _merge_list(
    previous: object,
    incoming: object,
    path: ProvenancePath,
) -> tuple[object, tuple[LayerContribution, ...]]:
    if type(previous) is not list or type(incoming) is not list:
        return _replacement(incoming, path)
    result = list(previous)
    operations: list[LayerContribution] = []
    for incoming_item in incoming:
        equal_index = next(
            (index for index, current in enumerate(result) if _carrier_equal(current, incoming_item)),
            None,
        )
        if equal_index is not None:
            operations.append(LayerContribution.contribution(*path, equal_index))
            continue
        result.append(incoming_item)
        operations.append(LayerContribution.replacement(*path, len(result) - 1))
    return result, tuple(operations)


def _carrier_equal(left: object, right: object) -> bool:
    """Type-sensitive structural equality over the closed raw JSON carrier."""
    work: list[tuple[str, object, object]] = [("compare", left, right)]
    active: set[tuple[int, int]] = set()
    while work:
        action, current_left, current_right = work.pop()
        pair = (id(current_left), id(current_right))
        if action == "leave":
            active.remove(pair)
            continue
        if type(current_left) is not type(current_right):
            return False
        if current_left is None:
            continue
        if type(current_left) in (bool, int, str):
            if current_left != current_right:
                return False
            continue
        if type(current_left) is float:
            left_float = current_left
            right_float = cast("float", current_right)
            if not math.isfinite(left_float) or not math.isfinite(right_float):
                return False
            if left_float != right_float:
                return False
            continue
        if type(current_left) is list:
            left_list = cast("list[object]", current_left)
            right_list = cast("list[object]", current_right)
            if len(left_list) != len(right_list) or pair in active:
                return False
            active.add(pair)
            work.append(("leave", current_left, current_right))
            work.extend(
                ("compare", left_item, right_item)
                for left_item, right_item in reversed(list(zip(left_list, right_list, strict=True)))
            )
            continue
        if type(current_left) is dict:
            left_dict = cast("dict[object, object]", current_left)
            right_dict = cast("dict[object, object]", current_right)
            if (
                not _has_exact_string_keys(left_dict)
                or not _has_exact_string_keys(right_dict)
                or len(left_dict) != len(right_dict)
                or pair in active
            ):
                return False
            if any(key not in right_dict for key in left_dict):
                return False
            active.add(pair)
            work.append(("leave", current_left, current_right))
            work.extend(("compare", left_dict[key], right_dict[key]) for key in reversed(tuple(left_dict)))
            continue
        return False
    return True


def _selected_model(node: _Node, value: object) -> type[BaseModel] | None:
    field = node.field
    if field is None:
        return None
    from agentworks.schema._shape import shape_of

    shape = shape_of(field)
    if shape.arms:
        if type(value) is dict:
            if not _has_exact_string_keys(value):
                return None
            tag = value.get(shape.discriminator or "")
            if type(tag) is not str:
                return None
            return next((arm.model for arm in shape.arms if arm.tag == tag), None)
        shorthand = shape.union_scalar_shorthand
        scalar = None if shorthand is None else scalar_shorthand_of(shorthand.arm)
        if shorthand is not None and scalar is not None and type(value) is scalar.annotation:
            return shorthand.arm
        return None
    if shape.structural_arms:
        return (
            structural_arm_for(shape.structural_arms, value)
            if type(value) is dict and _has_exact_string_keys(value)
            else None
        )
    if (
        shape.union_model is not None
        and type(value) is dict
        and _has_exact_string_keys(value)
        and table_addresses_block(shape.union_model, shape.union_members)
    ):
        return shape.union_model
    return None


def _has_discriminated_dispatch(node: _Node) -> bool:
    """Whether pydantic dispatches this field even after a union collapses."""
    if node.field is None:
        return False
    from agentworks.schema._shape import shape_of

    return bool(shape_of(node.field).arms)


def _root_node(node: _Node) -> _Node:
    seen: set[type[BaseModel]] = set()
    while True:
        base = _base_annotation(node.annotation)
        if not (_is_model(base) and issubclass(base, RootModel)) or base in seen:
            return node
        seen.add(base)
        fields = model_fields_of(base)
        if fields is None or (root := fields.get("root")) is None:
            return node
        inner = _node_from_field(root)
        node = _Node(
            inner.annotation,
            inner.field,
            node.strategy or inner.strategy,
            node.model_strategy or _model_strategy(base),
        )


def _effective_strategy(node: _Node, base: object) -> MergeStrategy:
    if node.strategy is not None:
        return node.strategy
    model_strategy = node.model_strategy or (_model_strategy(base) if _is_model(base) else None)
    if model_strategy is not None:
        return model_strategy
    origin = get_origin(base)
    if (_is_model(base) and not issubclass(base, RootModel)) or origin in (dict, Mapping):
        return MergeStrategy.MERGE
    if origin is list:
        return MergeStrategy.APPEND_DEDUPE
    return MergeStrategy.REPLACE


def _node_from_field(field: FieldInfo) -> _Node:
    return _Node(field.annotation, field, _one_strategy(spine_metadata(field)))


def _node_from_annotation(annotation: object) -> _Node:
    return _node_from_field(FieldInfo.from_annotation(cast("Any", annotation)))


def _model_strategy(model: object) -> MergeStrategy | None:
    strategy = getattr(model, "merge_strategy", None)
    return strategy if isinstance(strategy, MergeStrategy) else None


def _one_strategy(metadata: list[object]) -> MergeStrategy | None:
    return next((item for item in metadata if isinstance(item, MergeStrategy)), None)


def _base_annotation(annotation: object) -> object:
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if _is_union(annotation):
            args = get_args(annotation)
            present = tuple(arg for arg in args if arg is not type(None))
            if len(present) == 1 and len(present) != len(args):
                annotation = present[0]
                continue
        return annotation


def _replacement(
    incoming: object,
    path: ProvenancePath,
) -> tuple[object, tuple[LayerContribution, ...]]:
    return incoming, _replacement_operations(path)


def _replacement_operations(path: ProvenancePath) -> tuple[LayerContribution, LayerContribution]:
    return (
        LayerContribution.reset_prefix(*path),
        LayerContribution.replacement(*path),
    )


def _has_exact_string_keys(value: dict[object, object]) -> bool:
    return all(type(key) is str for key in value)


def _is_union(annotation: object) -> bool:
    return get_origin(annotation) in (Union, types.UnionType)


def _is_model(annotation: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def merge_contract_error(model: type[BaseModel]) -> str | None:
    """Return why ``model`` cannot safely participate in schema merging."""
    checker = _ContractChecker()
    return checker.model_error(model, active=True, path=model.__name__)


class _ContractChecker:
    def __init__(self) -> None:
        self._checked: set[tuple[type[BaseModel], bool, bool]] = set()
        self._comparable: set[type[BaseModel]] = set()

    def model_error(
        self,
        model: type[BaseModel],
        *,
        active: bool,
        path: str,
        model_overridden: bool = False,
    ) -> str | None:
        cache_key = (model, active, model_overridden)
        if cache_key in self._checked:
            return None
        self._checked.add(cache_key)
        strategy_value = getattr(model, "merge_strategy", None)
        if strategy_value is not None and not isinstance(strategy_value, MergeStrategy):
            return f"{path} declares unsupported model merge_strategy {strategy_value!r}"
        strategy = strategy_value if isinstance(strategy_value, MergeStrategy) else None
        if strategy is not None and not _model_is_mapping_shaped(model):
            return f"{path} declares model merge strategy {strategy.value!r} but is not mapping-shaped"
        if strategy is MergeStrategy.APPEND_DEDUPE:
            return f"{path} declares append-dedupe as an object-model strategy"
        effective_active = active and (model_overridden or strategy is not MergeStrategy.REPLACE)
        root_model_overridden = model_overridden or strategy is MergeStrategy.MERGE
        fields = model_fields_of(model)
        if fields is None:
            return None
        root_model = issubclass(model, RootModel)
        for name, field in fields.items():
            field_path = path if root_model and name == "root" else f"{path}.{name}"
            if field.validation_alias is not None:
                return f"{field_path} declares validation_alias, which the merge contract does not support"
            field_strategies = [item for item in spine_metadata(field) if isinstance(item, MergeStrategy)]
            field_active = active if root_model and field_strategies else effective_active
            reason = self.annotation_error(
                field.annotation,
                metadata=spine_metadata(field),
                active=field_active,
                path=field_path,
                role="field",
                field=field,
                model_overridden=root_model_overridden if root_model else False,
            )
            if reason is not None:
                return reason
        return None

    def annotation_error(
        self,
        annotation: object,
        *,
        metadata: list[object],
        active: bool,
        path: str,
        role: str,
        field: FieldInfo | None = None,
        model_overridden: bool = False,
    ) -> str | None:
        strategies = [item for item in metadata if isinstance(item, MergeStrategy)]
        if len(strategies) > 1:
            return f"{path} declares {len(strategies)} merge strategies; at most one is allowed"
        if strategies and role in ("sequence element", "mapping key"):
            return f"{path} declares merge strategy on a {role}, where conflicts have no merge-policy slot"
        raw_base, _outer_metadata = _split_annotated(annotation)
        if _is_union(raw_base):
            non_null_arms = tuple(arm for arm in get_args(raw_base) if arm is not type(None))
            if len(non_null_arms) > 1 and any(
                any(isinstance(item, MergeStrategy) for item in _split_annotated(arm)[1]) for arm in non_null_arms
            ):
                return f"{path} declares merge strategy on an individual union arm"
        strategy = strategies[0] if strategies else None
        base = _base_annotation(annotation)
        shape = _annotation_shape(
            base,
            field or FieldInfo.from_annotation(cast("Any", annotation)),
        )
        if strategy is MergeStrategy.MERGE and shape != "object":
            return f"{path} declares merge on non-object shape {shape}"
        if strategy is MergeStrategy.APPEND_DEDUPE and shape != "list":
            return f"{path} declares append-dedupe on non-list shape {shape}"
        effective_active = active and strategy is not MergeStrategy.REPLACE

        origin = get_origin(base)
        if base is list:
            return f"{path} has an untyped list; mark it replace" if effective_active else None
        if base in (dict, Mapping):
            return f"{path} has an untyped mapping; mark it replace" if effective_active else None
        if origin is list:
            args = get_args(base)
            if len(args) != 1:
                return f"{path} has an untyped list; mark it replace" if effective_active else None
            element = args[0]
            _element_base, element_metadata = _split_annotated(element)
            reason = self.annotation_error(
                element,
                metadata=element_metadata,
                active=False,
                path=f"{path}[]",
                role="sequence element",
            )
            if reason is not None:
                return reason
            if effective_active and strategy is not MergeStrategy.REPLACE and not self.comparable(element):
                return (
                    f"{path} uses append-dedupe with an element outside the closed comparable carrier; mark it replace"
                )
            return None
        if origin in (dict, Mapping):
            args = get_args(base)
            if len(args) != 2:
                return f"{path} has an untyped mapping; mark it replace" if effective_active else None
            key, value = args
            _key_base, key_metadata = _split_annotated(key)
            reason = self.annotation_error(
                key,
                metadata=key_metadata,
                active=False,
                path=f"{path}{{key}}",
                role="mapping key",
            )
            if reason is not None:
                return reason
            if effective_active and strategy is not MergeStrategy.REPLACE and _base_annotation(key) is not str:
                return f"{path} merges a mapping whose key annotation is not exact str; mark it replace"
            _value_base, value_metadata = _split_annotated(value)
            return self.annotation_error(
                value,
                metadata=value_metadata,
                active=effective_active,
                path=f"{path}{{value}}",
                role="mapping value",
            )
        if _is_union(base):
            child_model_overridden = (model_overridden and strategy is None) or strategy is MergeStrategy.MERGE
            for index, arm in enumerate(get_args(base)):
                arm_base, arm_metadata = _split_annotated(arm)
                reason = self.annotation_error(
                    arm_base,
                    metadata=arm_metadata,
                    active=effective_active,
                    path=f"{path}<arm {index + 1}>",
                    role="union arm",
                    model_overridden=child_model_overridden,
                )
                if reason is not None:
                    return reason
            return None
        if _is_model(base):
            child_model_overridden = (model_overridden and strategy is None) or strategy is MergeStrategy.MERGE
            return self.model_error(
                base,
                active=effective_active,
                path=path,
                model_overridden=child_model_overridden,
            )
        return None

    def comparable(self, annotation: object) -> bool:
        base, metadata = _unwrapped_annotation(annotation)
        if base in (None, type(None), bool, int, str):
            return True
        if base is float:
            constraints = [item for item in metadata if isinstance(item, AllowInfNan)]
            return bool(constraints) and constraints[-1].allow_inf_nan is False
        origin = get_origin(base)
        if origin is Literal:
            return bool(get_args(base)) and all(_literal_is_carrier(value) for value in get_args(base))
        if origin is list:
            args = get_args(base)
            return len(args) == 1 and self.comparable(args[0])
        if origin in (dict, Mapping):
            args = get_args(base)
            return len(args) == 2 and _base_annotation(args[0]) is str and self.comparable(args[1])
        if _is_union(base):
            args = get_args(base)
            return bool(args) and all(self.comparable(arm) for arm in args)
        if _is_model(base):
            if base in self._comparable:
                return True
            self._comparable.add(base)
            shorthand = scalar_shorthand_of(base)
            if shorthand is not None and not self.comparable(shorthand.annotation):
                return False
            fields = model_fields_of(base)
            if fields is None:
                return False
            return all(self.comparable(field.rebuild_annotation()) for field in fields.values())
        return False


def _annotation_shape(annotation: object, field: FieldInfo) -> str:
    if _is_model(annotation):
        return "object" if _model_is_mapping_shaped(annotation) else "scalar"
    origin = get_origin(annotation)
    if annotation in (dict, Mapping) or origin in (dict, Mapping):
        return "object"
    if annotation is list or origin is list:
        return "list"
    if _is_union(annotation):
        from agentworks.schema._shape import shape_of

        shape = shape_of(field)
        if shape.arms or shape.structural_arms or shape.union_model is not None:
            return "object"
    return "scalar"


def _model_is_mapping_shaped(
    model: type[BaseModel],
    seen: set[type[BaseModel]] | None = None,
) -> bool:
    seen = set() if seen is None else seen
    while True:
        if scalar_shorthand_of(model) is not None:
            return False
        if not issubclass(model, RootModel):
            return True
        if model in seen:
            return False
        seen.add(model)
        fields = model_fields_of(model)
        if fields is None or (root := fields.get("root")) is None:
            return False
        base = _base_annotation(root.annotation)
        from agentworks.schema._shape import shape_of

        if shape_of(root).union_scalar_shorthand is not None:
            return False
        if get_origin(base) in (dict, Mapping):
            return True
        if _is_union(base):
            return _root_union_is_mapping_shaped(base, root, seen)
        if not _is_model(base):
            return False
        model = base


def _root_union_is_mapping_shaped(
    annotation: object,
    field: FieldInfo,
    seen: set[type[BaseModel]],
) -> bool:
    """Whether every selectable value of one root union is an object."""
    from agentworks.schema._shape import shape_of

    shape = shape_of(field)
    if shape.union_scalar_shorthand is not None:
        return False
    selected = tuple(arm.model for arm in shape.arms) or shape.structural_arms
    if not selected:
        return False
    raw_arms = tuple(_base_annotation(arm) for arm in get_args(annotation))
    if not all(_is_model(arm) for arm in raw_arms):
        return False
    models = cast("tuple[type[BaseModel], ...]", raw_arms)
    if len(models) != len(selected) or set(models) != set(selected):
        return False
    return all(_model_is_mapping_shaped(arm, set(seen)) for arm in models)


def _split_annotated(annotation: object) -> tuple[object, list[object]]:
    if get_origin(annotation) is not Annotated:
        return annotation, []
    args = get_args(annotation)
    return args[0], list(args[1:])


def _unwrapped_annotation(annotation: object) -> tuple[object, list[object]]:
    metadata: list[object] = []
    while True:
        annotation, found_metadata = _split_annotated(annotation)
        metadata.extend(found_metadata)
        if _is_union(annotation):
            args = get_args(annotation)
            present = tuple(arg for arg in args if arg is not type(None))
            if len(present) == 1 and len(present) != len(args):
                annotation = present[0]
                continue
        if not found_metadata:
            return annotation, metadata


def _literal_is_carrier(value: object) -> bool:
    return value is None or type(value) in (bool, int, str) or (type(value) is float and math.isfinite(value))


__all__ = ["MergeStrategy", "merge_contract_error", "merge_model"]
