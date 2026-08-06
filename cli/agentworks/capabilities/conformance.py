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
from typing import TYPE_CHECKING

from agentworks.capabilities.base import Capability

if TYPE_CHECKING:
    from agentworks.capabilities.descriptor import CapabilityKindDescriptor


def conformance_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Why ``impl`` does not satisfy ``descriptor``'s contract, or ``None``
    when it does.

    Returns a reason rather than raising so each caller frames it in its own
    vocabulary (``register_plugin`` attributes it to the plugin; the table
    self-test reports it against the built-in).
    """
    return (
        _contract_error(descriptor, impl)
        or _metadata_error(impl)
        or _constructibility_error(impl)
        or _operations_error(descriptor, impl)
        or _version_error(descriptor, impl)
        # Per-slot model conformance (every provided slot model conforms to
        # its slot's model contract) is check five and is vacuous today: no
        # kind declares a slot until step 2.3 registers the per-kind config
        # models. Slot PRESENCE is the support claim, so there is never a
        # claimed-but-empty slot to check.
    )


def _contract_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 1: conformance to the implementation contract, branched by the
    contract's SHAPE.

    The four contracts are not uniform, and that is a code fact rather than
    an oversight. Three kinds derive from the ``Capability`` ABC and get a
    real nominal check. ``SecretBackend`` is a plain ``Protocol`` (not
    ``@runtime_checkable``) whose ``name`` / ``description`` / ``interactive``
    members are properties, so ``issubclass`` against it raises ``TypeError``
    even with the decorator added. A Protocol's real enforcement is
    structural anyway, and that is exactly what the metadata and
    required-operation checks below do, so its descriptor's
    ``implementation_contract`` is documentary.
    """
    if not issubclass(descriptor.implementation_contract, Capability):
        return None
    if not issubclass(impl, descriptor.implementation_contract):
        return (
            f"it does not derive from {descriptor.implementation_contract.__name__}, "
            f"the {descriptor.kind} implementation contract"
        )
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


def _constructibility_error(impl: type) -> str | None:
    """Check 3: the side-effect-free constructibility check.

    Purely structural: it asks whether anything would stop the impl being
    constructed, and never calls ``impl(...)`` to find out. For the three ABC
    kinds ``isabstract`` is decisive, because each kind's own base declares
    its domain operations abstract (the shared ``Capability`` ABC declares
    none), so a concrete impl must have implemented them. For the Protocol
    kind there is nothing to leave abstract, and the metadata and
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


def _version_error(descriptor: CapabilityKindDescriptor, impl: type) -> str | None:
    """Check 6: the impl declares a contract version this build supports.

    Trivially satisfied while there is one version, which is the point: the
    declaration and the comparison both exist before the first incompatible
    change, so nothing has to be retrofitted when one arrives.
    """
    declared = getattr(impl, "contract_version", None)
    if declared != descriptor.contract_version:
        return (
            f"it declares contract_version {declared!r}, but this build supports "
            f"{descriptor.kind} contract version {descriptor.contract_version}"
        )
    return None
