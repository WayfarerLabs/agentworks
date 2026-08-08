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

The operator-facing half of the soundness contract (what an editor checks
versus what loading checks, and why that gap leans the way it does) is in
``docs/guides/resources.md`` under "Editing manifests with schema
support".
"""

from __future__ import annotations

import json
import os.path
from pathlib import PurePath
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Union, cast

from pydantic import Field, create_model
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue

from agentworks.errors import ValidationError
from agentworks.manifests.envelope import API_VERSION
from agentworks.manifests.spec_model import class_name, declarable_kinds, metadata_model, spec_model
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import AgwModel, AgwRootModel

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from pydantic import BaseModel
    from pydantic_core import core_schema

#: Where written schemas live, relative to the resources directory.
#: Dot-prefixed on purpose: ``loader._iter_manifest_files`` prunes
#: dot-directories, so a generated artifact can never be mistaken for a
#: manifest.
SCHEMA_DIRNAME: Final = ".schema"

#: The any-kind document schema's filename.
ENVELOPE_SCHEMA_FILENAME: Final = "manifest.schema.json"

#: The plain scalars the LOADER reads as a boolean but a schema-aware
#: editor hands the validator as a string.
#:
#: The loader is pyyaml's ``SafeLoader``, which resolves YAML 1.1: ``no``,
#: ``off`` and friends are booleans to it. Every editor that consumes these
#: schemas parses YAML 1.2 core, where only ``true``/``false`` (in three
#: casings each) are booleans and the rest stay strings. So an operator who
#: writes the perfectly valid ``verify_ssl: no`` is handing a JSON Schema
#: validator the STRING ``"no"``, and a bare ``"type": "boolean"`` would
#: red-underline configuration that loads.
#:
#: Derived by running both parsers over every casing of every candidate
#: word rather than transcribed from either spec; ``tests/manifests/
#: test_emit.py`` re-derives the pyyaml half so a pyyaml resolver change
#: cannot leave this list quietly short.
YAML_11_ONLY_BOOLEANS: Final = (
    "yes",
    "Yes",
    "YES",
    "no",
    "No",
    "NO",
    "on",
    "On",
    "ON",
    "off",
    "Off",
    "OFF",
)

#: The plain scalars the LOADER reads as an integer but a schema-aware
#: editor hands the validator as a string, as an ECMA-262 ``pattern``.
#:
#: The integer counterpart of :data:`YAML_11_ONLY_BOOLEANS`, and a pattern
#: rather than a tuple for one reason: the boolean disagreement is twelve
#: words, while this one is infinite (any number of underscores, any
#: sexagesimal group). The membership rule is the same, though, and so is
#: the derivation. It is the language pyyaml's own ``tag:yaml.org,2002:int``
#: implicit resolver accepts, minus the spellings a YAML 1.2 core parser
#: also resolves to a number; ``tests/manifests/test_emit.py`` rebuilds it
#: from that live resolver so a pyyaml change cannot leave it quietly
#: short, the way ``YAML_11_ONLY_BOOLEANS`` is re-derived there.
#:
#: What that leaves, measured against both parsers rather than read off
#: the specs: underscore separators (``8_192``), sexagesimal (``1:30``),
#: binary (``0b1010``), and SIGNED hex (``+0x1F``, which 1.2 core does not
#: take a sign on). ``8_192`` in a ``memory`` field is the one an operator
#: actually types.
#:
#: The subtraction is what keeps a QUOTED ``"5"`` flagged. Unquoted ``5``
#: reaches a 1.2 editor as a number, so it never consults the string arm;
#: admitting plain decimals here would only stop the schema noticing a
#: quoted integer, which the strict loader does refuse. The residual that
#: remains is the boolean one exactly: a quoted ``"8_192"`` is the same
#: parsed instance as a bare ``8_192``, so no schema can separate them.
#:
#: Two disagreements this CANNOT reach are recorded in the emission LLD
#: beside it, because neither is a type error: leading-zero octal
#: (``010`` is 8 to the loader and 10 to the editor, the same JSON type
#: with a different value), and ``0o17`` / ``1e3`` (a number to the
#: editor, a string the strict loader refuses, which is the permitted
#: under-reporting direction).
YAML_11_ONLY_INTEGERS: Final = (
    r"^(?!(?:[-+]?[0-9]+|0x[0-9a-fA-F]+|0o[0-7]+)$)"
    r"(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)"
    r"|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$"
)


class _ManifestJsonSchema(GenerateJsonSchema):
    """Pydantic's generator, with scalars widened to what YAML spells.

    A JSON Schema never sees YAML; it sees whatever the editor's parser
    produced. The loader's parser and the editor's parser disagree about
    which plain scalars are booleans and which are integers (see
    :data:`YAML_11_ONLY_BOOLEANS` and :data:`YAML_11_ONLY_INTEGERS`), and
    that disagreement is not something any per-field annotation can fix,
    because it is a property of the PARSE rather than of the field. So it
    is corrected in the one place every boolean and every integer in every
    emitted schema comes through.

    Overriding the generator rather than post-walking the emitted dict so
    a field added anywhere later (a new kind, a plugin's capability
    config, a nested block) is covered without anyone remembering to do
    it.

    Floats would need the same treatment for the same reason: pyyaml
    reads ``1_000.5`` as a float where 1.2 core reads a string. There is
    no ``float_schema`` override because the emitted surface holds no
    float, and writing one for a field that does not exist would be a
    guess at its constraints. ``test_the_float_gap_is_still_unreachable``
    fails the day one appears.
    """

    def bool_schema(self, schema: core_schema.BoolSchema) -> JsonSchemaValue:
        """A boolean, or the string an editor saw one of its YAML 1.1
        spellings as.

        ``pattern`` rather than ``enum``, deliberately: JSON Schema
        applies ``pattern`` only to string instances, so one flat schema
        covers both types, and an editor draws its completions from
        ``enum`` but not from ``pattern``. Listing the spellings as an
        enum would offer twelve odd ways to say ``true`` on every boolean
        field in the surface.
        """
        return {
            "type": ["boolean", "string"],
            "pattern": f"^(?:{'|'.join(YAML_11_ONLY_BOOLEANS)})$",
        }

    def int_schema(self, schema: core_schema.IntSchema) -> JsonSchemaValue:
        """An integer, or the string an editor saw one of its YAML 1.1
        spellings as.

        The same correction ``bool_schema`` makes, for the same reason, at
        the other place the two parsers disagree: ``memory: 8_192`` is
        ``8192`` to the loader and the string ``"8_192"`` to the editor,
        and a bare ``"type": "integer"`` red-underlines it.

        Widening the generator's OWN answer rather than returning a
        literal, which ``bool_schema`` can afford to do because a boolean
        carries no constraints. An integer does: ``cpus`` is
        ``exclusiveMinimum: 0`` and ``template_vmid`` carries an
        ``examples``, and both have to survive.

        Those numeric constraints then apply to the integer arm only,
        because JSON Schema's numeric keywords ignore string instances. So
        ``cpus: 0_0`` is accepted here and refused by the loader. That is
        the under-approximation this module is built on rather than a
        hole: the alternative is a second copy of every bound restated as
        a regex, which would be a new place to be wrong about ``cpus``.
        """
        return {**super().int_schema(schema), "type": ["integer", "string"], "pattern": YAML_11_ONLY_INTEGERS}


def schema_filename(kind: str) -> str:
    """The filename ``kind``'s document schema is written under."""
    return f"{kind}.schema.json"


def document_schema(kind: str) -> dict[str, Any]:
    """The JSON Schema for one manifest document of ``kind``.

    The unit is the whole document (``apiVersion`` / ``kind`` /
    ``metadata`` / ``spec``) rather than the kind's ``spec`` mapping
    alone, because a yaml-language-server modeline associates a schema
    with a FILE and a file holds documents. A spec-only schema would have
    nothing that could point at it.
    """
    _require_emittable(kind)
    return _with_dialect(_document_model(kind).model_json_schema(schema_generator=_ManifestJsonSchema))


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
    arms = tuple(_document_model(kind) for kind in declarable_kinds())
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
    return _with_dialect(root.model_json_schema(schema_generator=_ManifestJsonSchema))


def schema_set() -> dict[str, dict[str, Any]]:
    """Every schema :func:`write_schema_set` writes, keyed by filename.

    The envelope plus one per declarable kind. Always the whole set: a
    partial set would leave some manifest's modeline pointing at a file
    that does not exist, which is a red banner in the operator's editor
    rather than a missing feature.
    """
    schemas = {ENVELOPE_SCHEMA_FILENAME: envelope_schema()}
    schemas.update({schema_filename(kind): document_schema(kind) for kind in declarable_kinds()})
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


MODELINE_PREFIX = "# yaml-language-server: $schema="
"""What a modeline starts with, for recognizing one already in a file.

A file's first line either is one of these or the file has none; nothing
reads further than the prefix to decide.
"""


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
    filename = ENVELOPE_SCHEMA_FILENAME if kind is None else schema_filename(kind)
    target = resources_dir / SCHEMA_DIRNAME / filename
    relative = os.path.relpath(target, start=manifest_path.parent)
    return f"{MODELINE_PREFIX}{PurePath(relative).as_posix()}"


def restamped_modeline(
    text: str, *, manifest_path: Path, resources_dir: Path, kinds: Collection[str]
) -> tuple[str, bool]:
    """``text`` with its modeline corrected for ``kinds`` being added to it.

    Returns ``(text, changed)``. Split out of ``resource sample --write``,
    its one caller, because the rule belongs beside ``modeline`` (the
    function that decides what a correct one says) rather than inside the
    writer. The way a modeline goes wrong is always the same: a file
    stamped for one kind stops being a one-kind file, and the editor goes
    on checking the new documents against the first kind's shape,
    red-underlining configuration the loader accepts.

    Two rules, and the difference between them is the whole reason this
    can be done at all:

    - A file with NO modeline is returned untouched. Adding one means
      inserting at line 1 and shifting every line number the operator and
      every stored ``declared_at`` already know, which is a bigger change
      to their file than an append was asked to make.
    - A modeline that is already there is REPLACED in place, which moves
      no line at all.

    Nothing parses the body to decide. The existing modeline is the record
    of what the file was for, and for a sample append the body is mostly
    commented-out text no YAML parser would report a kind for anyway.

    Callers must ensure the schema set exists, since the line may now name
    a schema file the file did not refer to before.
    """
    first, newline, rest = text.partition("\n")
    if not first.startswith(MODELINE_PREFIX):
        return text, False
    unique = set(kinds)
    only = unique.pop() if len(unique) == 1 else None
    if first == modeline(manifest_path=manifest_path, resources_dir=resources_dir, kind=only):
        return text, False
    envelope = modeline(manifest_path=manifest_path, resources_dir=resources_dir, kind=None)
    if first == envelope:
        return text, False
    return f"{envelope}{newline}{rest}", True


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
    return create_model(
        f"{class_name(kind)}Document",
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
            metadata_model(kind),
            Field(description="The fields every declared resource carries, whatever its kind."),
        ),
        spec=(
            # Required but nullable, matching the envelope exactly: the key
            # must be present, and `spec:` with nothing after it reads as an
            # empty mapping rather than an error. Dropping the null arm
            # would make the schema stricter than the loader, which is the
            # one direction this module may not go.
            spec_model(kind) | None,
            Field(description=f"The {kind} kind's own fields."),
        ),
    )


def _require_emittable(kind: str) -> None:
    known = ", ".join(declarable_kinds())
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

    Read off the generator these schemas are actually produced by rather
    than written as a literal, so the declared dialect is always the one
    the schema was generated for.
    """
    return {"$schema": _ManifestJsonSchema.schema_dialect, **schema}
