"""``ResourceReference`` and its directional counterpart ``ReferenceEntry``.

Two types model the two directions of the same edge between Resources:

- ``ResourceReference`` is **outbound**: a Resource saying "I need this other
  Resource by name." Producers (each Resource type's ``dependencies(context)``
  method) emit concrete subclasses (``SecretReference``, ``TemplateReference``,
  ...); the framework consumes them through the base class.
- ``ReferenceEntry`` is **inbound**: a record stored on the target's
  dependency-graph node during ``Registry.finalize()`` (queryable via
  ``Registry.graph.dependents_of``), projected from every outbound
  ``ResourceReference`` that resolved to that target.

The two types carry the same prose in their ``usage`` field (e.g., "the
tailscale auth key for vm_template:default") -- the symmetry is intentional.
The shape difference is just what each side already knows:

================== =========== =========== ============================
field              outbound    inbound     why
================== =========== =========== ============================
``name``           required    dropped     target name is implicit
                                           from the container Resource
``kind``           required    dropped     target kind is implicit
                                           from the container Resource
``source``         required    required    the declaring Resource's
                                           ``(kind, name)`` pair
``usage``          required    required    prose: "what the source
                                           needs the target for"
================== =========== =========== ============================

``ReferenceEntry`` instances are created exclusively in
``Registry.finalize()`` -- producers never construct them. The finalize pass
walks every published ``ResourceReference``, resolves each one to its target
(auto-declaring or erroring per the kind's miss policy), and records a
``ReferenceEntry(source=ref.source, usage=ref.usage)`` on the target's graph
node. After finalize, "who points at me?" is answered by
``Registry.graph.dependents_of(kind, name)``.

Concrete ``ResourceReference`` subclasses exist so producers and the
framework agree on the target kind via the *type*, not via string-dispatch
on the ``kind`` field. ``SecretReference`` and
``TemplateReference`` carry no extra fields beyond the base today;
future kinds may. Producers always instantiate a concrete subclass --
``ResourceReference`` itself is abstract-by-convention, not by ``ABC``;
the framework consumes references through the base type but never builds
one directly.

The subclass types the TARGET; ``relationship`` types the EDGE. They are
different questions and FR17 turns on the second one: an inheritance edge
is source composition rather than a runtime need, and a traversal that
means "what does this resource need at run time" must not cross it.
``TemplateReference`` answers "points at a template", which coincides with
inheritance only for as long as ``inherits`` is the sole reason to point
at one, so :func:`inherits_reference` is the single spelling of an
inheritance edge and ``RefRelationship.INHERITS`` is what every consumer
keys on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.schema.reference import ConfigReference, RefRelationship

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class ResourceReference:
    """Outbound reference record: "I (source) need a Resource of this kind
    with this name, for this purpose."

    Fields:

    - ``name``: target Resource's name (operator-overridable when the
      declaring resource exposes the name as a config field; otherwise
      fixed per the framework's defaults).
    - ``kind``: target Resource's kind identifier (``"secret"``,
      ``"vm-template"``, ...). The same kind strings appear throughout the
      framework: ``KIND_REGISTRY`` keys, ``Origin.source[0]``, error
      messages.
    - ``usage``: prose describing what the declaring Resource needs the
      target for. The framework propagates this verbatim to the
      ``ReferenceEntry`` it attaches to the target during finalize, so the
      same string appears in ``agw resource describe``'s "Referenced by:"
      section. Example: ``"the tailscale auth key for vm-template:default"``.
    - ``source``: ``(kind, name)`` pair identifying the declaring
      Resource. ``kind`` matches the declaring Resource's kind (e.g.,
      ``"vm-template"`` for ``vm_templates.azure-prod``); ``name`` is the
      declaring Resource's name. Kinds whose operator surface is still
      a singleton today (``admin-template``, ``named-console-template``)
      always source from ``"default"``; the framework treats those kinds
      as named-multi-instance under the hood, so a future plurified
      operator surface flows through the same shape unchanged.
    - ``relationship``: what the source MEANS by the edge (FR17). It
      defaults to ``USES``, a runtime need, which is what all but the
      inheritance edge is; ``INHERITS`` says the target's declaration is
      composed into the source's, and is emitted only through
      :func:`inherits_reference`.
    """

    name: str
    kind: str
    usage: str
    source: tuple[str, str]
    relationship: RefRelationship = RefRelationship.USES


@dataclass(frozen=True)
class SecretReference(ResourceReference):
    """Outbound reference targeting a ``"secret"`` Resource.

    No extra fields beyond the base; the subclass exists so producers and
    the framework agree on the target kind via the type, not via dispatch
    on ``ResourceReference.kind``.
    """


@dataclass(frozen=True)
class TemplateReference(ResourceReference):
    """Outbound reference targeting a template-kind Resource (``vm-template``,
    ``workspace-template``, ``agent-template``, ``session-template``).

    Emitted for every name in an inheriting resource's ``inherits = [...]``
    list (through :func:`inherits_reference`, which is what marks the edge
    ``INHERITS``). The framework's miss policy resolves the name
    (auto-declaring ``default`` when reserved, erroring on other typos) and
    cycle detection catches inheritance loops. Per-template field-merging
    (the actual ``inherits`` semantics) stays in the existing template
    resolvers; this class is purely the framework's handle on the target.

    It says "the target is a template" and nothing more. It is NOT the
    inheritance marker: a future uses-a-template edge would be this type
    with ``relationship=USES``, which is exactly why FR17 keys on the
    relationship instead (see the module docstring).

    No extra fields beyond the base today; the subclass exists so
    producers and the framework agree on the target kind via the type.
    """

    # Empty body intentional; ``@dataclass(frozen=True)`` on the subclass
    # picks up the parent's fields.


@dataclass(frozen=True)
class ReferenceEntry:
    """Inbound reference record: "I am pointed at by (source), for
    (usage)." One ``ReferenceEntry`` lands on a Resource's dependency-graph
    node for every outbound ``ResourceReference`` the framework resolved
    to that Resource during ``Registry.finalize()``.

    Fields:

    - ``source``: the originating ``ResourceReference.source`` ``(kind,
      name)`` pair -- the declaring Resource that needed this target.
    - ``usage``: the same prose the outbound ``ResourceReference.usage``
      carried. Same field name on both ends is intentional: one concept,
      surfaced in both directions. ``agw resource describe``'s
      "Referenced by:" section renders this verbatim.

    Producers never construct ``ReferenceEntry`` directly; the framework
    builds them in ``Registry.finalize()`` after every reference has
    been resolved to its target. ``kind`` and ``name`` from the outbound
    side are dropped here because they are implicit from the graph node
    the entry is stored on -- there is no ambiguity about which Resource an
    entry on the ``("vm-template", "default")`` node belongs to.
    """

    source: tuple[str, str]
    usage: str


def inherits_reference(parent: str, source: tuple[str, str]) -> TemplateReference:
    """The outbound edge an inheriting resource publishes for one name in
    its ``inherits = [...]`` list.

    The ONE spelling of an inheritance edge, so no producer can emit one
    that forgets to say so. That matters more than saving four lines: an
    edge left at the default ``USES`` is not a crash but a wrong answer,
    silently pulling a parent's runtime needs into the child's (FR17).

    The target kind comes from ``source`` rather than from a parameter
    because inheritance composes a declaration into another declaration OF
    THE SAME KIND, so the two can never legitimately differ and a
    parameter would only be a way to get them out of step.
    """
    return TemplateReference(
        name=parent,
        kind=source[0],
        usage="a parent template",
        source=source,
        relationship=RefRelationship.INHERITS,
    )


def sourced_references(
    config_refs: Iterable[ConfigReference],
    source: tuple[str, str],
) -> list[ResourceReference]:
    """Promote sourceless ``ConfigReference``s (a capability's
    ``dependencies`` output) to sourced outbound ``ResourceReference``s.

    Attaches ``source`` (the consuming resource that owns the config
    block) and selects the concrete subclass each reference's ``kind``
    implies: ``SecretReference`` for secrets, the base ``ResourceReference``
    otherwise. The consuming resource composes the capability's implied
    edges into its own outbound references through this one helper,
    centralizing what the three capability-config resources
    (``vm-site``, ``git-credential``, ``session-template``) duplicated.

    The ``ConfigReference``'s ``relationship`` rides along: a model's field
    marker is where a modeled inheritance edge would be declared, and
    dropping it here would leave the graph unable to tell one from a
    runtime need (FR17).
    """
    result: list[ResourceReference] = []
    for cref in config_refs:
        ref_cls = SecretReference if cref.kind == "secret" else ResourceReference
        result.append(
            ref_cls(
                name=cref.name,
                kind=cref.kind,
                usage=cref.usage,
                source=source,
                relationship=cref.relationship,
            )
        )
    return result


__all__ = [
    "ConfigReference",
    "RefRelationship",
    "ReferenceEntry",
    "ResourceReference",
    "SecretReference",
    "TemplateReference",
    "inherits_reference",
    "sourced_references",
]
