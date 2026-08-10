"""Focused registration conformance for the secret-backend class contract."""

from __future__ import annotations

import inspect
import typing
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretSourceClient,
)
from agentworks.schema import AgwModel

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend.base import SecretBackend


@dataclass(frozen=True, slots=True)
class _ParameterContract:
    name: str
    kind: inspect._ParameterKind
    annotation: object


def _secret_backend_conformance_error(impl: type[SecretBackend]) -> str | None:
    """Check the class-only facts and operations specific to secret backends."""
    from agentworks.resources.graph import Readiness

    interactive = getattr(impl, "interactive", None)
    if type(interactive) is not bool:
        return f"its interactive class attribute is {interactive!r}, not a bool"

    operation_contracts = (
        (
            "backend_readiness",
            (),
            Readiness,
        ),
        (
            "would_attempt",
            (
                _ParameterContract("secret_name", inspect.Parameter.POSITIONAL_OR_KEYWORD, str),
                _ParameterContract("mapping_present", inspect.Parameter.KEYWORD_ONLY, bool),
            ),
            bool,
        ),
        (
            "describe_lookup",
            (
                _ParameterContract("secret_name", inspect.Parameter.POSITIONAL_OR_KEYWORD, str),
                _ParameterContract("mapping", inspect.Parameter.POSITIONAL_OR_KEYWORD, BaseModel | None),
            ),
            str | None,
        ),
        (
            "external_operation_timeout",
            (_ParameterContract("config", inspect.Parameter.POSITIONAL_OR_KEYWORD, AgwModel),),
            float | None,
        ),
    )
    for name, parameters, return_annotation in operation_contracts:
        error = _classmethod_conformance_error(
            impl,
            name=name,
            parameters=parameters,
            return_annotation=return_annotation,
            definition_globals={"AgwModel": AgwModel, "BaseModel": BaseModel, "Readiness": Readiness},
        )
        if error is not None:
            return error
    return _create_client_conformance_error(impl)


def _create_client_conformance_error(impl: type[SecretBackend]) -> str | None:
    """Check the exact secret-backend factory shape fixed by the contract."""
    return _classmethod_conformance_error(
        impl,
        name="create_client",
        parameters=(
            _ParameterContract("source_name", inspect.Parameter.KEYWORD_ONLY, str),
            _ParameterContract("config", inspect.Parameter.KEYWORD_ONLY, AgwModel),
            _ParameterContract(
                "interaction_broker",
                inspect.Parameter.KEYWORD_ONLY,
                InteractionBroker | None,
            ),
            _ParameterContract("remaining_time", inspect.Parameter.KEYWORD_ONLY, RemainingTime),
        ),
        return_annotation=AbstractContextManager[SecretSourceClient],
    )


def _classmethod_conformance_error(
    impl: type[SecretBackend],
    *,
    name: str,
    parameters: tuple[_ParameterContract, ...],
    return_annotation: object,
    definition_globals: dict[str, object] | None = None,
) -> str | None:
    """Check one exact classmethod without binding or constructing its owner."""
    owner = next(base for base in impl.__mro__ if name in base.__dict__)
    raw = inspect.getattr_static(impl, name)
    if not isinstance(raw, classmethod):
        return f"its {name} must be declared as @classmethod"
    function = raw.__func__
    declared = tuple(inspect.signature(function).parameters.values())
    if not declared:
        return f"its {name} must declare a 'cls' binding parameter"
    binding = declared[0]
    if binding.name != "cls":
        return f"its {name} first parameter must be named 'cls' (got {binding.name!r})"
    if binding.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
        return f"its {name} parameter 'cls' must be positional-or-keyword"
    if binding.default is not inspect.Parameter.empty:
        return f"its {name} parameter 'cls' must not have a default"
    if binding.annotation is not inspect.Parameter.empty:
        return f"its {name} parameter 'cls' must not have an annotation"
    try:
        hints = typing.get_type_hints(
            function,
            globalns={**(definition_globals or {}), **function.__globals__},
            localns=dict(vars(owner)),
        )
    except Exception as exc:  # noqa: BLE001 - stable output names only the exception type
        return f"its {name} annotations could not be resolved: {type(exc).__name__}"
    explicit = declared[1:]
    if len(explicit) != len(parameters):
        return f"its {name} must declare {len(parameters)} parameters after cls (got {len(explicit)})"
    for position, (parameter, expected) in enumerate(zip(explicit, parameters, strict=True), start=1):
        if parameter.name != expected.name:
            return f"its {name} parameter {position} must be named {expected.name!r} (got {parameter.name!r})"
        if parameter.kind is not expected.kind:
            return f"its {name} parameter {expected.name!r} must be {_kind_label(expected.kind)}"
        if parameter.default is not inspect.Parameter.empty:
            return f"its {name} parameter {expected.name!r} must not have a default"
        actual = hints.get(expected.name)
        if actual != expected.annotation:
            return (
                f"its {name} parameter {expected.name!r} must be annotated as "
                f"{_type_label(expected.annotation)} "
                f"(got {_type_label(actual)})"
            )
    actual_return = hints.get("return")
    if actual_return != return_annotation:
        return f"its {name} must return {_type_label(return_annotation)} (got {_type_label(actual_return)})"
    return None


def _kind_label(kind: inspect._ParameterKind) -> str:
    return {
        inspect.Parameter.POSITIONAL_ONLY: "positional-only",
        inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional-or-keyword",
        inspect.Parameter.VAR_POSITIONAL: "variadic positional",
        inspect.Parameter.KEYWORD_ONLY: "keyword-only",
        inspect.Parameter.VAR_KEYWORD: "variadic keyword",
    }[kind]


def _type_label(annotation: object) -> str:
    if annotation is None:
        return "None"
    if annotation == AbstractContextManager[SecretSourceClient]:
        return "AbstractContextManager[SecretSourceClient]"
    if isinstance(annotation, type):
        if annotation.__module__ == "builtins":
            return annotation.__name__
        return f"{annotation.__module__}.{annotation.__qualname__}"
    text = str(annotation)
    return text.removeprefix("typing.").replace("collections.abc.", "")
