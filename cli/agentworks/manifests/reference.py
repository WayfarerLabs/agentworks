"""What a kind or a capability implementation accepts, collected once.

The service layer behind BOTH schema-derived operator surfaces: the
generated sample (``manifests/skeleton.py``, reached through
``agw resource sample``) and the field reference
(``manifests/describe.py``, reached through ``agw resource describe-kind``).
The onboarding effort's ``agw guide`` calls the same functions for its
``FieldReference`` and ``Sample`` blocks rather than scraping rendered CLI
output, which is why the records below carry facts and not text.

One collector, two presenters. The alternative (each surface walking the
stream itself) is two walks that agree until one of them learns something,
and the thing they would disagree about is which fields exist.

**Nothing here loads config, builds a registry, or constructs a
capability.** It reads models and the capability code registries, so it
answers for a plugin whose config has not opted in (enablement is a
property of a published ROW), and it answers on a host whose config does
not parse.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, RootModel

from agentworks.errors import StateError, ValidationError
from agentworks.manifests.spec_model import declarable_kinds, hosted_capability, metadata_model, spec_model
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import MAPPING_KEY, SEQUENCE_ELEMENT, UNSET, iter_field_docs, render_type
from agentworks.topics import prose_of

if TYPE_CHECKING:
    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.schema import FieldDoc

#: What a value looks like when the model says only its type. Angle
#: brackets on purpose: a sample line carrying one is a line the operator
#: has to fill in, and it should look like one.
_PLACEHOLDERS = {
    "string": "<string>",
    "integer": "<integer>",
    "number": "<number>",
    "boolean": "<true or false>",
    "timestamp": "<2026-01-01T00:00:00Z>",
    "date": "<2026-01-01>",
}

#: The stand-in key for a table whose keys the operator chooses.
_PLACEHOLDER_KEY = "<key>"


@dataclass(frozen=True, kw_only=True)
class Alternative:
    """One of the several things that could go where one thing goes: an
    implementation of a capability kind."""

    name: str
    """The tag that selects it (``"azure-vm"``)."""

    summary: str | None
    """Its one-line description, so a list of alternatives says what each
    one IS without a second lookup."""

    target: str | None
    """The address that describes it in full (``"vm-platform/azure-vm"``),
    or ``None`` for a union that is not a capability."""


@dataclass(frozen=True, kw_only=True)
class FieldEntry:
    """One field, with the tree and the derived facts both presenters
    need.

    Carries its :class:`~agentworks.schema.FieldDoc` verbatim rather than
    replacing it: a presenter that wants the annotation, the constraints,
    or the reference marker reads them off ``doc``, and the derived
    properties below are a convenience it may ignore.
    """

    doc: FieldDoc
    children: tuple[FieldEntry, ...]
    """The fields inside this one: a nested block's, a collection
    element's, or the fields of the union arm this entry renders."""

    alternatives: tuple[Alternative, ...]
    """The arms of a discriminated union, when this field is one."""

    rendered: str | None
    """Which alternative ``children`` came from. One is expanded and the
    rest are listed, because a document holds one."""

    @property
    def name(self) -> str:
        """The key an operator writes, or the placeholder segment standing
        for any element of a collection."""
        return self.doc.path[-1]

    @property
    def writable(self) -> bool:
        """Whether an operator MUST write this field.

        Not simply ``required``: a field with an owner-templated default is
        required to pydantic and optional to the operator, because the
        model fills it from its owner when it is omitted (emitted schema
        makes the same subtraction, for the same reason). A skeleton that
        listed it as a line to fill in would be telling them to name a
        secret the framework already names.
        """
        return self.doc.required and self.doc.default_template is None

    @property
    def type_label(self) -> str:
        """The type as an operator reads it. A presenter may replace this
        with its own rendering of ``doc.annotation``."""
        return render_type(self.doc.annotation)

    @property
    def sample_value(self) -> object:
        """The value a generated sample writes for this field.

        In order: the author's first example, the one value a closed field
        can hold (a union arm's tag), the field's own default where it is
        worth showing, and finally a placeholder derived from the type.
        ``UNSET`` for a field whose content is its children's.

        An EMPTY default (``[]``, ``{}``, ``""``) is skipped in favor of the
        placeholder: it is the honest default and it teaches nothing, and a
        commented ``repos: []`` says less than a commented
        ``repos: [<string>]`` about what may go there.
        """
        if self.doc.examples:
            return self.doc.examples[0]
        if self.doc.choices:
            return _wire_value(self.doc.choices[0])
        if self.children:
            return UNSET
        if worth_showing(self.doc.default):
            return self.doc.default
        return _placeholder(self.type_label)


@dataclass(frozen=True, kw_only=True)
class SchemaReference:
    """Everything both surfaces show about one documented thing."""

    target: str
    """What an operator types to get this: ``"vm-site"`` or
    ``"vm-platform/lima"``."""

    kind: str
    implementation: str | None
    category: Literal["declarable", "capability"]

    title: str | None
    """The authored display title, when prose was written."""

    summary: str | None
    """The one-line description the kind or the implementation declares.
    This is the topic contract's ``summary``: it is not authored a second
    time as prose (see :mod:`agentworks.topics`)."""

    overview: str | None
    """The authored markdown, when prose was written."""

    metadata: tuple[FieldEntry, ...]
    """The document's ``metadata`` block. Empty for a capability target,
    whose config is written inside another kind's document."""

    spec: tuple[FieldEntry, ...]
    """The kind's ``spec`` fields, or the implementation's config fields."""

    alternatives: tuple[Alternative, ...]
    """The implementations of a capability KIND, when the target is one."""

    root_value: FieldEntry | None
    """Set when the config IS a value rather than a mapping (a secret
    backend's per-secret mapping may be a bare string), in which case
    ``spec`` is empty and this describes what may be written."""


def describable_targets() -> tuple[str, ...]:
    """Every target :func:`reference_for` accepts, sorted, for completion
    and for the error that lists them."""
    from agentworks.capabilities.config import registered_implementations

    targets = list(declarable_kinds())
    for descriptor in _capability_descriptors():
        targets.append(descriptor.kind)
        targets.extend(f"{descriptor.kind}/{name}" for name in registered_implementations(descriptor.kind))
    return tuple(sorted(targets))


def reference_for(target: str) -> SchemaReference:
    """The reference for ``KIND`` or ``KIND/NAME``.

    One grammar for both, and the same one ``agw resource describe`` and
    ``agw resource edit`` take, because ``vm-platform/lima`` names the same
    thing here as it does there: this surface answers what its config
    looks like, that one answers what the registry row says.
    """
    kind, slash, name = target.partition("/")
    if slash and not name:
        raise ValidationError(
            f"expected KIND or KIND/NAME, got {target!r}",
            hint="Example: agw resource describe-kind vm-platform/lima",
        )
    handler = KIND_REGISTRY.get(kind)
    if handler is None:
        raise ValidationError(
            f"unknown kind {kind!r}",
            entity_kind="resource",
            entity_name=kind,
            hint=f"known kinds: {', '.join(sorted(KIND_REGISTRY))}",
        )
    if handler.category == "declarable":
        if name:
            raise ValidationError(
                f"{kind!r} is a declarable kind, so it has no implementations to select",
                entity_kind="resource",
                entity_name=target,
                hint=f"Drop the name: `agw resource describe-kind {kind}`.",
            )
        return kind_reference(kind)
    return capability_kind_reference(kind) if not name else implementation_reference(kind, name)


def kind_reference(kind: str) -> SchemaReference:
    """The field reference for one declarable kind: its document's
    ``metadata`` and ``spec`` blocks."""
    descriptor = hosted_capability(kind)
    return SchemaReference(
        target=kind,
        kind=kind,
        implementation=None,
        category="declarable",
        **_prose_of(KIND_REGISTRY[kind]),
        metadata=_named(_entries(metadata_model(kind)), kind),
        spec=_entries(spec_model(kind), capability_kind=None if descriptor is None else descriptor.kind),
        alternatives=(),
        root_value=None,
    )


def capability_kind_reference(kind: str) -> SchemaReference:
    """The index for one capability kind: what it is, and which
    implementations of it this host has."""
    descriptor = _descriptor_for(kind)
    return SchemaReference(
        target=kind,
        kind=kind,
        implementation=None,
        category="capability",
        **_prose_of(KIND_REGISTRY[kind]),
        metadata=(),
        spec=(),
        alternatives=_alternatives_of(descriptor),
        root_value=None,
    )


def implementation_reference(kind: str, name: str) -> SchemaReference:
    """The config reference for one implementation of a capability kind.

    Reads the implementation's DECLARED model. Nothing is constructed, so
    a platform whose plugin is not enabled documents itself exactly as an
    enabled one does.
    """
    from agentworks.capabilities.config import offered_model

    descriptor = _descriptor_for(kind)
    impl = _implementation(descriptor, name)
    model = offered_model(impl)
    entries = _entries(model)
    root = _root_entry(model, entries)
    return SchemaReference(
        target=f"{kind}/{name}",
        kind=kind,
        implementation=name,
        category="capability",
        **_prose_of(impl),
        metadata=(),
        spec=() if root is not None else entries,
        alternatives=(),
        root_value=root,
    )


def _named(metadata: tuple[FieldEntry, ...], kind: str) -> tuple[FieldEntry, ...]:
    """``metadata`` with the ``name`` field as a DOCUMENT has it.

    Two corrections, both because the row and the document disagree about
    this one field, and the document is what is being described.

    **It is always required.** The envelope demands `metadata.name` of every
    document, whatever the row says; a kind that defaults its name
    (``admin-template``) does so for the row the framework SYNTHESIZES, not
    for one an operator writes. Left alone, a rendered admin-template would
    comment out the only key in its metadata block and produce a document
    the loader rejects.

    **Its example is per-kind**, and it is the only value this module
    derives rather than reads. Three rules, each from the code that owns
    it: the envelope accepts only ``default`` for a kind with no instance
    selector, a kind that defaults its name has already said what to write,
    and everything else gets ``my-<kind>``, which is a legal name, is
    available, and is obviously a placeholder.

    Attached to the record rather than handled in a presenter, so both
    presenters (and the guide) see the same field.
    """

    return tuple(_document_name(entry, kind) if entry.name == "name" else entry for entry in metadata)


def _document_name(entry: FieldEntry, kind: str) -> FieldEntry:
    from agentworks.manifests.envelope import only_default_name

    if entry.doc.examples:
        example = entry.doc.examples[0]
    elif only_default_name(kind):
        example = "default"
    elif entry.doc.default is not UNSET and isinstance(entry.doc.default, str):
        example = entry.doc.default
    else:
        example = f"my-{kind}"
    return replace(entry, doc=replace(entry.doc, required=True, default=UNSET, examples=(example,)))


def _prose_of(subject: object) -> dict[str, str | None]:
    """The three authored strings, as the record's fields.

    ``summary`` is the subject's own ``description``, which every kind and
    every capability implementation already declares; :mod:`agentworks.topics`
    records why it is not restated as prose.
    """
    prose = prose_of(subject)
    summary = getattr(subject, "description", None)
    return {
        "title": None if prose is None else prose.title,
        "summary": summary if isinstance(summary, str) and summary else None,
        "overview": None if prose is None else prose.overview,
    }


def _entries(model: type[BaseModel], capability_kind: str | None = None) -> tuple[FieldEntry, ...]:
    """``model``'s fields as a tree, one union arm expanded.

    The stream is flat and addressed by path, so the tree is built by
    attaching each doc to the entry at its parent path.
    """
    roots: list[FieldEntry] = []
    by_path: dict[tuple[str, ...], list[FieldEntry]] = {(): roots}
    for doc in iter_field_docs(model):
        parent = doc.path[:-1]
        if parent not in by_path:
            _open_element(parent, by_path)
        siblings = by_path.get(parent)
        if siblings is None:
            raise StateError(f"the field stream reached {'.'.join(doc.path)} before its parent block")
        siblings.append(FieldEntry(doc=doc, children=(), alternatives=(), rendered=None))
        by_path[doc.path] = []
    return tuple(_resolved(entry, by_path, capability_kind) for entry in roots)


def _open_element(path: tuple[str, ...], by_path: dict[tuple[str, ...], list[FieldEntry]]) -> None:
    """Add the node standing for ONE element of a collection of blocks.

    The stream has no doc for it, and rightly: a model says a list holds
    tables without saying how many, so it streams the element's fields once
    under a placeholder segment and the element itself is not a field
    anyone declared. A tree has to have it, though, or the element's fields
    would hang directly off the collection and render one indent level too
    shallow, which in YAML is a different document.
    """
    container = by_path.get(path[:-2])
    if not path or path[-1] not in (SEQUENCE_ELEMENT, MAPPING_KEY) or container is None:
        return
    holder = next((entry for entry in container if entry.doc.path == path[:-1]), None)
    if holder is None or holder.doc.item_model is None:
        return
    element = replace(
        holder.doc,
        path=path,
        # The element itself: a table with no description, no default, and
        # nothing optional about it (writing the collection at all means
        # writing an element). What it CONTAINS follows in the stream.
        annotation=holder.doc.item_model,
        required=True,
        default=UNSET,
        default_template=None,
        description=None,
        choices=(),
        examples=(),
        ref=None,
        nested_model=holder.doc.item_model,
        item_model=None,
    )
    holder_children = by_path[path[:-1]]
    holder_children.append(FieldEntry(doc=element, children=(), alternatives=(), rendered=None))
    by_path[path] = []


def _resolved(
    entry: FieldEntry,
    by_path: dict[tuple[str, ...], list[FieldEntry]],
    capability_kind: str | None,
) -> FieldEntry:
    """``entry`` with its children attached, root wrappers collapsed, and
    its union (if it has one) expanded to one arm."""
    children = tuple(_resolved(child, by_path, capability_kind) for child in by_path[entry.doc.path])
    entry = replace(entry, children=children)
    entry = _collapsed(entry)
    return _expanded(entry, capability_kind)


def _collapsed(entry: FieldEntry) -> FieldEntry:
    """A field whose type is a ROOT model, merged with its ``root`` child.

    The capability config union is an ``AgwRootModel`` (the error bridge
    frames against a model), so the stream reads ``platform`` ->
    ``platform.root`` -> the arms. ``root`` is the wrapper's mechanism and
    never a key an operator writes, so the child's facts move up and the
    segment disappears.

    Presentation-layer, not stream-layer: ``iter_field_docs`` is shared and
    landed, and one presenter needing a normalization is not yet reason to
    change what every consumer sees.
    """
    if not _is_root_model(entry.doc.nested_model) or len(entry.children) != 1:
        return entry
    inner = entry.children[0]
    if inner.name != "root":
        return entry
    # The parent keeps its own path, description, and requiredness (they
    # are the field's); the wrapped value supplies what it IS.
    return replace(
        entry,
        doc=replace(entry.doc, union_arms=inner.doc.union_arms, choices=inner.doc.choices),
        children=inner.children,
    )


def _expanded(entry: FieldEntry, capability_kind: str | None) -> FieldEntry:
    """A discriminated union rendered as ONE arm, with the rest listed.

    The first registered arm, which is a built-in: the core's
    implementations seat at import and plugins seat after them, so the
    expanded arm is the one that needs no plugin and usually no
    credentials. A document holds one arm, so rendering them all would
    produce a sample that cannot be uncommented.
    """
    if not entry.doc.union_arms:
        return entry
    arms = entry.doc.union_arms
    summaries = _implementation_summaries(capability_kind)
    alternatives = tuple(
        Alternative(
            name=arm.tag,
            # The IMPLEMENTATION's one-liner where the arm is one (what
            # lima IS), falling back to the arm model's own docstring
            # (what its config is) for a union that is not a capability.
            summary=summaries.get(arm.tag) or arm.doc.description,
            target=None if capability_kind is None else f"{capability_kind}/{arm.tag}",
        )
        for arm in arms
    )
    return replace(
        entry,
        children=_entries(arms[0].doc.model, capability_kind),
        alternatives=alternatives,
        rendered=arms[0].tag,
    )


def _implementation_summaries(capability_kind: str | None) -> dict[str, str]:
    """Each implementation's one-line description, keyed by the tag that
    selects it, for a union whose arms are capability configs."""
    if capability_kind is None:
        return {}
    from agentworks.capabilities.config import registered_implementations

    summaries = {}
    for name, impl in registered_implementations(capability_kind).items():
        description = getattr(impl, "description", None)
        if isinstance(description, str) and description:
            summaries[name] = description
    return summaries


def _root_entry(model: type[BaseModel], entries: tuple[FieldEntry, ...]) -> FieldEntry | None:
    """The single entry describing a ROOT model's value, or ``None`` for an
    ordinary mapping-shaped model.

    A secret backend's per-secret mapping may be a bare string, which no
    ``BaseModel`` can be, so its declared model is a root model and its
    "fields" are one wrapper field nobody writes.
    """
    if not _is_root_model(model) or len(entries) != 1:
        return None
    return entries[0] if entries[0].name == "root" else None


def _is_root_model(model: type[BaseModel] | None) -> bool:
    return model is not None and isinstance(model, type) and issubclass(model, RootModel)


def _alternatives_of(descriptor: CapabilityKindDescriptor) -> tuple[Alternative, ...]:
    from agentworks.capabilities.config import registered_implementations

    return tuple(
        Alternative(
            name=name,
            summary=getattr(impl, "description", None),
            target=f"{descriptor.kind}/{name}",
        )
        for name, impl in sorted(registered_implementations(descriptor.kind).items())
    )


def _implementation(descriptor: CapabilityKindDescriptor, name: str) -> type:
    """The implementation class to document, or a typed refusal.

    The registry is reached through ``capabilities/config.py`` rather than
    through the descriptor's accessor, which is where every other read of a
    capability registry goes: one sanctioned call site, and the guard in
    ``tests/resources/test_graph_guard.py`` is what keeps it one.
    """
    from agentworks.capabilities.config import registered_implementation, registered_implementations

    impl = registered_implementation(descriptor.kind, name)
    if impl is None:
        known = ", ".join(sorted(registered_implementations(descriptor.kind)))
        raise ValidationError(
            f"no {descriptor.kind} named {name!r} is registered on this host",
            entity_kind=descriptor.kind,
            entity_name=name,
            hint=f"registered: {known}",
        )
    return impl


def _descriptor_for(kind: str) -> CapabilityKindDescriptor:
    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.plugins.registration import seat_installed_plugins

    # The same seating the spec-model assembly does, for the path that does
    # not go through it: an implementation contributed by an installed
    # plugin is one this host has, whether or not config opted into it.
    seat_installed_plugins()
    return descriptor_for(kind)


def _capability_descriptors() -> tuple[CapabilityKindDescriptor, ...]:
    from agentworks.capabilities.descriptor import capability_descriptors
    from agentworks.plugins.registration import seat_installed_plugins

    seat_installed_plugins()
    return capability_descriptors()


def plain_text(text: str) -> str:
    """Authored markdown as a terminal (or a YAML comment) should show it.

    Only one transform, and only because it is the one that fires on every
    line: a model's attribute docstrings use RST-style ``double backticks``
    for code, which markdown consumers (emitted schema descriptions, the
    guide's topic pages) render as a code span and a plain-text reader sees
    as noise. The record keeps the author's text; the presenters call this.
    """
    return text.replace("``", "`")


def worth_showing(default: object) -> bool:
    """Whether a declared default says anything to an operator.

    ``false`` and ``0`` do (they are the value the field takes); an empty
    list, table, or string does not, because "optional" has already said
    what omitting the field does. The distinction is truthiness plus two
    carve-outs, so it is spelled once here and shared by the value the
    sample writes and the parenthetical that names it.
    """
    if default is UNSET or default is None:
        return False
    return isinstance(default, bool | int | float) or bool(default)


def _placeholder(type_label: str) -> object:
    """A stand-in value shaped like the type, so the SHAPE is right even
    when the content is not.

    Built off :func:`~agentworks.schema.render_type`'s vocabulary rather
    than off the annotation, so one reading of a type serves the type line
    and the value beside it. An unrecognized type gets a scalar stand-in:
    the alternative is a field with no value at all, which reads as though
    it takes none.
    """
    label = type_label.removesuffix(" or null")
    if label.startswith("list of "):
        return [_placeholder(label.removeprefix("list of "))]
    if label.startswith("table of "):
        return {_PLACEHOLDER_KEY: _placeholder(label.removeprefix("table of "))}
    return _PLACEHOLDERS.get(label, "<value>")


def _wire_value(choice: object) -> object:
    """A ``Literal`` value or ``Enum`` member as a document carries it.

    ``FieldDoc.choices`` carries enum MEMBERS deliberately, so a presenter
    that wants the wire form asks for it. A sample writes the wire form.
    """
    return choice.value if isinstance(choice, Enum) else choice
