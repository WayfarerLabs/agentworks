"""``ResourceReference`` and its directional counterpart ``ReferenceEntry``.

Two types model the two directions of the same edge between Resources:

- ``ResourceReference`` is **outbound**: a Resource saying "I need this other
  Resource by name." Producers (each Resource type's ``referenced_resources()``
  method) emit concrete subclasses (``SecretReference``, ``TemplateReference``,
  ...); the framework consumes them through the base class.
- ``ReferenceEntry`` is **inbound**: a record attached to the target Resource's
  ``references`` tuple during ``Registry.finalize()``, projected from every
  outbound ``ResourceReference`` that resolved to that target.

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
(auto-declaring or erroring per the kind's miss policy), and appends a
``ReferenceEntry(source=ref.source, usage=ref.usage)`` to the target's
``references`` tuple. After finalize, each Resource can answer "who points
at me?" by iterating its own ``references``.

Concrete ``ResourceReference`` subclasses exist so producers and the
framework agree on the target kind via the *type*, not via string-dispatch
on the ``kind`` field. ``SecretReference`` and
``TemplateReference`` carry no extra fields beyond the base today;
future kinds may. Producers always instantiate a concrete subclass --
``ResourceReference`` itself is abstract-by-convention, not by ``ABC``;
the framework consumes references through the base type but never builds
one directly.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    """

    name: str
    kind: str
    usage: str
    source: tuple[str, str]


@dataclass(frozen=True)
class ConfigReference:
    """A resource reference implied by a capability's config block,
    returned by the capability's ``validate_config``. Sourceless by
    design: the consuming resource that owns the config block attaches
    itself as the ``source`` when it emits the corresponding
    ``ResourceReference`` (whoever hosts the config that names the
    resource emits the reference).
    """

    kind: str
    name: str
    usage: str


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

    Emitted by each template type's ``referenced_resources()`` for every
    name in its ``inherits = [...]`` list. The framework's miss policy
    resolves the name (auto-declaring ``default`` when reserved, erroring
    on other typos) and cycle detection catches inheritance loops.
    Per-template field-merging (the actual ``inherits`` semantics) stays
    in the existing template resolvers; this class is purely the
    framework's handle on the reference.

    No extra fields beyond the base today -- the subclass exists so
    producers and the framework agree on the target kind via the type.
    """

    # Empty body intentional; ``@dataclass(frozen=True)`` on the subclass
    # picks up the parent's fields.


@dataclass(frozen=True)
class ReferenceEntry:
    """Inbound reference record: "I am pointed at by (source), for
    (usage)." One ``ReferenceEntry`` lands on a Resource's ``references``
    tuple for every outbound ``ResourceReference`` the framework resolved
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
    side are dropped here because they are implicit from the container
    Resource -- there is no ambiguity about which Resource an entry on
    ``vm-template:default.references`` is attached to.
    """

    source: tuple[str, str]
    usage: str
