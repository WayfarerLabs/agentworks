"""JSON Schema emission: one document schema per kind, plus the any-kind one.

Everything here derives from the same models the loader validates against,
so an emitted schema is not a second description of a manifest, it is the
first one recompiled. ``model_json_schema`` is the whole mechanism: this
module assembles the models that describe a DOCUMENT and lets pydantic
write the schema.

**The soundness contract, which shapes every choice below: an emitted
schema is a sound UNDER-approximation of what the loader accepts.**
Everything the schema rejects, the loader rejects too; the loader rejects
more (validators, name character rules, the capability config of an
implementation this host has not registered). The direction is
deliberate. A permissive schema costs an operator a completion; a strict
one red-underlines valid config in their editor, confidently and wrongly,
which is worse than shipping no schema at all. So a rule that JSON Schema
cannot state faithfully is left out rather than approximated.

Emission is a SIBLING of the field-reference stream
(:func:`~agentworks.schema.iter_field_docs`), not a consumer of it: both
derive from the models, because deriving JSON Schema from ``FieldDoc``
would mean writing a second schema generator. The reference markers' own
``__get_pydantic_json_schema__`` hook is what carries ``x-agw-ref`` into
emitted schema, so the two derivations cannot disagree about what a field
means.

See ``docs/sdd/2026-07-31-declarative-schema/emission-lld.md`` for the
design record, including what is deliberately not expressed and why.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Union, cast

from pydantic import Field, create_model
from pydantic.fields import FieldInfo
from pydantic.json_schema import GenerateJsonSchema, SkipJsonSchema

from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource
from agentworks.errors import ValidationError
from agentworks.manifests.envelope import API_VERSION
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import AgwModel, AgwRootModel
from agentworks.schema._shape import unwrap_optional

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor

#: Where written schemas live, relative to the resources directory.
#: Dot-prefixed on purpose: ``loader._iter_manifest_files`` prunes
#: dot-directories, so a generated artifact can never be mistaken for a
#: manifest.
SCHEMA_DIRNAME: Final = ".schema"

#: The any-kind document schema's filename.
ENVELOPE_SCHEMA_FILENAME: Final = "manifest.schema.json"


def schema_filename(kind: str) -> str:
    """The filename ``kind``'s document schema is written under."""
    return f"{kind}.schema.json"


def emittable_kinds() -> tuple[str, ...]:
    """Every kind a manifest may declare, sorted.

    The same derivation the sample surface uses (the kind registry's
    per-kind category), rather than a list here: a kind is emittable
    exactly when it is declarable, because that is exactly when a
    document of it can exist.
    """
    return tuple(sorted(name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"))


def document_schema(kind: str) -> dict[str, Any]:
    """The JSON Schema for one manifest document of ``kind``.

    The unit is the whole document (``apiVersion`` / ``kind`` /
    ``metadata`` / ``spec``) rather than the kind's ``spec`` mapping
    alone, because a yaml-language-server modeline associates a schema
    with a FILE and a file holds documents. A spec-only schema would have
    nothing that could point at it.
    """
    _require_emittable(kind)
    return _with_dialect(_document_model(kind).model_json_schema())


def envelope_schema() -> dict[str, Any]:
    """The JSON Schema for a manifest document of ANY kind.

    A real pydantic discriminated union over the per-kind document models,
    each of which pins its own ``kind`` literal, so pydantic writes the
    ``oneOf`` and the discriminator mapping and this module writes
    neither.

    Self-contained rather than a set of ``$ref``s to the sibling files:
    cross-file references would make each file useless on its own and
    make an editor's view of a manifest depend on relative-path
    resolution. The duplication is the right trade for a generated
    artifact nobody hand-edits.
    """
    arms = tuple(_document_model(kind) for kind in emittable_kinds())
    if not arms:
        raise ValidationError("no declarable kinds are registered, so there is no manifest schema to emit")
    # Built from a runtime value (the kind registry), so mypy cannot see a
    # type here, exactly as ``capabilities.config._build_union`` cannot.
    union: Any = Annotated[Union[arms], Field(discriminator="kind")]  # noqa: UP007
    root = cast(
        "type[BaseModel]",
        type(
            "ManifestDocument",
            (AgwRootModel[union],),
            {"__module__": __name__, "__doc__": "One agentworks resource manifest document, of any declarable kind."},
        ),
    )
    return _with_dialect(root.model_json_schema())


def schema_set() -> dict[str, dict[str, Any]]:
    """Every schema :func:`write_schema_set` writes, keyed by filename.

    The envelope plus one per declarable kind. Always the whole set: a
    partial set would leave some manifest's modeline pointing at a file
    that does not exist, which is a red banner in the operator's editor
    rather than a missing feature.
    """
    schemas = {ENVELOPE_SCHEMA_FILENAME: envelope_schema()}
    schemas.update({schema_filename(kind): document_schema(kind) for kind in emittable_kinds()})
    return schemas


def write_schema_set(schema_dir: Path) -> tuple[Path, ...]:
    """Write the whole schema set into ``schema_dir``; return the paths.

    Overwrites unconditionally. These are derived artifacts whose only
    correct content is what the current registry (plugins included)
    implies, so a stale file left in place would associate an operator's
    manifests with a schema that no longer describes them.
    """
    schema_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, schema in schema_set().items():
        path = schema_dir / filename
        path.write_text(schema_json(schema), encoding="utf-8")
        written.append(path)
    return tuple(written)


def schema_json(schema: dict[str, Any]) -> str:
    """One schema as the text that goes in a file or on stdout.

    Two-space indent and a trailing newline: these land in an operator's
    config directory, where they are as likely to be read (and diffed, and
    committed) as consumed.
    """
    return json.dumps(schema, indent=2) + "\n"


def modeline(*, manifest_path: Path, resources_dir: Path, kind: str | None) -> str:
    """The yaml-language-server modeline for a manifest file, without its
    trailing newline.

    ``kind`` names the ONE kind the file holds, or ``None`` for a file
    that holds several. A single-kind file gets its own schema rather than
    the envelope, because an editor that ignores the OpenAPI-style
    ``discriminator`` keyword localizes an error far better against one
    document shape than against a thirteen-arm ``oneOf``.

    The path is relative to the manifest itself, so the resources
    directory stays portable: moving or copying it does not break the
    association.
    """
    import os.path
    from pathlib import PurePath

    filename = ENVELOPE_SCHEMA_FILENAME if kind is None else schema_filename(kind)
    target = resources_dir / SCHEMA_DIRNAME / filename
    relative = os.path.relpath(target, start=manifest_path.parent)
    return f"# yaml-language-server: $schema={PurePath(relative).as_posix()}"


# -- Model assembly --------------------------------------------------------


def _document_model(kind: str) -> type[BaseModel]:
    """The model describing one whole ``kind`` document.

    Emission-only. ``manifests/envelope.py`` keeps its hand-rolled runtime
    validation: its errors are the best in the codebase and it must be
    able to NAME the kind before any kind model is in hand, so replacing
    it would trade good errors for uniformity nobody asked for. That is
    not two authorities, because every fact here is READ from the place
    that already owns it (``API_VERSION``, ``KIND_REGISTRY``,
    ``METADATA_FIELDS``, the kind's row) rather than restated. The one
    fact this model does own is the top-level key set, which
    ``tests/manifests/test_emit.py`` pins against the envelope's own.
    """
    row = _row_model(kind)
    return create_model(
        f"{_class_name(kind)}Document",
        __base__=AgwModel,
        __module__=__name__,
        __doc__=f"One {kind} resource manifest document.",
        apiVersion=(
            Literal[API_VERSION],
            Field(description="The manifest schema version. Always `agentworks/v1`."),
        ),
        kind=(
            Literal[kind],
            Field(description=KIND_REGISTRY[kind].description),
        ),
        metadata=(
            _metadata_model(kind, row),
            Field(description="The fields every declared resource carries, whatever its kind."),
        ),
        spec=(
            # Required but nullable, matching the envelope exactly: the key
            # must be present, and `spec:` with nothing after it reads as an
            # empty mapping rather than an error. Dropping the null arm
            # would make the schema stricter than the loader, which is the
            # one direction this module may not go.
            _spec_model(kind, row) | None,
            Field(description=f"The {kind} kind's own fields."),
        ),
    )


def _metadata_model(kind: str, row: type[DeclaredResource]) -> type[BaseModel]:
    """The model describing a ``kind`` document's ``metadata`` block.

    Built from THE KIND's row rather than from ``EnvelopeMetadata``,
    because a kind may re-declare a metadata field: ``secret`` makes
    ``description`` required and ``admin-template`` defaults ``name``, and
    reading the row is what makes both differences show up here for free.

    Each field's ``SkipJsonSchema`` marker is dropped, and only that.
    The marker is what keeps these fields out of the SPEC surface (they
    are real fields of the row, and decode refuses one written inside
    ``spec``); here we are describing the block they DO belong to, so it
    is the one place the marker should not apply.
    """
    fields: dict[str, Any] = {name: _metadata_field(row, name) for name in sorted(METADATA_FIELDS)}
    return _built_model(
        f"{_class_name(kind)}Metadata",
        base=AgwModel,
        doc=f"The metadata block of a {kind} manifest document.",
        fields=fields,
    )


def _metadata_field(row: type[DeclaredResource], name: str) -> tuple[Any, FieldInfo]:
    """One metadata field as ``create_model`` wants it: the row's own
    annotation and a ``FieldInfo`` with ``SkipJsonSchema`` removed.

    A copy of the row's own ``FieldInfo`` with one entry filtered out of
    its metadata, rather than a fresh one built from the parts we happen
    to care about: a constraint a kind puts on a metadata field it
    overrides has to survive, and rebuilding would drop it silently.

    ``name`` picks up the kind's ``NAME_MAX_LENGTH`` where it declares
    one. That cap is applied by ``decode._check_declared_name`` to
    exactly the names a manifest carries (operator-written ones), so
    stating it here is faithful rather than an approximation. The
    CHARACTER rule is deliberately not emitted; see the LLD.
    """
    field = row.model_fields[name]
    visible = FieldInfo.merge_field_infos(field)
    visible.metadata = [entry for entry in field.metadata if not _is_skip_marker(entry)]
    if name == "name" and row.NAME_MAX_LENGTH is not None:
        visible = FieldInfo.merge_field_infos(visible, FieldInfo(max_length=row.NAME_MAX_LENGTH))
    return field.annotation, visible


def _is_skip_marker(entry: object) -> bool:
    """Whether ``entry`` is the ``SkipJsonSchema`` annotation.

    The ignore is pydantic's shape, not a shortcut: ``SkipJsonSchema`` is
    an ``Annotated`` alias to a type checker and a dataclass to the
    interpreter, so there is no spelling of the name that is both a class
    to ``isinstance`` and a type to mypy.
    """
    return isinstance(entry, SkipJsonSchema)  # type: ignore[misc]


def _spec_model(kind: str, row: type[DeclaredResource]) -> type[BaseModel]:
    """The model describing a ``kind`` document's ``spec`` block.

    The row itself for a kind that hosts no capability. For a kind that
    does, a subclass whose naming field is re-annotated to the union over
    the capability's registered implementations, because what the row
    carries there is a ``CapabilityBlock``: ``name`` plus ``extra="allow"``,
    since the extra keys belong to another owner and are checked at
    finalize against that owner's model. That is right for the row and
    says nothing useful in a schema.

    Subclassing rather than merging two ``model_json_schema`` outputs and
    rewriting ``$ref`` strings: this way the whole document is ONE
    generation call, so pydantic owns ``$defs`` naming, collisions, and
    reference integrity. A mis-merge would be a ``$ref`` resolving to the
    wrong model, which is a silent wrong answer rather than a crash.

    The splice replaces the field's MODEL and nothing else about it. A row
    whose capability block is optional (``session-template``'s
    ``harness_integration: CapabilityBlock | None = None``) keeps its null
    arm, because ``harness_integration: null`` loads: dropping it would
    reject a document the loader accepts, and would leave the property
    carrying ``default: null`` against a subschema that refuses null, so
    an editor's insert-default would produce config the same schema flags.
    """
    descriptor = _hosted_capability(kind)
    if descriptor is None or descriptor.manifest_section is None:
        return row

    from agentworks.capabilities.config import capability_config_union

    field_name = descriptor.manifest_section.naming_field
    field = row.model_fields[field_name]
    union: Any = capability_config_union(descriptor.kind)
    _declared, optional = unwrap_optional(field.annotation)
    if optional:
        union = union | None
    return _built_model(
        f"{_class_name(kind)}Spec",
        base=row,
        doc=row.__doc__,
        # The row's own FieldInfo, so the authored description and default
        # survive onto the spliced property.
        fields={field_name: (union, field)},
    )


def _built_model(name: str, *, base: type[BaseModel], doc: str | None, fields: dict[str, Any]) -> type[BaseModel]:
    """``create_model`` with the field names supplied at runtime.

    One spelling for both callers, so ``__module__`` is set the same way
    at each (without it, the generated class claims pydantic's module and
    a repr or a traceback points at the wrong file).
    """
    return create_model(name, __base__=base, __module__=__name__, __doc__=doc, **fields)


def _hosted_capability(kind: str) -> CapabilityKindDescriptor | None:
    """The descriptor of the capability kind ``kind``'s spec selects by a
    TAGGED table, or ``None``.

    Classified on DISCRIMINATOR PRESENCE, never on whether the resulting
    annotation is still a union: ``Union[(X,)]`` collapses to ``X``, and a
    capability kind with one registered implementation is the shipped case
    for two of the three tagged kinds. A kind dispatched by map key
    (secret-backend) has no discriminator and no union to splice; the LLD
    records why its per-key splice is not built.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    for descriptor in capability_descriptors():
        section = descriptor.manifest_section
        if section is not None and section.host_kind == kind and descriptor.config_schema.discriminator is not None:
            return descriptor
    return None


def _row_model(kind: str) -> type[DeclaredResource]:
    """The kind's row class, which IS its spec model."""
    model = getattr(KIND_REGISTRY[kind], "model", None)
    if not (isinstance(model, type) and issubclass(model, DeclaredResource)):
        raise ValidationError(f"the {kind} kind declares no spec model, so there is no schema to emit")
    return model


def _require_emittable(kind: str) -> None:
    known = ", ".join(emittable_kinds())
    handler = KIND_REGISTRY.get(kind)
    if handler is None:
        raise ValidationError(
            f"unknown kind {kind!r}",
            entity_kind="resource",
            entity_name=kind,
            hint=f"known kinds: {known}",
        )
    if handler.category != "declarable":
        # Same shape as the sample surface's refusal: a capability kind is
        # listed by `resource kinds`, so a curious operator will ask for
        # one here, and it carries no manifest to describe.
        raise ValidationError(
            f"{kind!r} is a capability kind; it is declared in code, not in a manifest, so it has no schema",
            entity_kind="resource",
            entity_name=kind,
            hint=f"declarable kinds: {known}",
        )


def _with_dialect(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with its ``$schema`` dialect declared, first.

    Read off pydantic's own generator rather than written as a literal, so
    the declared dialect is always the one the schema was generated for.
    """
    return {"$schema": GenerateJsonSchema.schema_dialect, **schema}


def _class_name(kind: str) -> str:
    """A kind identifier as a Python class-name fragment, which is what
    pydantic keys ``$defs`` by.

    ``capabilities/config.py`` computes the same string for the generated
    union class it names. Not shared: that module must not import anything
    under ``agentworks.resources`` (importing it loads every kind module,
    which loads every capability package), and this one is built on the
    kind registry, so the only shared home would be a new leaf module for
    one expression.
    """
    return "".join(part.capitalize() for part in kind.split("-"))
