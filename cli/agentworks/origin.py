"""``Origin``: the per-Resource record of where a Resource came from.

Set once when the Resource is added to the Registry (via
``Registry.add(origin=...)`` for operator- and built-in Resources, or
via the kind's ``synthesize`` for auto-declared ones); never mutated.

Four variants today:

- ``operator-declared``: from the operator's YAML manifests (the manifest
  publisher). Carries ``file: Path`` + ``line: int`` for traceability, built
  from the manifest document's ``SourceLocation`` at decode.
- ``built-in``: shipped with the app itself, inseparable from it (the
  bundled-manifest publisher and other app-bundled publishers). Carries
  ``source: str``, a code-source identifier like
  ``"agentworks.manifests.builtin/apt-sources.yaml"``.
- ``auto-declared``: synthesized by a kind's miss policy during
  ``Registry.finalize()`` to satisfy a reference that didn't resolve to
  any published Resource. Carries ``source: tuple[str, str]`` -- the first
  matching reference's ``(kind, name)`` source per the config-load walk
  order.
- ``system-plugin``: contributed by a system plugin, distributed with
  the app but separable and possibly requiring explicit enable. Carries
  ``plugin: str`` (the plugin name) and ``source: str`` (a code-source
  identifier like ``"agentworks.plugins.<name>"`` for a capability row
  or ``"agentworks.plugins.<name>/manifests/<file>"`` for a bundled
  manifest); ``file``/``line`` are ``None``. Nothing constructs this
  variant yet; the plugin effort's later phases wire in the producers.

One variant is reserved for the plugin system and not constructible
yet: ``external-plugin`` (installed from outside sources). It is
documented here so display vocabulary and operator expectations are
stable, but has no ``Literal`` entry and no factory classmethod until a
later phase adds them.

The framework's ``Origin`` is distinct from the Config layer's
``SourceLocation`` so the two layers can evolve independently. Operators see
``Origin`` (rendered as e.g., ``"operator-declared (config.toml:42)"`` or
``"auto-declared by vm-template:azure-prod"``) in ``agw doctor``,
``agw secret list``, and ``agw secret describe``.

This type BELONGS to the resource layer and is re-exported by
``agentworks.resources``, but it LIVES at top level, next to
``declared_resource`` and ``source_location`` and for the same reason
they do: the declared-row base carries ``origin`` as a MODEL FIELD, which
a model resolves at class-definition time, and importing anything under
``agentworks.resources`` runs that package's ``__init__``, which loads
every kind module, which loads the very rows that inherit the base. It
imports nothing of ours, so nothing about it needed the deeper home.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Origin:
    """Per-Resource provenance record. Construct via the
    ``operator_declared`` / ``built_in`` / ``auto_declared`` /
    ``system_plugin`` classmethods; never instantiate directly.

    The variant-specific fields are typed as broad unions on the class so
    one dataclass can express all four shapes. The factory classmethods
    are the only correct construction path; they pin the right fields per
    variant. Inspect ``variant`` first; the other fields' contracts depend
    on it.

    Variant contracts:

    - ``operator-declared``: ``file`` and ``line`` are populated; ``source``
      is ``None``.
    - ``built-in``: ``source`` is a ``str``; ``file`` and ``line`` are
      ``None``.
    - ``auto-declared``: ``source`` is a ``tuple[str, str]``; ``file`` and
      ``line`` are ``None``.
    - ``system-plugin``: ``plugin`` is set; ``source`` is a ``str``;
      ``file`` and ``line`` are ``None``.

    ``external-plugin`` stays reserved and not constructible: no
    ``Literal`` entry, no factory.
    """

    variant: Literal["operator-declared", "built-in", "auto-declared", "system-plugin"]
    file: Path | None = None
    line: int | None = None
    source: str | tuple[str, str] | None = None
    plugin: str | None = None

    @classmethod
    def operator_declared(cls, *, file: Path, line: int) -> Origin:
        """Operator-typed Resource (TOML Config or YAML manifests)."""
        return cls(variant="operator-declared", file=file, line=line)

    @classmethod
    def built_in(cls, *, source: str) -> Origin:
        """Resource shipped with the app itself (the bundled-manifest
        publisher, other app-bundled publishers). ``source`` is a
        code-source identifier like
        ``"agentworks.manifests.builtin/apt-sources.yaml"``.
        Plugin-shipped resources do NOT use this variant; they get
        ``system_plugin`` (or the reserved ``external-plugin`` variant,
        once a later phase makes it constructible).
        """
        return cls(variant="built-in", source=source)

    @classmethod
    def system_plugin(cls, *, plugin: str, source: str) -> Origin:
        """A resource contributed by a system plugin (R1). ``plugin`` is the
        plugin name; ``source`` is a code-source identifier
        (``agentworks.plugins.<name>`` for a capability row,
        ``agentworks.plugins.<name>/manifests/<file>`` for a bundled
        manifest). ``file``/``line`` are ``None``."""
        return cls(variant="system-plugin", plugin=plugin, source=source)

    @classmethod
    def auto_declared(cls, *, source: tuple[str, str]) -> Origin:
        """Framework-synthesized Resource (auto-declared by a kind's miss
        policy during ``Registry.finalize()``). ``source`` is the first
        matching reference's ``(kind, name)`` per config-load walk
        order; the full set of matching references is recorded in the
        Resource's ``references`` tuple, not here.
        """
        return cls(variant="auto-declared", source=source)
