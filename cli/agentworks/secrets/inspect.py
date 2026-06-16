"""Service-layer introspection for ``agw secret`` commands.

Builds a structured view of every declared secret across every active
backend in the configured chain. CLI surfaces (``agw secret list``)
consume the returned dataclasses; the build logic stays here so it's
testable without rendering.

The table is the only discovery path operators have for "which env var
is this secret read from?" -- per the env-and-secrets SDD design,
env-var is just another backend, so the table treats every backend
uniformly via the ``SecretSource.describe_lookup`` protocol method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config


@dataclass(frozen=True)
class SecretCell:
    """One (secret, backend) cell in the table."""

    backend_kind: str
    would_attempt: bool
    """False = this backend won't attempt this secret (mapping=false or no
    default convention and no explicit mapping). True = backend will try."""
    identifier: str | None
    """Backend's lookup identifier for this secret (env var name, op:// URI,
    vault path, ...). None means the backend has no static identifier --
    prompt always attempts but doesn't know what to look up until run time."""


@dataclass(frozen=True)
class SecretRow:
    """One declared secret, with a cell per active backend."""

    name: str
    description: str
    cells: tuple[SecretCell, ...]


@dataclass(frozen=True)
class SecretTable:
    """Full table for ``agw secret list``.

    ``backend_kinds`` lists the columns in the configured chain order
    (precedence order). ``rows`` is one per declared secret. Empty rows
    or backends are surfaced by the renderer, not filtered here.
    """

    backend_kinds: tuple[str, ...]
    rows: tuple[SecretRow, ...]


def build_secret_table(config: Config) -> SecretTable:
    """Build a (secrets x backends) table from the loaded Config.

    Walks ``config.secret_resolver``'s active source chain in precedence
    order; for each declared secret asks each source whether it would
    attempt and what identifier it would use. Pure config-derived; never
    probes the backend or resolves a value.
    """
    resolver = config.secret_resolver
    sources = resolver.sources
    backend_kinds = tuple(s.kind for s in sources)

    rows: list[SecretRow] = []
    for name in sorted(config.secrets):
        decl = config.secrets[name]
        cells = tuple(
            SecretCell(
                backend_kind=s.kind,
                would_attempt=s.would_attempt(decl),
                identifier=s.describe_lookup(decl),
            )
            for s in sources
        )
        rows.append(
            SecretRow(name=name, description=decl.description, cells=cells)
        )

    return SecretTable(backend_kinds=backend_kinds, rows=tuple(rows))
