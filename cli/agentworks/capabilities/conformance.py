"""Registration-time conformance: does an impl actually satisfy its kind's
contract?

The descriptor makes "trust but verify" enforceable. Before this, a
capability impl reached its registry through an ``isinstance(impl, type)``
gate and a ``cast``, so a class that merely looked plausible seated fine and
failed later, far from the mistake. :func:`conformance_error` is the whole
check, derived entirely from the kind's descriptor, and
``register_plugin`` runs it in its validation pass before any registry is
mutated, so a non-conforming impl is a typed error naming the plugin and
seating stays all-or-nothing.

The check is STRUCTURAL and never constructs the impl. That is deliberate:
it must say the same thing for every kind regardless of registry policy, so
that when wave 3 ends ``secret-backend``'s constructed-singleton exception
the check does not change. (``secret-backend``'s ``impl()`` call is the
adapter's ``prepare`` step building the registry payload, a separate,
already-fallible-and-caught concern.)
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Literal, get_args, get_origin

from agentworks.errors import StateError
from agentworks.schema import model_is_complete

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor

_MISSING = object()
"""Absence sentinel for attribute presence checks. ``None`` cannot serve:
a member whose legitimate value is ``None`` (or ``False``) must read as
present, not missing."""


def conformance_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Why ``impl`` does not satisfy ``descriptor``'s contract, or ``None``
    when it does.

    Returns a reason rather than raising so each caller frames it in its own
    vocabulary (``register_plugin`` attributes it to the plugin; the table
    self-test reports it against the built-in). A malformed DESCRIPTOR is a
    different failure class and raises: that is a framework bug, not
    something an impl author can fix.
    """
    return (
        _contract_error(descriptor, impl)
        or _metadata_error(impl)
        or _attributes_error(descriptor, impl)
        or _constructibility_error(impl)
        or _operations_error(descriptor, impl)
        or _config_model_error(descriptor, impl)
        or _version_error(descriptor, impl)
    )


def _is_protocol(contract: type) -> bool:
    """Whether ``contract`` is a ``typing.Protocol``.

    ``typing.is_protocol`` is the public spelling and landed in 3.13; this
    project's floor is 3.12, so read the flag the typing machinery stamps on
    every Protocol class (and only on Protocol classes: a concrete
    implementer inheriting one carries ``False``). Swap in
    ``typing.is_protocol`` when the floor moves.
    """
    return bool(getattr(contract, "_is_protocol", False))


def _contract_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 1: conformance to the implementation contract, branched on
    whether the contract is a Protocol.

    The four contracts are not uniform in shape, and that is a code fact
    rather than an oversight. A Protocol cannot be checked nominally:
    ``issubclass`` against one whose members include properties raises
    ``TypeError`` even with ``@runtime_checkable``, which is exactly
    ``SecretBackend``'s shape. Its enforcement is structural instead, and
    that is what the metadata, attribute, and operation checks do, so its
    descriptor's ``implementation_contract`` is documentary.

    Anything else, ABC or plain class alike, gets the nominal check. The
    branch keys on protocol-ness rather than on deriving from ``Capability``
    so that a future kind declaring a non-``Capability`` base still gets a
    real check instead of quietly degrading to the structural one.
    """
    contract = descriptor.implementation_contract
    if not isinstance(contract, type):
        raise StateError(
            f"the {descriptor.kind} descriptor's implementation_contract is {contract!r}, "
            f"which is neither a class nor a Protocol; conformance cannot be checked against it"
        )
    if _is_protocol(contract):
        return None
    if not issubclass(impl, contract):
        return f"it does not derive from {contract.__name__}, the {descriptor.kind} implementation contract"
    return None


def _metadata_error(impl: type) -> str | None:
    """Check 2: the identity metadata the capability row carries, readable
    at CLASS level.

    Concrete impls expose ``name`` / ``description`` as class attributes
    uniformly, including the secret backends whose Protocol declares them as
    properties, so this reads them off the class without constructing.
    """
    name = getattr(impl, "name", None)
    if not isinstance(name, str) or not name or "/" in name:
        return f"its capability name {name!r} is not a non-empty, '/'-free 'name' class attribute"
    description = getattr(impl, "description", None)
    if not isinstance(description, str):
        return f"its 'description' class attribute is {description!r}, not a string"
    return None


def _attributes_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 2b: the kind's other non-operation members are present.

    The metadata check above covers what EVERY capability row carries;
    this covers what a particular kind's consumers read. Presence only, not
    type: the framework's use of the value is the domain's business, and a
    class-level property (rather than a plain attribute) must still read as
    present.
    """
    missing = sorted(attr for attr in descriptor.required_attributes if getattr(impl, attr, _MISSING) is _MISSING)
    if missing:
        return f"it is missing the required {descriptor.kind} attributes: {', '.join(missing)}"
    return None


def _constructibility_error(impl: type) -> str | None:
    """Check 3: the side-effect-free constructibility check.

    Purely structural: it asks whether anything would stop the impl being
    constructed, and never calls ``impl(...)`` to find out. For the three ABC
    kinds ``isabstract`` is decisive, because each kind's own base declares
    its domain operations abstract (the shared ``Capability`` ABC declares
    none), so a concrete impl must have implemented them. For the Protocol
    kind there is nothing to leave abstract, and the metadata, attribute, and
    required-operation checks are the real structural enforcement.
    """
    if inspect.isabstract(impl):
        unimplemented = ", ".join(sorted(getattr(impl, "__abstractmethods__", ())))
        return f"it is abstract (unimplemented operations: {unimplemented})"
    return None


def _operations_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 4: every operation the framework depends on is present and
    callable."""
    missing = sorted(op for op in descriptor.required_operations if not callable(getattr(impl, op, None)))
    if missing:
        return f"it does not implement the required {descriptor.kind} operations: {', '.join(missing)}"
    return None


def _config_model_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 5: the config model the impl declares satisfies its kind's
    model contract.

    Read off ``config_model`` directly rather than through
    ``Capability.config_for``, and deliberately: conformance must not
    invoke implementation code. A capability whose methods run at several
    levels therefore declares its offered models as DATA this can read,
    which is the second reason the offered set is a mapping rather than a
    computation.

    The declaration is still OPTIONAL here, which is a bounded interim
    with one trigger: no shipped capability declares a model until its
    kind's commit lands, and the final commit of step 2.3 (the one that
    deletes the invoked ``validate`` contract) makes it required, because
    only then is a capability without a model genuinely unusable.
    """
    contract = descriptor.config_schema
    model = getattr(impl, "config_model", None)
    if model is None:
        return None
    if not isinstance(model, type) or not issubclass(model, contract.base):
        return (
            f"its config_model is {model!r}, which is not a {contract.base.__name__} subclass "
            f"(the {descriptor.kind} config contract)"
        )
    if not model_is_complete(model):
        return (
            f"its config_model {model.__name__} cannot be built (an unresolved annotation?), "
            f"so nothing could validate or extract references against it"
        )
    return _config_tag_error(descriptor, impl, model)


def _config_tag_error(descriptor: CapabilityKindDescriptor, impl: type, model: type[BaseModel]) -> str | None:
    """The tag half of check 5, for a kind whose config is dispatched by a
    discriminated union.

    An arm whose tag does not include the implementation's own name is
    UNADDRESSABLE from a manifest while everything else about it looks
    fine, which is exactly the class of silent failure registration
    conformance exists for.
    """
    discriminator = descriptor.config_schema.discriminator
    if discriminator is None:
        return None
    field = model.model_fields.get(discriminator)
    tags = _literal_values(field.annotation) if field is not None else ()
    name = getattr(impl, "name", None)
    if name not in tags:
        return (
            f"its config_model {model.__name__} does not tag itself {name!r}: the {descriptor.kind} "
            f"config is selected by a {discriminator!r} field typed "
            f"Literal[{name!r}], and this model declares {list(tags) or 'no such field'}"
        )
    return None


def _literal_values(annotation: object) -> tuple[object, ...]:
    """The members of a ``Literal[...]`` annotation, or nothing."""
    return get_args(annotation) if get_origin(annotation) is Literal else ()


def _version_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 6: the impl declares a contract version this build supports.

    Trivially satisfied while there is one version, which is the point: the
    declaration and the comparison both exist before the first incompatible
    change, so nothing has to be retrofitted when one arrives.

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
