"""Non-constructing registration conformance for capability classes.

Boundary, shared by every check here: a capability class arriving from
outside our type checking. ``plugins.register_plugin`` is the only
production caller, and it is exported from the package's public API, so
any caller can hand it a class our mypy run never saw.

Core built-ins reach the same checks through a different door and at a
different moment: ``tests/capabilities/test_capability_descriptors.py``'s
``test_every_registered_builtin_impl_conforms`` runs this whole chain over
every seated implementation of every kind. That is the right instrument for
our own tree, where a non-conforming built-in is a bug to fail the build on
rather than a class to refuse at startup, and refusing one at startup would
brick the CLI instead of helping anyone.
"""

from __future__ import annotations

import inspect
import math
import types
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from agentworks.capabilities.descriptor import ConfigContract, ModelInputDomain
from agentworks.schema import (
    RefMarker,
    merge_contract_error,
    model_is_complete,
    reference_marker_error,
    structural_union_error,
    union_scalar_shorthand_error,
)

if TYPE_CHECKING:
    from agentworks.capabilities.descriptor import CapabilityKindDescriptor

_MISSING = object()


def conformance_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Return why ``impl`` violates ``descriptor``, without constructing it.

    Primary config conformance follows ``config_for`` because that is the
    model every core consumer uses. The mapping model remains a direct class
    declaration with no selection hook.
    """
    preliminary = (
        _contract_error(descriptor, impl)
        or _metadata_error(impl)
        or _constructibility_error(impl)
        or _config_hook_error(impl)
        or _attributes_error(descriptor, impl)
        or _operations_error(descriptor, impl)
        or _focused_operation_error(descriptor, impl)
        or _version_error(descriptor, impl)
    )
    if preliminary is not None:
        return preliminary

    model, hook_error = _offered_config_model(impl)
    if hook_error is not None:
        return hook_error
    model_label = "config_model"
    if inspect.getattr_static(impl, "config_model", _MISSING) is not model:
        model_label = "config_for() offered model"
    return (
        _model_error(
            descriptor,
            impl,
            model_label,
            descriptor.config_schema,
            model=model,
        )
        or (
            _model_error(
                descriptor,
                impl,
                "mapping_model",
                descriptor.mapping_schema,
            )
            if descriptor.mapping_schema is not None
            else None
        )
        or _forbidden_reference_error(descriptor, model, model_label)
    )


def _contract_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    contract = descriptor.implementation_contract
    if not issubclass(impl, contract):
        return f"it does not derive from {contract.__name__}, the {descriptor.kind} implementation contract"
    return None


def _metadata_error(impl: type) -> str | None:
    name = getattr(impl, "name", None)
    if type(name) is not str or not name or "/" in name:
        return f"its capability name {name!r} is not a non-empty, '/'-free 'name' class attribute"
    description = getattr(impl, "description", None)
    if not isinstance(description, str):
        return f"its 'description' class attribute is {description!r}, not a string"
    return None


def _constructibility_error(impl: type) -> str | None:
    """Prove the class could be constructed, structurally and without doing it.

    One half of the seam the framework keeps at registration (call shape is
    the other): a class that leaves an operation unimplemented fails at the
    first operation instead of at the moment it was seated, far from the
    author who can fix it.
    """
    if inspect.isabstract(impl):
        unimplemented = ", ".join(sorted(getattr(impl, "__abstractmethods__", ())))
        return f"it is abstract (unimplemented operations: {unimplemented})"
    return None


def _config_hook_error(impl: type) -> str | None:
    """The impl's ``config_for`` is callable.

    The other half of the call-shape seam, and the framework calls this hook
    on every path that reads a capability's config: sample rendering, schema
    emission, the field reference, union assembly, and construct-time
    validation. ``Capability`` supplies a working default, so an impl only
    fails here by shadowing it with something that is not callable, which a
    class arriving through ``register_plugin`` can do and our type checker
    never sees. Refused here, at the seam, rather than defended against by
    every interior caller: without this the class seats cleanly and the
    first resource command to render its config dies on a raw ``TypeError``.
    """
    if not callable(getattr(impl, "config_for", None)):
        return "its 'config_for' is not callable, so the framework cannot ask which config it offers"
    return None


def _offered_config_model(impl: type) -> tuple[object, str | None]:
    """Call the checked model-selection hook once at the registration seam."""
    from agentworks.capabilities.config import offered_model

    try:
        return offered_model(impl), None
    except Exception as exc:
        return None, (
            f"its 'config_for' raised {type(exc).__name__} while selecting the config model, "
            "so the framework cannot determine which schema it offers"
        )


def _attributes_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    missing = sorted(attr for attr in descriptor.required_attributes if getattr(impl, attr, _MISSING) is _MISSING)
    if missing:
        return f"it is missing the required {descriptor.kind} attributes: {', '.join(missing)}"
    return None


def _operations_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    missing = sorted(op for op in descriptor.required_operations if not callable(getattr(impl, op, None)))
    if missing:
        return f"it does not implement the required {descriptor.kind} operations: {', '.join(missing)}"
    return None


def _focused_operation_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    from agentworks.capabilities.secret_backend.base import SecretBackend
    from agentworks.capabilities.secret_backend.conformance import _secret_backend_conformance_error

    if descriptor.implementation_contract is SecretBackend:
        return _secret_backend_conformance_error(impl)
    return None


def _model_error(
    descriptor: CapabilityKindDescriptor,
    impl: type,
    attribute_name: str,
    contract: ConfigContract,
    *,
    model: object = _MISSING,
) -> str | None:
    if model is _MISSING:
        model = getattr(impl, attribute_name, None)
    if model is None:
        return (
            f"it declares no {attribute_name}, so the framework has no schema to validate its "
            "config against (a capability that accepts none declares a model with no fields)"
        )
    if not isinstance(model, type) or not issubclass(model, contract.base):
        return (
            f"its {attribute_name} is {model!r}, which is not a {contract.base.__name__} subclass "
            f"(the {descriptor.kind} config contract)"
        )
    if not model_is_complete(model):
        return (
            f"its {attribute_name} {model.__name__} cannot be built (an unresolved annotation?), "
            "so nothing could validate or extract references against it"
        )
    union_shape = structural_union_error(model)
    if union_shape is not None:
        return f"its {attribute_name} declares an invalid structural union: {union_shape}"
    placement = reference_marker_error(model)
    if placement is not None:
        return f"its {attribute_name} declares a reference marker nothing can honor: {placement}"
    shorthand = union_scalar_shorthand_error(model)
    if shorthand is not None:
        return f"its {attribute_name} declares an inconsistent union scalar shorthand: {shorthand}"
    tag_error = _model_tag_error(descriptor, impl, attribute_name, model, contract)
    if tag_error is not None:
        return tag_error
    if contract.input_domain is ModelInputDomain.JSON_NATIVE:
        violation = _json_native_violation(model)
        if violation is not None:
            label, path, mapping_key = violation
            problem = "non-string mapping key type" if mapping_key else "non-JSON-native type"
            return (
                f"its {attribute_name} {model.__name__} accepts {problem} {label} at {path}; "
                f"{descriptor.kind} mapping input is limited to JSON-native types"
            )
    if contract.layered_merge:
        merge_contract = merge_contract_error(model)
        if merge_contract is not None:
            return f"its {attribute_name} declares an invalid merge contract: {merge_contract}"
    return None


def _model_tag_error(
    descriptor: CapabilityKindDescriptor,
    impl: type,
    attribute_name: str,
    model: type[BaseModel],
    contract: ConfigContract,
) -> str | None:
    discriminator = contract.discriminator
    if discriminator is None:
        return None
    field = model.model_fields.get(discriminator)
    tags = _literal_values(field.annotation) if field is not None else ()
    name = getattr(impl, "name", None)
    if name not in tags:
        return (
            f"its {attribute_name} {model.__name__} does not tag itself {name!r}: the {descriptor.kind} "
            f"config is selected by a {discriminator!r} field typed Literal[{name!r}], and this model "
            f"declares {list(tags) or 'no such field'}"
        )
    return None


def _literal_values(annotation: object) -> tuple[object, ...]:
    return get_args(annotation) if get_origin(annotation) is Literal else ()


def _forbidden_reference_error(
    descriptor: CapabilityKindDescriptor,
    model: object,
    model_label: str,
) -> str | None:
    forbidden = descriptor.config_schema.forbidden_reference_kinds
    if not forbidden:
        return None
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        return None
    found = sorted((kind, path) for path, kind in _reference_markers(model) if kind in forbidden)
    if not found:
        return None
    kind, path = found[0]
    return (
        f"its {model_label} {model.__name__} references forbidden kind {kind!r} at {path}; "
        f"{descriptor.kind} source config cannot reference {kind} values"
    )


def _reference_markers(model: type[BaseModel]) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    visited: set[type[BaseModel]] = set()

    def walk_model(current: type[BaseModel], path: str) -> None:
        if current in visited:
            return
        visited.add(current)
        root_model = bool(getattr(current, "__pydantic_root_model__", False))
        for name, field in current.model_fields.items():
            field_path = path if root_model and name == "root" else f"{path}.{name}"
            found.extend((field_path, marker.kind) for marker in field.metadata if isinstance(marker, RefMarker))
            walk_annotation(field.annotation, field_path)

    def walk_annotation(annotation: object, path: str) -> None:
        origin = get_origin(annotation)
        if origin is Annotated:
            base, *metadata = get_args(annotation)
            found.extend((path, marker.kind) for marker in metadata if isinstance(marker, RefMarker))
            walk_annotation(base, path)
            return
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            walk_model(annotation, path)
            return
        if origin in (list, set, frozenset, tuple):
            for item in get_args(annotation):
                if item is not Ellipsis:
                    walk_annotation(item, f"{path}[]")
            return
        if origin is dict:
            key, value = get_args(annotation)
            walk_annotation(key, f"{path}{{key}}")
            walk_annotation(value, f"{path}{{value}}")
            return
        for arm in get_args(annotation):
            walk_annotation(arm, path)

    walk_model(model, "root")
    return tuple(found)


def _json_native_violation(model: type[BaseModel]) -> tuple[str, str, bool] | None:
    visited: set[type[BaseModel]] = set()

    def walk_model(current: type[BaseModel], path: str) -> tuple[str, str, bool] | None:
        if current in visited:
            return None
        visited.add(current)
        root_model = bool(getattr(current, "__pydantic_root_model__", False))
        for name, field in current.model_fields.items():
            field_path = path if root_model and name == "root" else f"{path}.{name}"
            violation = walk(field.annotation, field_path)
            if violation is not None:
                return violation
        return None

    def walk(annotation: object, path: str) -> tuple[str, str, bool] | None:
        origin = get_origin(annotation)
        if origin is Annotated:
            return walk(get_args(annotation)[0], path)
        if annotation in (Any, object, None, type(None), str, bool, int, float):
            return None
        if origin is Literal:
            for literal in get_args(annotation):
                if not _json_scalar(literal):
                    return (_type_label(type(literal)), path, False)
            return None
        if origin in (Union, types.UnionType):
            for arm in get_args(annotation):
                violation = walk(arm, path)
                if violation is not None:
                    return violation
            return None
        if origin is list:
            return walk(get_args(annotation)[0], f"{path}[]")
        if origin is dict:
            key, value = get_args(annotation)
            if not _string_only_key(key):
                return (_type_label(key), f"{path}{{key}}", True)
            return walk(value, f"{path}{{value}}")
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return walk_model(annotation, path)
        return (_type_label(annotation), path, False)

    return walk_model(model, "root")


def _json_scalar(value: object) -> bool:
    return value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value))


def _string_only_key(annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _string_only_key(get_args(annotation)[0])
    if annotation is str:
        return True
    if origin is Literal:
        return bool(get_args(annotation)) and all(type(value) is str for value in get_args(annotation))
    if origin in (Union, types.UnionType):
        return bool(get_args(annotation)) and all(_string_only_key(arm) for arm in get_args(annotation))
    return False


def _version_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """The impl declares a contract version this build supports.

    Trivially satisfied while there is one version, which is the point: the
    declaration and the comparison both exist before the first incompatible
    change, so nothing has to be retrofitted when one arrives. Without it, a
    class written against an older contract seats cleanly and goes wrong
    later, at a call site with no idea a contract ever revved. That is what
    the version check is for, and it is why it stays while the annotation
    comparisons beside it did not.

    Exact equality, deliberately: a contract change is a hard cutover, and
    every impl migrates before the descriptor's number moves. Supporting two
    versions at once is a real decision (a supported range, a compatibility
    rule) to make when a migration actually needs it, not a default to drift
    into.
    """
    declared = getattr(impl, "contract_version", None)
    if declared != descriptor.contract_version:
        return (
            f"it declares contract_version {declared!r}, but this build supports "
            f"{descriptor.kind} contract version {descriptor.contract_version}"
        )
    return None


def _type_label(annotation: object) -> str:
    if annotation is None:
        return "None"
    if isinstance(annotation, type):
        if annotation.__module__ == "builtins":
            return annotation.__name__
        return f"{annotation.__module__}.{annotation.__qualname__}"
    text = str(annotation)
    return text.removeprefix("typing.").replace("collections.abc.", "")
