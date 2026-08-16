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

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import StringConstraints, create_model
from pydantic.fields import FieldInfo
from pydantic.json_schema import SkipJsonSchema

from agentworks.declared_resource import DeclaredResource, EnvelopeMetadata
from agentworks.errors import ValidationError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import AgwModel, NonEmptyStr
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
    projected: type[BaseModel] = row
    descriptor = hosted_capability(kind)
    if descriptor is not None:
        from agentworks.capabilities.config import capability_config_union

        field_name = descriptor.manifest_section.naming_field
        field = projected.model_fields[field_name]
        union: Any = capability_config_union(descriptor.kind)
        _declared, optional = unwrap_optional(field.annotation)
        if optional:
            union = union | None
        projected = built_model(
            f"{class_name(kind)}Spec",
            base=projected,
            doc=row.__doc__,
            # The row's own FieldInfo, so the authored description and default
            # survive onto the spliced field.
            fields={field_name: (union, field)},
        )

    from agentworks.capabilities.config import capability_mapping_union
    from agentworks.capabilities.descriptor import mapping_descriptors_for_host

    for mapping_descriptor in mapping_descriptors_for_host(kind):
        host = mapping_descriptor.mapping_host
        assert host is not None
        field = projected.model_fields[host.field_name]
        key: Any = Annotated[NonEmptyStr, host.key_reference]
        value: Any = capability_mapping_union(mapping_descriptor.kind)
        mapping: Any = dict[key, value]
        projected = built_model(
            f"{class_name(kind)}Spec",
            base=projected,
            doc=row.__doc__,
            fields={host.field_name: (mapping, field)},
        )
    return projected


def metadata_model(kind: str) -> type[BaseModel]:
    """The model describing a ``kind`` document's ``metadata`` block.

    Built from THE KIND's row rather than from ``EnvelopeMetadata``,
    because a kind may re-declare a metadata field: ``secret`` makes
    ``description`` required and ``admin-template`` defaults ``name``, and
    reading the row is what makes both differences show up here for free,
    in the emitted schema and in the rendered sample alike.

    Each field's ``SkipJsonSchema`` marker is dropped, and only that. The
    marker is what keeps these fields out of the SPEC surface (they are
    real fields of the row, and decode refuses one written inside
    ``spec``); here we are describing the block they DO belong to, so it is
    the one place the marker should not apply.
    """
    row = row_model(kind)
    # Declaration order, not sorted: ``name`` is what a document opens its
    # metadata block with, and a rendered sample that led with ``expires``
    # would be teaching an order nobody writes. ``METADATA_FIELDS`` is a
    # frozenset (it answers membership questions elsewhere), so the order
    # comes from the class that declares them.
    fields: dict[str, Any] = {name: _metadata_field(row, name) for name in EnvelopeMetadata.model_fields}
    return built_model(
        f"{class_name(kind)}Metadata",
        base=AgwModel,
        doc=f"The metadata block of a {kind} manifest document.",
        fields=fields,
    )


def _metadata_field(row: type[DeclaredResource], name: str) -> tuple[Any, FieldInfo]:
    """One metadata field as ``create_model`` wants it: the row's own
    annotation and a ``FieldInfo`` with ``SkipJsonSchema`` removed.

    A copy of the row's own ``FieldInfo`` with one entry filtered out of
    its metadata, rather than a fresh one built from the parts we happen to
    care about: a constraint a kind puts on a metadata field it overrides
    has to survive, and rebuilding would drop it silently.

    ``name`` picks up the kind's ``NAME_MAX_LENGTH`` where it declares one.
    That cap is applied by ``decode._check_declared_name`` to exactly the
    names a manifest carries (operator-written ones), so stating it here is
    faithful rather than an approximation. The CHARACTER rule is
    deliberately not expressed; see ``emission-lld.md``.

    The cap joins the metadata list rather than arriving as a second
    ``merge_field_infos`` argument, because merging resets every attribute
    the later ``FieldInfo`` does not set: verified, and it silently took
    the field's description with it, which is the hover text an operator
    reads on the one block every document carries.
    """
    field = row.model_fields[name]
    visible = FieldInfo.merge_field_infos(field)
    visible.metadata = [entry for entry in field.metadata if not _is_skip_marker(entry)]
    if name == "name" and row.NAME_MAX_LENGTH is not None:
        visible.metadata.append(StringConstraints(max_length=row.NAME_MAX_LENGTH))
    return field.annotation, visible


def _is_skip_marker(entry: object) -> bool:
    """Whether ``entry`` is the ``SkipJsonSchema`` annotation.

    The ignore is pydantic's shape, not a shortcut: ``SkipJsonSchema`` is
    an ``Annotated`` alias to a type checker and a dataclass to the
    interpreter, so there is no spelling of the name that is both a class
    to ``isinstance`` and a type to mypy.
    """
    return isinstance(entry, SkipJsonSchema)  # type: ignore[misc]


def hosted_capability(kind: str) -> CapabilityKindDescriptor | None:
    """The descriptor of the capability kind ``kind``'s spec selects by a
    TAGGED table, or ``None``.

    Every capability config contract is tagged, so hosting is the whole
    question: the kind named by some descriptor's ``manifest_section`` is
    the one whose spec carries a tagged table. Map-keyed consuming surfaces
    are projected separately from ``mapping_host`` by :func:`spec_model`.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    _seat_plugin_capabilities()
    for descriptor in capability_descriptors():
        if descriptor.manifest_section.host_kind == kind:
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

    **The IMPORT below is what seats**, because importing any submodule of
    ``agentworks.plugins`` runs the package body, and the package body
    registers every shipped plugin. The call that follows is therefore
    always a no-op, and it is here anyway for two reasons: it gives the
    requirement a name a reader can follow, and it is where a real re-seat
    would go if one is ever needed. What makes this LAZY is the placement,
    not the call: the import lives inside a function, so importing this
    module does not drag in every plugin.
    """
    from agentworks.plugins.registration import seat_installed_plugins

    seat_installed_plugins()
