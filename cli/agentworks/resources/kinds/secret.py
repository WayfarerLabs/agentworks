"""``SecretKind``: the framework's strategy for the ``"secret"`` kind.

Miss policy is ``auto-declare`` with no name restriction -- any name a
``SecretRequirement`` references will be auto-synthesized when not
operator-declared. The synthesized ``SecretDecl`` carries an empty
``description``; operators are warned (per FRD R9) that auto-declared
secrets should be promoted to explicit ``[secrets.<name>]`` blocks so they
can carry a description.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import KIND_REGISTRY
from agentworks.resources.origin import Origin
from agentworks.resources.requirement import UsageEntry
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _SecretKind:
    """Implementation of ``ResourceKind`` for ``"secret"``. Module-private;
    callers reach this through ``KIND_REGISTRY["secret"]``.
    """

    kind: str = "secret"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = None  # None = any name accepted

    def synthesize(self, requirements: Sequence[ResourceRequirement]) -> SecretDecl:
        """Build a ``SecretDecl`` for an auto-declared secret. ``requirements``
        is non-empty (the Registry never calls ``synthesize`` for an
        unreferenced name) and ordered by config-load walk order.
        """
        first = requirements[0]
        return SecretDecl(
            name=first.name,
            description="",
            origin=Origin.auto_declared(source=first.source),
            usage=tuple(UsageEntry(source=r.source, text=r.usage) for r in requirements),
        )


KIND_REGISTRY["secret"] = _SecretKind()
