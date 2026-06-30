"""``SecretKind``: the framework's strategy for the ``"secret"`` kind.

Miss policy is ``auto-declare`` with no name restriction -- any name a
``SecretReference`` references will be auto-synthesized when not
operator-declared. The synthesized ``SecretDecl`` carries an empty
``description``; operators are warned (per FRD R9) that auto-declared
secrets should be promoted to explicit ``[secrets.<name>]`` blocks so they
can carry a description.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.resources.origin import Origin
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


SECRET_KIND_NAME = "secret"
"""Single source of truth for the ``"secret"`` kind identifier. Callers
that need to render or compare against the kind name import this rather
than re-typing the literal -- a hypothetical rename then flows through
every site by construction."""


@dataclass(frozen=True)
class _SecretKind:
    """Implementation of ``ResourceKind`` for ``"secret"``. Module-private;
    callers reach this through ``KIND_REGISTRY["secret"]``.
    """

    kind: str = SECRET_KIND_NAME
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = None  # None = any name accepted

    def synthesize(self, requirements: Sequence[ResourceReference]) -> SecretDecl:
        """Build a ``SecretDecl`` for an auto-declared secret. ``requirements``
        is non-empty in normal operation (the Registry calls ``synthesize``
        only when an incoming reference triggered the miss policy) and
        ordered by config-load walk order.

        Only ``origin`` (auto-declared, source = first matching
        reference's source) is attached here. ``usage`` is centralized
        in ``Registry.finalize``'s post-stabilization pass so the kind
        doesn't need to know the final reference map -- a synthesized
        Resource that goes on to publish requirements of its own may
        gather later incoming edges that this initial call can't see.

        Raises ``NoUnreferencedDefaultError`` if called with
        ``requirements=()`` -- the secret kind has no concept of an
        unreferenced default (``auto_declare_names = None``), so the
        framework never calls this path; the explicit error is defensive
        in case the kind's auto-declare configuration ever changes.
        """
        if not requirements:
            raise NoUnreferencedDefaultError(
                "the secret kind has no reserved default name; "
                "synthesize requires at least one reference"
            )
        first = requirements[0]
        return SecretDecl(
            name=first.name,
            description="",
            origin=Origin.auto_declared(source=first.source),
        )


KIND_REGISTRY[SECRET_KIND_NAME] = _SecretKind()
