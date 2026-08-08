"""The field TREE: the flat stream, shaped the way both presenters need it.

``iter_field_docs`` is flat and path-addressed, which is the right shape
for a walker and the wrong one for anything rendering nesting. So the tree
is built ONCE here, and the generated sample (``manifests/skeleton.py``)
and the field reference (``manifests/describe.py``) both read it rather
than each walking the stream with its own idea of what a nested block is.

:class:`FieldEntry` also carries the handful of facts both presenters would
otherwise derive identically (is this field the operator's to write, what
type does it take, what value would a sample write beside it), so they
cannot come to different answers about the same field.

The public surface is ``manifests/reference.py``, which assembles these
into the record a caller asks for by name.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Union

from pydantic import BaseModel, RootModel

from agentworks.errors import StateError
from agentworks.schema import UNSET, element_annotation, iter_field_docs, render_type

if TYPE_CHECKING:
    from agentworks.schema import FieldDoc, UnionArm

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
    implementation of a capability kind, or an arm of any other tagged
    union.

    **Every alternative is READABLE**, and at least one of the three
    fields below says how: :attr:`target` addresses it, :attr:`fields`
    shows it, or :attr:`recurring` says it is the block already open
    above. Naming an arm and offering none of the three is the defect this
    record is shaped to make impossible, and
    ``tests/manifests/test_describe_kind.py`` asserts the property over
    every describable target.
    """

    name: str
    """The tag that selects it (``"azure-vm"``)."""

    summary: str | None
    """Its one-line description, so a list of alternatives says what each
    one IS without a second lookup."""

    target: str | None
    """The address that describes it in full (``"vm-platform/azure-vm"``),
    or ``None`` for an arm of a union that is not a capability."""

    fields: tuple[FieldEntry, ...] = ()
    """This arm's own fields, where this reference shows them.

    Set for an arm of a PLAIN union, which has no address and never will
    (it is an anonymous model inside one field, and no CLI form reaches
    it), so here is the only place its fields can appear. Set for the arm
    a DOCUMENT would be written against, addressable or not, because a
    sample has to write one; that is :attr:`FieldEntry.rendered`.

    Empty for any OTHER arm carrying a :attr:`target`, whose address
    documents it in full and whose config may be as large as a whole
    platform's, and for a :attr:`recurring` arm. :func:`_shows_fields` is
    where the rule lives."""

    recurring: bool = False
    """Whether this arm is the block already open above this point.

    A union arm reachable from itself (a group whose members are groups)
    is not expanded again, because a document nests it to a depth the
    model cannot know and one more level would be no more complete than
    none. Its fields are on the page, higher up, which is why this counts
    as readable and is not the same absence as an arm nobody expanded."""


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
    """The fields inside this one: a nested block's or a collection
    element's.

    Empty when this field is a union, whose contents belong to its ARMS
    rather than to it: see :attr:`alternatives` and :attr:`contents`."""

    alternatives: tuple[Alternative, ...]
    """The arms of a discriminated union, when this field is one. Each
    carries its own fields where this reference shows them."""

    @property
    def rendered(self) -> str | None:
        """Which alternative a DOCUMENT would be written against: the
        first arm whose fields are shown here, or ``None`` when none are.

        A document holds one arm, so a sample has to pick, and this is the
        pick. Derived from :attr:`alternatives` rather than stored beside
        them, so the arm named here and the arm whose fields are available
        cannot come apart."""
        return next((alt.name for alt in self.alternatives if alt.fields), None)

    @property
    def contents(self) -> tuple[FieldEntry, ...]:
        """The fields ONE document writes inside this one.

        :attr:`children` for a plain block; for a union, the fields of the
        arm :attr:`rendered` names. For the surfaces that write or measure
        a single document (the generated sample, the value a field is
        given): a reference showing every arm reads
        :attr:`alternatives` instead.
        """
        if not self.alternatives:
            return self.children
        return next((alt.fields for alt in self.alternatives if alt.fields), ())

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
        """The value a generated sample writes for this field, as a Python
        value: a presenter renders it with
        :func:`~agentworks.manifests.yaml_value.render_value`, which is
        what turns an enum member, a nested table, or a plugin's exotic
        default into the form a document carries.

        In order: the author's first example, the ONE value a closed field
        can hold, the field's own default where it is worth showing, and
        finally a placeholder derived from the type. ``UNSET`` for a field
        whose content is what a document writes INSIDE it
        (:attr:`contents`).

        The one-value step is the union arm's tag (``name: lima``), which is
        the value rather than a choice among values. A field with SEVERAL
        choices falls through to its default, because suggesting
        ``tmux_layout: tiled`` beside a parenthetical reading "default:
        aw-session-vertical" would offer an arbitrary pick as though it were
        the recommended one; the type line already lists what may go there.

        An EMPTY default (``[]``, ``{}``, ``""``) is skipped in favor of the
        placeholder: it is the honest default and it teaches nothing, and a
        commented ``repos: []`` says less than a commented
        ``repos: [<string>]`` about what may go there.
        """
        if self.doc.examples:
            return self.doc.examples[0]
        if len(self.doc.choices) == 1:
            return self.doc.choices[0]
        if self.contents:
            return UNSET
        if worth_showing(self.doc.default):
            return self.doc.default
        if self.doc.choices:
            return self.doc.choices[0]
        return _placeholder(self.type_label)


def field_tree(model: type[BaseModel], capability_kind: str | None = None) -> tuple[FieldEntry, ...]:
    """``model``'s fields as a tree, one union arm expanded.

    ``capability_kind`` names the capability whose implementations a
    discriminated union's arms ARE, when they are: it is what lets an
    alternative carry the address that documents it and the implementation's
    own one-liner rather than its config model's docstring.
    """
    return _tree(model, capability_kind, ())


def _tree(
    model: type[BaseModel],
    capability_kind: str | None,
    expanding: tuple[type[BaseModel], ...],
) -> tuple[FieldEntry, ...]:
    """One level of :func:`field_tree`, told which models are already open
    above it.

    The stream is flat and addressed by path, so the tree is built by
    attaching each doc to the entry at its parent path.

    ``expanding`` is the current PATH of models whose union arms are being
    rendered, and it is the guard against a config model reachable from
    itself through a union: ``iter_field_docs`` threads its own guard, but
    it is re-entered from scratch for each expanded arm, so without this
    a self-reachable arm would recur until the interpreter gave up and
    take ``describe-kind``, ``sample``, and ``schema`` down with it. Path
    scoped rather than accumulating, for the same reason the stream's own
    guard is: two sibling fields whose unions share an arm each render it.

    Naturally recursive, and it stays that way: the structure walked here
    is a model class graph, whose depth an AUTHOR writes rather than an
    operator, which is the case the shared traversal discipline
    (:mod:`agentworks.traversal`) leaves alone.
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
        siblings.append(FieldEntry(doc=doc, children=(), alternatives=()))
        by_path[doc.path] = []
        _open_tagged_element(doc, by_path)
    expanding = (*expanding, model)
    return tuple(_resolved(entry, by_path, capability_kind, expanding) for entry in roots)


def root_entry(model: type[BaseModel], entries: tuple[FieldEntry, ...]) -> FieldEntry | None:
    """The single entry describing a ROOT model's value, or ``None`` for an
    ordinary mapping-shaped model.

    A secret backend's per-secret mapping may be a bare string, which no
    ``BaseModel`` can be, so its declared model is a root model and its
    "fields" are one wrapper field nobody writes.
    """
    if not _is_root_model(model) or len(entries) != 1:
        return None
    return entries[0] if entries[0].name == "root" else None


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


def _open_element(path: tuple[str, ...], by_path: dict[tuple[str, ...], list[FieldEntry]]) -> None:
    """Add the node standing for ONE element of a collection of blocks, on
    meeting the first of that element's fields.

    The stream has no doc for it, and rightly: a model says a list holds
    tables without saying how many, so it streams the element's fields once
    under a placeholder segment and the element itself is not a field
    anyone declared. A tree has to have it, though, or the element's fields
    would hang directly off the collection and render one indent level too
    shallow, which in YAML is a different document.

    Driven by the CHILD rather than by the holder, so a collection whose
    element opens nothing (its model is already expanding above this
    point) stays the bare collection the stream describes rather than
    growing an element with nothing inside it.
    """
    container = by_path.get(path[:-2])
    if not path or container is None:
        return
    holder = next((entry for entry in container if entry.doc.path == path[:-1]), None)
    if holder is None or holder.doc.item_model is None or path[-1] != holder.doc.item_segment:
        return
    _add_element(
        holder.doc,
        by_path,
        # What one element IS, read off the collection's own type rather
        # than off the model it opens. The two differ exactly where an
        # element may be written as a bare scalar as well as a table: the
        # model is one arm of the element, and naming it here would
        # document a shorthand's table form as the only form.
        segment=path[-1],
        annotation=element_annotation(holder.doc.annotation) or holder.doc.item_model,
        nested_model=holder.doc.item_model,
    )


def _open_tagged_element(doc: FieldDoc, by_path: dict[tuple[str, ...], list[FieldEntry]]) -> None:
    """Add the node standing for one element of a collection whose
    ELEMENTS are a discriminated union of models.

    Driven by the holder's own doc, because such an element streams no
    fields to be driven by: whose fields those would be is the arm
    question, and the stream leaves that to the presenter exactly as it
    does for a union written on the field itself. Answering it here is
    what puts an arm's fields one indent under the collection, where a
    document has them, and it is what lets :func:`_expanded` apply to this
    union the same path guard it applies to every other one.
    """
    if not doc.item_union_arms or doc.item_segment is None:
        return
    _add_element(
        doc,
        by_path,
        segment=doc.item_segment,
        annotation=_element_annotation(doc.item_union_arms),
        union_arms=doc.item_union_arms,
    )


def _element_annotation(arms: tuple[UnionArm, ...]) -> object:
    """One element's own type: it is one of the arms.

    Named from the arms rather than read back off the collection's
    annotation, so the type an operator is shown and the alternatives they
    are offered cannot disagree about which models are on offer. Every
    classified arm is a model, so this renders as the same "table" a plain
    collection's element renders as.
    """
    return Union[tuple(arm.doc.model for arm in arms)]  # noqa: UP007


def _add_element(
    holder: FieldDoc,
    by_path: dict[tuple[str, ...], list[FieldEntry]],
    *,
    segment: str,
    annotation: object,
    nested_model: type[BaseModel] | None = None,
    union_arms: tuple[UnionArm, ...] = (),
) -> None:
    """Attach the element node ``holder`` holds many of, however it was
    reached.

    One synthesizer for both element shapes, so a collection of blocks and
    a collection of TAGGED blocks cannot come to different answers about
    what an element is. What differs is what the element IS: one model,
    whose fields the stream already streamed, or a choice among arms, one
    of which :func:`_expanded` opens.
    """
    path = (*holder.path, segment)
    element = replace(
        holder,
        path=path,
        # The element itself: a table with no description, no default, and
        # nothing optional about it (writing the collection at all means
        # writing an element). What it CONTAINS follows: the stream's
        # fields for a plain block, the expanded arm's for a tagged one.
        annotation=annotation,
        required=True,
        default=UNSET,
        default_template=None,
        description=None,
        choices=(),
        examples=(),
        ref=None,
        nested_model=nested_model,
        item_model=None,
        item_segment=None,
        union_arms=union_arms,
        item_union_arms=(),
    )
    by_path[holder.path].append(FieldEntry(doc=element, children=(), alternatives=()))
    by_path[path] = []


def _resolved(
    entry: FieldEntry,
    by_path: dict[tuple[str, ...], list[FieldEntry]],
    capability_kind: str | None,
    expanding: tuple[type[BaseModel], ...],
) -> FieldEntry:
    """``entry`` with its children attached, root wrappers collapsed, and
    its union (if it has one) expanded to one arm."""
    children = tuple(_resolved(child, by_path, capability_kind, expanding) for child in by_path[entry.doc.path])
    entry = replace(entry, children=children)
    entry = _collapsed(entry)
    return _expanded(entry, capability_kind, expanding)


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


def _expanded(
    entry: FieldEntry,
    capability_kind: str | None,
    expanding: tuple[type[BaseModel], ...],
) -> FieldEntry:
    """A discriminated union as its arms, each one readable.

    Every arm is named, and naming an arm raises exactly the question
    :func:`_shows_fields` answers: what do I write if I pick that one. An
    arm that has an address of its own is answered by the address; an arm
    that has none is answered here, with its fields; an arm that is the
    block already open above is answered by that block.
    """
    if not entry.doc.union_arms:
        return entry
    arms = entry.doc.union_arms
    implementations = _implementations(capability_kind)
    alternatives = tuple(
        _alternative(arm, first=index == 0, kind=capability_kind, implementations=implementations, expanding=expanding)
        for index, arm in enumerate(arms)
    )
    return replace(entry, alternatives=alternatives)


def _alternative(
    arm: UnionArm,
    *,
    first: bool,
    kind: str | None,
    implementations: dict[str, str | None],
    expanding: tuple[type[BaseModel], ...],
) -> Alternative:
    """One arm, with whichever of the three ways of being readable it
    has. See :class:`Alternative`."""
    target = f"{kind}/{arm.tag}" if arm.tag in implementations else None
    recurring = arm.doc.model in expanding
    return Alternative(
        name=arm.tag,
        # The IMPLEMENTATION's one-liner where the arm is one (what lima
        # IS), falling back to the arm model's own docstring (what its
        # config is) for a union that is not a capability.
        summary=implementations.get(arm.tag) or arm.doc.description,
        target=target,
        fields=() if recurring or not _shows_fields(target, first=first) else _tree(arm.doc.model, kind, expanding),
        recurring=recurring,
    )


def _shows_fields(target: str | None, *, first: bool) -> bool:
    """Whether this reference expands an arm in place.

    **An arm with no address is expanded**, because there is nowhere else
    to read it. It is an anonymous model inside one field, so no
    ``describe-kind`` form reaches it and no generated sample can show it
    either (a document holds one arm, so the sample expands one and names
    the rest). Left unexpanded it is a word an operator cannot act on,
    which is what ``auth: {mode: service-principal}`` was: named in the
    list, and its three required keys documented nowhere.

    **An arm WITH an address is not**, unless it is the first. Its address
    documents it in full, its config is as large as a whole platform's,
    and inlining five of them would bury the field that holds one.

    **The first arm is expanded whatever it is**, because one has to be:
    the generated sample writes a document, a document holds one arm, and
    :attr:`FieldEntry.rendered` names the one it writes. First rather than
    chosen: the core's implementations seat at import and plugins seat
    after them, so it is the arm that needs no plugin and usually no
    credentials.
    """
    return first or target is None


def _implementations(capability_kind: str | None) -> dict[str, str | None]:
    """Every implementation of ``capability_kind`` this host has, mapped to
    its one-line description.

    MEMBERSHIP is as load-bearing as the description, which is why an
    implementation with no description is still a key here: an arm gets
    the address that documents it only when that address exists. A
    capability kind's own union has an implementation behind every arm,
    but it is not the only union in the tree an implementation's config is
    collected under, and any other one (a list of disks, each saying which
    kind of disk it is) would otherwise be handed
    ``agw resource describe-kind vm-platform/local``, a command that
    fails.
    """
    if capability_kind is None:
        return {}
    from agentworks.capabilities.config import registered_implementations

    implementations: dict[str, str | None] = {}
    for name, impl in registered_implementations(capability_kind).items():
        description = getattr(impl, "description", None)
        implementations[name] = description if isinstance(description, str) and description else None
    return implementations


def _is_root_model(model: type[BaseModel] | None) -> bool:
    return model is not None and isinstance(model, type) and issubclass(model, RootModel)


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
