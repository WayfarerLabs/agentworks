"""``ResourceRequirement`` and concrete subclasses.

A requirement is a **reference declaration**: one Resource saying "I need
this other Resource by name." Producers (each Resource type's
``required_resources()`` method) return concrete subclasses
(``SecretRequirement``, ``TemplateRequirement``, ...); the framework consumes
them through the base class.

Concrete subclasses exist so producers and the framework agree on the target
kind without string-dispatch on the ``kind`` field. Phase 1 ships
``SecretRequirement`` (no extra fields); Phase 2 subclasses (``Template
Requirement``, ``CatalogRequirement``, ...) carry per-kind defaults.

``UsageEntry`` is the per-Resource-per-requirement record the framework
accumulates on each Resource's ``usage`` list during ``Registry.finalize()``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceRequirement:
    """Base requirement record.

    Fields:

    - ``name``: target Resource's name (operator-overridable when the
      declaring resource exposes the name as a config field; otherwise
      fixed per the framework's defaults).
    - ``kind``: target Resource's kind identifier (``"secret"``,
      ``"vm_template"``, ...). The same kind strings appear throughout the
      framework: ``KIND_REGISTRY`` keys, ``Origin.source[0]``, error
      messages.
    - ``usage``: system-defined "what the declaring resource needs this
      target for" text. Each requirement contributes one entry to its
      target's ``usage`` list.
    - ``source``: ``(kind, name)`` pair identifying the declaring
      Resource. ``kind`` matches the declaring Resource's kind (e.g.,
      ``"vm_template"`` for ``vm_templates.azure-prod``); ``name`` is the
      declaring Resource's name. Singleton kinds use ``"default"`` as the
      name (e.g., ``("admin_template", "default")``).
    """

    name: str
    kind: str
    usage: str
    source: tuple[str, str]


@dataclass(frozen=True)
class SecretRequirement(ResourceRequirement):
    """Requirement targeting a ``"secret"`` Resource.

    Phase 1 adds no extra fields beyond the base; the subclass exists so
    producers and the framework agree on the target kind via the type, not
    via dispatch on ``ResourceRequirement.kind``.
    """


@dataclass(frozen=True)
class UsageEntry:
    """A single entry on a Resource's ``usage`` list. The framework appends
    one per matching requirement during ``Registry.finalize()``.

    - ``source``: the requirement's ``source`` ``(kind, name)`` pair.
    - ``text``: the requirement's ``usage`` text. Multiple sources requiring
      the same Resource with the same text are preserved as distinct
      entries; deduplication happens at render time only where summary
      display calls for it.
    """

    source: tuple[str, str]
    text: str
