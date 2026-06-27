"""Service-layer introspection and rendering for ``agw secret`` commands.

``build_secret_table`` / ``render_secret_table`` back ``agw secret list``;
``describe_secret`` / ``render_secret_description`` back the Phase-1e
``agw secret describe <name>``. Both follow the same "build structured
view, render via ``agentworks.output``" pattern.

Per FRD R10, neither command prompts the operator nor resolves a secret
value; they report state by walking the registry and the resolver's
configured backend chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources import Registry
    from agentworks.resources.requirement import UsageEntry


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
    origin_text: str
    cells: tuple[SecretCell, ...]


@dataclass(frozen=True)
class SecretTable:
    """Full table for ``agw secret list``.

    ``backend_kinds`` lists the columns in the configured chain order
    (precedence order). ``rows`` is one per Registry-published secret
    (operator-declared and auto-declared alike). ``operator_count`` /
    ``auto_count`` drive the header summary per FRD R10.
    """

    backend_kinds: tuple[str, ...]
    rows: tuple[SecretRow, ...]
    operator_count: int
    auto_count: int


def build_secret_table(config: Config, registry: Registry) -> SecretTable:
    """Build a (secrets x backends) table from the Registry.

    Phase 1e of the Resource Registry SDD: the table iterates the
    Registry's ``"secret"`` kind so auto-declared secrets surface in
    ``agw secret list`` alongside operator-declared ones (FRD R10).
    Each row carries an Origin string so operators can tell which
    secret came from where; the header summary reports the counts.

    Walks ``config.secret_resolver``'s active source chain in
    precedence order; for each Registry-published secret asks each
    source whether it would attempt and what identifier it would use.
    Pure config + registry derived; never probes the backend or
    resolves a value.
    """
    resolver = config.secret_resolver
    sources = resolver.sources
    backend_kinds = tuple(s.kind for s in sources)

    operator_count = 0
    auto_count = 0
    rows: list[SecretRow] = []
    for decl in sorted(registry.iter_kind("secret"), key=lambda d: d.name):
        origin_text = _format_origin(decl)
        # Variant-based counter; defensive on missing origin.
        variant = getattr(getattr(decl, "origin", None), "variant", None)
        if variant == "operator-declared":
            operator_count += 1
        elif variant == "auto-declared":
            auto_count += 1
        # code-declared not yet a path for secrets; Phase 2b's catalog
        # publisher emits code-declared but for non-secret kinds.

        cells = tuple(
            SecretCell(
                backend_kind=s.kind,
                would_attempt=s.would_attempt(decl),
                identifier=s.describe_lookup(decl),
            )
            for s in sources
        )
        rows.append(
            SecretRow(
                name=decl.name,
                description=decl.description,
                origin_text=origin_text,
                cells=cells,
            )
        )

    return SecretTable(
        backend_kinds=backend_kinds,
        rows=tuple(rows),
        operator_count=operator_count,
        auto_count=auto_count,
    )


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
        output.info("No secrets in the resource registry.")
        return
    if not table.backend_kinds:
        output.info(
            "No active secret backends. Set [secret_config].backends in your "
            "config (or leave it unset to use the default chain).",
        )
        return

    # Header summary per FRD R10: total + per-origin counts.
    total = len(table.rows)
    parts: list[str] = []
    if table.operator_count:
        parts.append(f"{table.operator_count} operator-declared")
    if table.auto_count:
        parts.append(f"{table.auto_count} auto-declared")
    breakdown = f" ({', '.join(parts)})" if parts else ""
    output.info(f"{total} secret{'s' if total != 1 else ''}{breakdown}")
    output.info("")

    # Render cells to strings up front so column widths can be measured.
    rendered: list[tuple[str, ...]] = []
    for row in table.rows:
        cells: list[str] = [row.name, row.origin_text]
        for cell in row.cells:
            if not cell.would_attempt:
                cells.append("disabled")
            elif cell.identifier is not None:
                cells.append(cell.identifier)
            else:
                cells.append("enabled")
        rendered.append(tuple(cells))

    headers = ("NAME", "ORIGIN", *table.backend_kinds)
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


# -- agw secret describe ---------------------------------------------------


@dataclass(frozen=True)
class BackendMapping:
    """One backend's view of a secret for the describe view's mapping table.

    Fields express what the backend would do at resolution time without
    actually resolving (no I/O):

    - ``backend_kind``: the backend identifier (``"env-var"``,
      ``"prompt"``, future backends).
    - ``would_attempt``: True if the backend would try this secret at
      resolution time. False = explicit opt-out via
      ``backend_mappings.<kind> = false``, or the backend has no
      default convention for this secret and no operator override.
    - ``identifier``: the backend's lookup identifier (env-var name,
      ``op://`` URI, vault path, etc.) when meaningful. ``None`` for
      backends with no static identifier (prompt) or for backends that
      won't attempt.
    """

    backend_kind: str
    would_attempt: bool
    identifier: str | None


@dataclass(frozen=True)
class ResolutionPreview:
    """What the active backend chain would do at resolution time.

    - ``resolved_by``: the kind of the first backend in the chain that
      would yield a value for this secret right now (e.g. ``"env-var"``
      when ``AW_SECRET_<NAME>`` is set, ``"prompt"`` when the chain
      falls through to an interactive prompt). ``None`` = no active
      backend would resolve the secret.
    - ``available``: True iff ``resolved_by`` is not None. Convenience
      flag for the renderer (mirrors ``resolved_by is not None``).
    """

    resolved_by: str | None
    available: bool


@dataclass(frozen=True)
class SecretDescription:
    """Structured per-secret detail view backing ``agw secret describe``.

    ``origin_text`` is pre-rendered ("operator-declared (config.toml:42)"
    or "auto-declared by vm_template:default") so the CLI renderer is
    purely a formatter. The supplemental "also required by ..." list
    is derived from ``usages`` at render time. ``hint`` is the
    operator-set prompt hint (``[secrets.<name>].hint``), surfaced for
    debugging "why isn't my prompt showing the helpful hint" without
    triggering a prompt.
    """

    name: str
    kind: str
    origin_text: str
    description: str
    hint: str | None
    usages: tuple[UsageEntry, ...]
    backend_mappings: tuple[BackendMapping, ...]
    resolution: ResolutionPreview


def _format_origin(decl: object) -> str:
    """Render a Resource's ``origin: Origin`` field as the
    ``"operator-declared (path:line)"`` / ``"auto-declared by k:n"`` /
    ``"code-declared by source"`` string per FRD R10.

    The path is rendered relative to ``$HOME`` with a ``~/`` prefix
    when it falls under the user's home directory; otherwise the bare
    absolute path. (Operator configs live at ``~/.config/agentworks/
    config.toml`` so the common case becomes the short, FRD-example-
    shaped form.)
    """
    origin = getattr(decl, "origin", None)
    if origin is None:
        # Defensive: a Resource constructed outside the framework path
        # may not carry an origin yet. Surfaces as "unknown" rather
        # than crashing.
        return "unknown"
    variant = getattr(origin, "variant", None)
    if variant == "operator-declared":
        file = origin.file
        line = origin.line
        if file is not None and line:
            return f"operator-declared ({_format_file_path(file)}:{line})"
        return "operator-declared"
    if variant == "auto-declared":
        source = origin.source
        if isinstance(source, tuple) and len(source) == 2:
            return f"auto-declared by {source[0]}:{source[1]}"
        return "auto-declared"
    if variant == "code-declared":
        source = origin.source
        return f"code-declared by {source}" if source else "code-declared"
    # Unreachable under the Origin dataclass's Literal[...] variant
    # constraint; surface loudly if it ever fires rather than emitting
    # a confusing string in front of an operator.
    raise AssertionError(f"unhandled Origin variant: {variant!r}")


def _format_file_path(file: object) -> str:
    """Render a file path operator-friendly: ``~/path`` when under
    ``$HOME``, else the bare absolute path. Falls back to ``str(file)``
    on any unexpected shape (defensive; Origin's file field is typed
    ``Path | None`` so this only fires on truly weird construction).
    """
    from pathlib import Path

    try:
        path = Path(str(file))
        home = Path.home()
        if path.is_absolute():
            try:
                return f"~/{path.relative_to(home)}"
            except ValueError:
                return str(path)
        return str(path)
    except Exception:
        return str(file)


def describe_secret(
    registry: Registry,
    config: Config,
    name: str,
) -> SecretDescription:
    """Build a ``SecretDescription`` for one secret in the registry.

    Per FRD R10. Pure config + registry derived; no I/O, no prompting,
    no resolution. Raises ``NotFoundError`` if ``name`` isn't a
    published secret -- typed at the service layer so CLI / future
    web/API clients all see the same error shape (per the project's
    service-layer-is-the-authority rule). The ``hint`` attribute
    points operators at ``agw secret list``.
    """
    from agentworks.errors import NotFoundError

    try:
        decl = registry.lookup("secret", name)
    except KeyError:
        raise NotFoundError(
            f"secret {name!r} is not in the resource registry",
            entity_kind="secret",
            entity_name=name,
            hint="check `agw secret list` for declared and auto-declared names",
        ) from None
    origin_text = _format_origin(decl)
    description = getattr(decl, "description", "") or ""
    # Usages come from the finalize pass's attachment (one entry per
    # requirement that contributed). Defensive: a Resource constructed
    # outside the framework path may not have the field.
    usages: tuple[UsageEntry, ...] = tuple(getattr(decl, "usage", ()))

    # Backend mappings: walk the active source chain and ask each
    # source how it would handle this secret.
    mappings: list[BackendMapping] = []
    for source in config.secret_resolver.sources:
        mappings.append(
            BackendMapping(
                backend_kind=source.kind,
                would_attempt=source.would_attempt(decl),
                identifier=source.describe_lookup(decl),
            )
        )

    # Resolution preview: which active backend would actually yield a
    # value right now. Delegate to the resolver's ``preview_resolution``
    # so the answer reflects runtime presence (e.g. is the env var set?),
    # not just whether the backend is configured. The local
    # ``backend_mappings`` list above already covers configuration shape;
    # this layer is the live probe.
    preview_kind = config.secret_resolver.preview_resolution(decl)
    resolved_by = preview_kind
    available = preview_kind is not None

    return SecretDescription(
        name=name,
        kind="secret",
        origin_text=origin_text,
        description=description,
        hint=getattr(decl, "hint", None),
        usages=usages,
        backend_mappings=tuple(mappings),
        resolution=ResolutionPreview(
            resolved_by=resolved_by,
            available=available,
        ),
    )


def render_secret_description(desc: SecretDescription) -> None:
    """Emit a ``SecretDescription`` as four operator-friendly sections:
    header, usages, backend mappings, resolution preview. Mirrors FRD
    R10's documented shape.
    """
    # --- Header ---
    output.info(f"Secret: {desc.name}")
    output.info(f"  Kind:        {desc.kind}")
    output.info(f"  Origin:      {desc.origin_text}")
    if desc.description:
        output.info(f"  Description: {desc.description}")
    else:
        output.info("  Description: (none)")
    if desc.hint:
        output.info(f"  Hint:        {desc.hint}")

    # --- Usages ---
    output.info("")
    output.info("Usages:")
    if not desc.usages:
        output.info("  (none recorded)")
    else:
        # Dedupe by (source, text) preserving first-encounter order.
        seen: set[tuple[tuple[str, str], str]] = set()
        for entry in desc.usages:
            key = (entry.source, entry.text)
            if key in seen:
                continue
            seen.add(key)
            src = f"{entry.source[0]}:{entry.source[1]}"
            output.info(f"  - {src} -- {entry.text}")

    # --- Backend mappings ---
    output.info("")
    output.info("Backend mappings:")
    if not desc.backend_mappings:
        output.info("  (no active backends in [secret_config].backends)")
    else:
        for mapping in desc.backend_mappings:
            if not mapping.would_attempt:
                status = "no mapping (skipped)"
            elif mapping.identifier is not None:
                status = mapping.identifier
            else:
                status = "(prompt at resolution time)"
            output.info(f"  - {mapping.backend_kind}: {status}")

    # --- Resolution preview ---
    output.info("")
    output.info("Resolution preview:")
    if not desc.resolution.available:
        output.info("  not available in any active backend")
    else:
        output.info(f"  would resolve via {desc.resolution.resolved_by}")
