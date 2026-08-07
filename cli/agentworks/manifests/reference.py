"""What a kind or a capability implementation accepts, collected once.

The service layer behind BOTH schema-derived operator surfaces: the
generated sample (``manifests/skeleton.py``, reached through
``agw resource sample``) and the field reference
(``manifests/describe.py``, reached through ``agw resource describe-kind``).
The onboarding effort's ``agw guide`` calls the same functions for its
``FieldReference`` and ``Sample`` blocks rather than scraping rendered CLI
output, which is why the records here carry facts and not text.

One collector, two presenters. The alternative (each surface walking the
stream itself) is two walks that agree until one of them learns something,
and the thing they would disagree about is which fields exist.

**Nothing here loads config, builds a registry, or constructs a
capability.** It reads models and the capability code registries, so it
answers for a plugin whose config has not opted in (enablement is a
property of a published ROW), and it answers on a host whose config does
not parse.

The tree and the per-field derivations live in ``manifests/field_tree.py``;
this module names things, finds their models, and attaches their prose.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ValidationError
from agentworks.manifests.field_tree import Alternative, FieldEntry, field_tree, root_entry
from agentworks.manifests.spec_model import declarable_kinds, hosted_capability, metadata_model, spec_model
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import UNSET
from agentworks.topics import prose_of, summary_of

if TYPE_CHECKING:
    from agentworks.capabilities.descriptor import CapabilityKindDescriptor

__all__ = [
    "Alternative",
    "FieldEntry",
    "SchemaReference",
    "capability_kind_reference",
    "describable_targets",
    "implementation_reference",
    "kind_reference",
    "plain_text",
    "reference_for",
]


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
    """Every target :func:`reference_for` accepts, sorted.

    A declared service API rather than something a CLI surface consumes:
    shell completion for ``describe-kind`` uses the config-free kinds
    completer (which is deliberately narrower, since it must answer on a
    host whose config does not load), and the unknown-kind refusal lists
    the kind registry. This is for a caller that wants to ENUMERATE what
    can be documented, which is what the guide's catalog does.
    """
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
        metadata=_named(field_tree(metadata_model(kind)), kind),
        spec=field_tree(spec_model(kind), None if descriptor is None else descriptor.kind),
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
    entries = field_tree(model)
    root = root_entry(model, entries)
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


def plain_text(text: str) -> str:
    """Authored markdown as a terminal (or a YAML comment) should show it.

    Only one transform, and only because it is the one that fires on every
    line: a model's attribute docstrings use RST-style ``double backticks``
    for code, which markdown consumers (emitted schema descriptions, the
    guide's topic pages) render as a code span and a plain-text reader sees
    as noise. The record keeps the author's text; the presenters call this.
    """
    return text.replace("``", "`")


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
    return {
        "title": None if prose is None else prose.title,
        "summary": summary_of(subject),
        "overview": None if prose is None else prose.overview,
    }


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
