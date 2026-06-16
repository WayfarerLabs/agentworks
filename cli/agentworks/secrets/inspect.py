"""Service-layer introspection and rendering for ``agw secret`` commands.

``build_secret_table`` produces the structured view (tested without
rendering); ``render_secret_table`` formats it for an operator. The CLI
command stays thin: build, render, done. Matches the shape of
``env.show.show_env`` (build rows + emit through ``agentworks.output``).

The table is the only discovery path operators have for "which env var
is this secret read from?" -- per the env-and-secrets SDD design,
env-var is just another backend, so the table treats every backend
uniformly via the ``SecretSource.describe_lookup`` protocol method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output

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


def render_secret_table(table: SecretTable) -> None:
    """Emit the table as operator-friendly output.

    Empty-state messages match the loader's defaults so an operator who
    runs ``agw secret list`` on a fresh config sees one of:

    - ``No secrets declared in config.`` -- no `[secrets.<name>]` tables.
    - ``No active secret backends.`` -- ``[secret_config].backends = []``.

    Otherwise a header + table with one column per active backend in
    chain order. Cell semantics: an explicit identifier
    (``AW_SECRET_X``, ``op://...``) when the backend has one;
    ``disabled`` when ``would_attempt`` is False;
    ``enabled`` for backends that always attempt without a static key
    (e.g. ``prompt``).
    """
    if not table.rows:
        output.info("No secrets declared in config.")
        return
    if not table.backend_kinds:
        output.info(
            "No active secret backends. Set [secret_config].backends in your "
            "config (or leave it unset to use the default chain).",
        )
        return

    # Render cells to strings up front so column widths can be measured.
    rendered: list[tuple[str, ...]] = []
    for row in table.rows:
        cells: list[str] = [row.name]
        for cell in row.cells:
            if not cell.would_attempt:
                cells.append("disabled")
            elif cell.identifier is not None:
                cells.append(cell.identifier)
            else:
                cells.append("enabled")
        rendered.append(tuple(cells))

    headers = ("NAME", *table.backend_kinds)
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rendered))
        for i in range(len(headers))
    ]

    def _fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    output.info(_fmt(headers))
    output.info(_fmt(tuple("-" * w for w in widths)))
    for r in rendered:
        output.info(_fmt(r))
