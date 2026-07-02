"""``Origin``: the per-Resource record of where a Resource came from.

Set once when the Resource is added to the Registry (via
``Registry.add(origin=...)`` for operator- and built-in Resources, or
via the kind's ``synthesize`` for auto-declared ones); never mutated.

Three variants today:

- ``operator-declared``: from operator config (Config publisher today;
  future YAML manifest publishers later). Carries ``file: Path`` + ``line:
  int`` for traceability. Built from the Config-layer ``SourceLocation``
  during ``Config.publish_to``.
- ``built-in``: shipped with the app itself, inseparable from it (the
  catalog publisher and other app-bundled publishers). Carries
  ``source: str`` -- a code-source identifier like ``"agentworks.catalog"``.
- ``auto-declared``: synthesized by a kind's miss policy during
  ``Registry.finalize()`` to satisfy a reference that didn't resolve to
  any published Resource. Carries ``source: tuple[str, str]`` -- the first
  matching reference's ``(kind, name)`` source per the config-load walk
  order.

Two variants are reserved for the plugin system and are not constructible
until that lands: ``system-plugin`` (distributed with the app but
separable, possibly requiring explicit enable) and ``external-plugin``
(installed from outside sources). They are documented here so display
vocabulary and operator expectations are stable; the plugin effort adds
them to the ``Literal`` and gives them factory classmethods.

The framework's ``Origin`` is distinct from the Config layer's
``SourceLocation`` so the two layers can evolve independently. Operators see
``Origin`` (rendered as e.g., ``"operator-declared (config.toml:42)"`` or
``"auto-declared by vm-template:azure-prod"``) in ``agw doctor``,
``agw secret list``, and ``agw secret describe``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Origin:
    """Per-Resource provenance record. Construct via the
    ``operator_declared`` / ``built_in`` / ``auto_declared``
    classmethods; never instantiate directly.

    The variant-specific fields are typed as broad unions on the class so
    one dataclass can express all three shapes. The factory classmethods
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
    """

    variant: Literal["operator-declared", "built-in", "auto-declared"]
    file: Path | None = None
    line: int | None = None
    source: str | tuple[str, str] | None = None

    @classmethod
    def operator_declared(cls, *, file: Path, line: int) -> Origin:
        """Operator-typed Resource (Config or future operator-publishers)."""
        return cls(variant="operator-declared", file=file, line=line)

    @classmethod
    def built_in(cls, *, source: str) -> Origin:
        """Resource shipped with the app itself (catalog publisher,
        app-bundled publishers). ``source`` is a code-source identifier
        like ``"agentworks.catalog"``. Plugin-shipped resources will NOT
        use this variant; they get the reserved ``system-plugin`` /
        ``external-plugin`` variants when the plugin system lands.
        """
        return cls(variant="built-in", source=source)

    @classmethod
    def auto_declared(cls, *, source: tuple[str, str]) -> Origin:
        """Framework-synthesized Resource (auto-declared by a kind's miss
        policy during ``Registry.finalize()``). ``source`` is the first
        matching reference's ``(kind, name)`` per config-load walk
        order; the full set of matching references is recorded in the
        Resource's ``references`` tuple, not here.
        """
        return cls(variant="auto-declared", source=source)
