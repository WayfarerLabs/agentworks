"""``Origin``: the per-Resource record of where a Resource came from.

Set once when the Resource is added to the Registry (via
``Registry.add(origin=...)`` for operator- and code-declared Resources, or
via the kind's ``synthesize`` for auto-declared ones); never mutated.

Three variants:

- ``operator-declared``: from operator config (Config publisher today;
  future YAML manifest publishers later). Carries ``file: Path`` + ``line:
  int`` for traceability. Built from the Config-layer ``SourceLocation``
  during ``Config.publish_to``.
- ``code-declared``: from a code publisher (the Phase 2b catalog
  publisher today; future plugin publishers later). Carries ``source: str``
  -- a code-source identifier like ``"agentworks.catalog"``.
- ``auto-declared``: synthesized by a kind's miss policy during
  ``Registry.finalize()`` to satisfy a reference that didn't resolve to
  any published Resource. Carries ``source: tuple[str, str]`` -- the first
  matching reference's ``(kind, name)`` source per the config-load walk
  order.

The framework's ``Origin`` is distinct from the Config layer's
``SourceLocation`` so the two layers can evolve independently. Operators see
``Origin`` (rendered as e.g., ``"operator-declared (config.toml:42)"`` or
``"auto-declared by vm_template:azure-prod"``) in ``agw doctor``,
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
    ``operator_declared`` / ``code_declared`` / ``auto_declared``
    classmethods; never instantiate directly.

    The variant-specific fields are typed as broad unions on the class so
    one dataclass can express all three shapes. The factory classmethods
    are the only correct construction path; they pin the right fields per
    variant. Inspect ``variant`` first; the other fields' contracts depend
    on it.

    Variant contracts:

    - ``operator-declared``: ``file`` and ``line`` are populated; ``source``
      is ``None``.
    - ``code-declared``: ``source`` is a ``str``; ``file`` and ``line`` are
      ``None``.
    - ``auto-declared``: ``source`` is a ``tuple[str, str]``; ``file`` and
      ``line`` are ``None``.
    """

    variant: Literal["operator-declared", "code-declared", "auto-declared"]
    file: Path | None = None
    line: int | None = None
    source: str | tuple[str, str] | None = None

    @classmethod
    def operator_declared(cls, *, file: Path, line: int) -> Origin:
        """Operator-typed Resource (Config or future operator-publishers)."""
        return cls(variant="operator-declared", file=file, line=line)

    @classmethod
    def code_declared(cls, *, source: str) -> Origin:
        """Code-published Resource (catalog publisher in Phase 2b; future
        plugin publishers). ``source`` is a code-source identifier like
        ``"agentworks.catalog"``.
        """
        return cls(variant="code-declared", source=source)

    @classmethod
    def auto_declared(cls, *, source: tuple[str, str]) -> Origin:
        """Framework-synthesized Resource (auto-declared by a kind's miss
        policy during ``Registry.finalize()``). ``source`` is the first
        matching reference's ``(kind, name)`` per config-load walk
        order; the full set of matching requirements is recorded in the
        Resource's ``usage`` list, not here.
        """
        return cls(variant="auto-declared", source=source)
