"""Focused registration conformance for the secret-backend class contract."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend.base import SecretBackend


@dataclass(frozen=True, slots=True)
class _ParameterContract:
    """One parameter the resolution loop supplies, by name and by kind."""

    name: str
    kind: inspect._ParameterKind


#: The call shape of every classmethod the resolution loop invokes on a
#: registered backend class, keyed by operation and valued by its parameters
#: after ``cls``. ``backend_readiness`` takes none. These are the names and
#: kinds ``resolve.py`` actually calls with, so a mismatch here is a
#: TypeError at the first source turn rather than at registration.
_OPERATION_CONTRACTS: dict[str, tuple[_ParameterContract, ...]] = {
    "backend_readiness": (),
    "would_attempt": (
        _ParameterContract("secret_name", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        _ParameterContract("mapping_present", inspect.Parameter.KEYWORD_ONLY),
    ),
    "describe_lookup": (
        _ParameterContract("secret_name", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        _ParameterContract("mapping", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "external_operation_timeout": (_ParameterContract("config", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
    "create_client": (
        _ParameterContract("source_name", inspect.Parameter.KEYWORD_ONLY),
        _ParameterContract("config", inspect.Parameter.KEYWORD_ONLY),
        _ParameterContract("interaction_broker", inspect.Parameter.KEYWORD_ONLY),
        _ParameterContract("remaining_time", inspect.Parameter.KEYWORD_ONLY),
    ),
}


def _secret_backend_conformance_error(impl: type[SecretBackend]) -> str | None:
    """Check the class-only facts and call shapes specific to secret backends.

    Boundary: a capability class arriving at registration from outside our
    type checking. ``register_plugin`` is exported from the ``plugins``
    package's public API and seats whatever class it is handed, so the shape
    the resolution loop calls with is checked once here rather than trusted.
    What it checks is deliberately narrow: whether the class is callable the
    way ``resolve.py`` calls it. Return values and annotations are not
    re-checked, at registration or per call.
    """
    from agentworks.capabilities.secret_backend.base import InteractionChannel

    channel = getattr(impl, "interaction_channel", None)
    if type(channel) is not InteractionChannel:
        return f"its interaction_channel class attribute is {channel!r}, not an InteractionChannel member"

    for name, parameters in _OPERATION_CONTRACTS.items():
        error = _classmethod_conformance_error(impl, name=name, parameters=parameters)
        if error is not None:
            return error
    return None


def _classmethod_conformance_error(
    impl: type[SecretBackend],
    *,
    name: str,
    parameters: tuple[_ParameterContract, ...],
) -> str | None:
    """Check one classmethod's call shape without binding or constructing its owner.

    Boundary as named on :func:`_secret_backend_conformance_error`.
    """
    raw = inspect.getattr_static(impl, name)
    if not isinstance(raw, classmethod):
        return f"its {name} must be declared as @classmethod"
    declared = tuple(inspect.signature(raw.__func__).parameters.values())
    if not declared:
        return f"its {name} must declare a 'cls' binding parameter"
    # The binding parameter's SPELLING is not part of the contract: Python binds
    # the class to whatever the first parameter is called. Its kind and default
    # are, because either one stops the framework calling the method at all.
    binding = declared[0]
    if binding.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
        return f"its {name} parameter {binding.name!r} must be positional-or-keyword"
    if binding.default is not inspect.Parameter.empty:
        return f"its {name} parameter {binding.name!r} must not have a default"
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
    return None


def _kind_label(kind: inspect._ParameterKind) -> str:
    return {
        inspect.Parameter.POSITIONAL_ONLY: "positional-only",
        inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional-or-keyword",
        inspect.Parameter.VAR_POSITIONAL: "variadic positional",
        inspect.Parameter.KEYWORD_ONLY: "keyword-only",
        inspect.Parameter.VAR_KEYWORD: "variadic keyword",
    }[kind]
