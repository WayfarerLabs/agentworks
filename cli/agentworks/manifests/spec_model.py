"""The per-kind model assembly, shared by emission and the renderer.

A kind's row IS its spec model (see ``agentworks.declared_resource``), with
one exception that both derived surfaces have to handle identically: a kind
whose spec SELECTS a capability carries a
:class:`~agentworks.schema.CapabilityBlock` on the naming field (``name``
plus ``extra="allow"``), because the extra keys belong to another owner.
That is right for the row and useless to anyone describing the document: the
keys an operator may write inside ``platform:`` are the selected platform's,
and they are knowable, from the union over the registered implementations.

So :func:`spec_model` is the one answer to "what does a ``kind`` document's
``spec`` look like", and both the JSON Schema emitter (``manifests/emit.py``)
and the sample / field-reference renderer (``manifests/reference.py``) read
it. Two independent splices would be two answers to that question, and the
failure would be silent rather than loud: a rendered sample teaching a shape
the emitted schema does not describe.

Nothing here validates. The runtime authority stays ``manifests/decode.py``
plus ``manifests/envelope.py``; every fact assembled here is READ from the
place that owns it (``KIND_REGISTRY``, the kind's row, the capability-kind
descriptor, the capability config union).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import create_model

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ValidationError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema._shape import unwrap_optional

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor


def declarable_kinds() -> tuple[str, ...]:
    """Every kind a manifest may declare, sorted.

    Derived from the kind registry's per-kind category (ADR 0016) rather
    than listed anywhere: a kind is declarable exactly when a document of
    it can exist, which is exactly when it has a schema to emit and a
    sample to render. Sorted for a stable order matching
    ``agw resource kinds``, insulated from ``KIND_REGISTRY``'s import-order
    churn.
    """
    return tuple(sorted(name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"))


def row_model(kind: str) -> type[DeclaredResource]:
    """The kind's row class, which IS its spec model before the splice."""
    model = getattr(KIND_REGISTRY[kind], "model", None)
    if not (isinstance(model, type) and issubclass(model, DeclaredResource)):
        raise ValidationError(f"the {kind} kind declares no spec model, so there is nothing to describe")
    return model


def spec_model(kind: str) -> type[BaseModel]:
    """The model describing a ``kind`` document's ``spec`` block.

    The row itself for a kind that hosts no capability. For a kind that
    does, a subclass whose naming field is re-annotated to the union over
    the capability's registered implementations.

    Subclassing rather than merging generated fragments: this way a whole
    document is ONE pydantic generation call, so pydantic owns ``$defs``
    naming, collisions, and reference integrity, and the field-reference
    walk sees one ordinary model.

    The splice replaces the field's MODEL and nothing else about it. A row
    whose capability block is optional (``session-template``'s
    ``harness_integration: CapabilityBlock | None = None``) keeps its null
    arm, because ``harness_integration: null`` loads: dropping it would
    describe a document the loader accepts as invalid.
    """
    _seat_plugin_capabilities()
    row = row_model(kind)
    descriptor = hosted_capability(kind)
    if descriptor is None or descriptor.manifest_section is None:
        return row

    from agentworks.capabilities.config import capability_config_union

    field_name = descriptor.manifest_section.naming_field
    field = row.model_fields[field_name]
    union: Any = capability_config_union(descriptor.kind)
    _declared, optional = unwrap_optional(field.annotation)
    if optional:
        union = union | None
    return built_model(
        f"{class_name(kind)}Spec",
        base=row,
        doc=row.__doc__,
        # The row's own FieldInfo, so the authored description and default
        # survive onto the spliced field.
        fields={field_name: (union, field)},
    )


def hosted_capability(kind: str) -> CapabilityKindDescriptor | None:
    """The descriptor of the capability kind ``kind``'s spec selects by a
    TAGGED table, or ``None``.

    Classified on DISCRIMINATOR PRESENCE, never on whether the resulting
    annotation is still a union: ``Union[(X,)]`` collapses to ``X``, and a
    capability kind may have one registered implementation. A kind
    dispatched by map key (``secret-backend``) has no discriminator and no
    union to splice; ``emission-lld.md`` section 5 records why its per-key
    splice is not built.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    _seat_plugin_capabilities()
    for descriptor in capability_descriptors():
        section = descriptor.manifest_section
        if section is not None and section.host_kind == kind and descriptor.config_schema.discriminator is not None:
            return descriptor
    return None


def built_model(name: str, *, base: type[BaseModel], doc: str | None, fields: dict[str, Any]) -> type[BaseModel]:
    """``create_model`` with the field names supplied at runtime.

    One spelling for every caller, so ``__module__`` is set the same way at
    each (without it, the generated class claims pydantic's module and a
    repr or a traceback points at the wrong file).
    """
    return create_model(name, __base__=base, __module__=__name__, __doc__=doc, **fields)


def class_name(kind: str) -> str:
    """A kind identifier as a Python class-name fragment, which is what
    pydantic keys ``$defs`` by.

    ``capabilities/config.py`` computes the same string for the generated
    union class it names. Not shared with it: that module must not import
    anything under ``agentworks.resources`` (importing it loads every kind
    module, which loads every capability package), and this one is built on
    the kind registry.
    """
    return "".join(part.capitalize() for part in kind.split("-"))


def _seat_plugin_capabilities() -> None:
    """Make sure every installed plugin's capabilities are in their
    registries before this module reads one.

    Both surfaces built on this module promise to describe THIS host, and a
    surface that never builds a registry never triggers the import that
    seats a plugin's implementations. Before this call, ``agw resource
    schema vm-site`` emitted lima and wsl2 and silently omitted the three
    platform plugins that ship in-tree.
    """
    from agentworks.plugins.registration import seat_installed_plugins

    seat_installed_plugins()
