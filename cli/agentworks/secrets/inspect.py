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
from agentworks.resources.inspect import used_by_for
from agentworks.resources.kinds.secret import SECRET_KIND_NAME
from agentworks.resources.render import format_origin_line

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources import Registry
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry


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
    """One declared secret, with a cell per active backend.

    ``description`` is the operator-supplied text for operator-declared
    secrets, or the framework-synthesized ``"auto-declared by k:n[ (and
    N more)]"`` for auto-declared ones (set during ``Registry.finalize``
    so the list view's Description column is always populated).
    """

    name: str
    description: str
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
    for decl in sorted(registry.iter_kind(SECRET_KIND_NAME), key=lambda d: d.name):
        # Variant-based counter; defensive on missing origin.
        variant = getattr(getattr(decl, "origin", None), "variant", None)
        if variant == "operator-declared":
            operator_count += 1
        elif variant == "auto-declared":
            auto_count += 1
        # built-in not yet a path for secrets; Phase 2b's catalog
        # publisher emits built-in but for non-secret kinds.

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
        cells: list[str] = [row.name, row.description]
        for cell in row.cells:
            if not cell.would_attempt:
                cells.append("disabled")
            elif cell.identifier is not None:
                cells.append(cell.identifier)
            else:
                cells.append("enabled")
        rendered.append(tuple(cells))

    headers = ("NAME", "DESCRIPTION", *table.backend_kinds)
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

    ``origin`` is the raw structured ``Origin``; the renderer formats it
    as a multi-line block (variant + sub-fields). ``description`` is the
    operator-supplied text or the framework-synthesized text for
    auto-declared secrets (set during ``Registry.finalize``). ``hint``
    is the operator-set prompt hint (``[secrets.<name>].hint``),
    surfaced for debugging "why isn't my prompt showing the helpful
    hint" without triggering a prompt.

    ``references`` is the inbound reference list (config points that
    name this secret); ``used_by`` is the live DB instances that
    depend on this secret per the current config (projected via the
    secret kind's ``instances`` hook). ``used_by`` is ``None`` when
    ``describe_secret`` was called without a ``db`` -- the renderer
    omits the "Used by:" section in that case.
    """

    name: str
    kind: str
    origin: Origin | None
    description: str
    hint: str | None
    references: tuple[ReferenceEntry, ...]
    used_by: tuple[InstanceRef, ...] | None
    backend_mappings: tuple[BackendMapping, ...]
    resolution: ResolutionPreview


def describe_secret(
    registry: Registry,
    config: Config,
    name: str,
    db: Database | None = None,
) -> SecretDescription:
    """Build a ``SecretDescription`` for one secret in the registry.

    Per FRD R10. Pure config + registry derived; no I/O, no prompting,
    no resolution. Raises ``NotFoundError`` if ``name`` isn't a
    published secret -- typed at the service layer so CLI / future
    web/API clients all see the same error shape (per the project's
    service-layer-is-the-authority rule). The ``hint`` attribute
    points operators at ``agw secret list``.

    ``db`` is optional: when provided, the ``used_by`` field is
    populated with the sessions whose subgraph reaches this secret
    (via the secret kind's ``instances`` hook, shared with
    ``agw resource describe``). When ``None``, ``used_by`` stays
    ``None`` and the renderer omits the "Used by:" section.
    """
    from agentworks.errors import NotFoundError

    try:
        decl = registry.lookup(SECRET_KIND_NAME, name)
    except KeyError:
        raise NotFoundError(
            f"secret {name!r} is not in the resource registry",
            entity_kind=SECRET_KIND_NAME,
            entity_name=name,
            hint="check `agw secret list` for declared and auto-declared names",
        ) from None
    origin = getattr(decl, "origin", None)
    description = getattr(decl, "description", "") or ""
    # References come from the finalize pass's attachment (one entry per
    # reference that resolved here). Defensive: a Resource constructed
    # outside the framework path may not have the field.
    references: tuple[ReferenceEntry, ...] = tuple(getattr(decl, "references", ()))

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
        kind=SECRET_KIND_NAME,
        origin=origin,
        description=description,
        hint=getattr(decl, "hint", None),
        references=references,
        used_by=used_by_for(db, registry, SECRET_KIND_NAME, decl),
        backend_mappings=tuple(mappings),
        resolution=ResolutionPreview(
            resolved_by=resolved_by,
            available=available,
        ),
    )


def render_secret_description(desc: SecretDescription) -> None:
    """Emit a ``SecretDescription`` as operator-friendly sections:
    header, referenced by, used by (when db provided), backend
    mappings, resolution preview. Mirrors FRD R10's documented shape.
    """
    # --- Header ---
    output.info(f"Secret: {desc.name}")
    output.detail(f"Kind: {desc.kind}")
    if desc.description:
        output.detail(f"Description: {desc.description}")
    else:
        output.detail("Description: (none)")
    output.detail(f"Origin: {format_origin_line(desc.origin)}")
    if desc.hint:
        output.detail(f"Hint: {desc.hint}")

    # --- Referenced by ---
    output.info("")
    output.info("Referenced by:")
    if not desc.references:
        output.detail("(none recorded)")
    else:
        # Dedupe by (source, usage) preserving first-encounter order.
        seen: set[tuple[tuple[str, str], str]] = set()
        for entry in desc.references:
            key = (entry.source, entry.usage)
            if key in seen:
                continue
            seen.add(key)
            src = f"{entry.source[0]}:{entry.source[1]}"
            output.detail(f"- {src} -- {entry.usage}")

    # --- Used by (dynamic, per current config) ---
    # Only rendered when describe_secret was called with a db. Same
    # projection shape as agw resource describe's Used by section; the
    # annotation is in the section header so the projection-vs-
    # materialized signal is visible at-a-glance.
    if desc.used_by is not None:
        output.info("")
        output.info("Used by (per current config):")
        if not desc.used_by:
            output.detail("(no live sessions reach this secret)")
        else:
            # Group by instance_kind for readability; preserve
            # first-encounter order within a kind. Today the secret
            # kind emits only session InstanceRefs, but grouping keeps
            # the rendering identical to agw resource describe's shape
            # so a future SDD that emits other instance kinds (agents,
            # VMs) slots in without renderer changes.
            grouped: dict[str, list[str]] = {}
            for ref in desc.used_by:
                grouped.setdefault(ref.instance_kind, []).append(ref.instance_name)
            for instance_kind in grouped:
                for instance_name in grouped[instance_kind]:
                    output.detail(f"- {instance_kind}:{instance_name}")

    # --- Backend mappings ---
    output.info("")
    output.info("Backend mappings:")
    if not desc.backend_mappings:
        output.detail("(no active backends in [secret_config].backends)")
    else:
        for mapping in desc.backend_mappings:
            if not mapping.would_attempt:
                status = "no mapping (skipped)"
            elif mapping.identifier is not None:
                status = mapping.identifier
            else:
                status = "(prompt at resolution time)"
            output.detail(f"- {mapping.backend_kind}: {status}")

    # --- Resolution preview ---
    output.info("")
    output.info("Resolution preview:")
    if not desc.resolution.available:
        output.detail("not available in any active backend")
    else:
        output.detail(f"would resolve via {desc.resolution.resolved_by}")
