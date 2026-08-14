"""Service-layer introspection and rendering for ``agw secret`` commands.

``build_secret_table`` / ``render_secret_table`` back ``agw secret list``;
``describe_secret`` / ``render_secret_description`` back
``agw secret describe <name>``. Both follow the same "build structured
view, render via ``agentworks.output``" pattern.

Neither command prompts the operator nor resolves a secret value for
display; both consume the pure active-source projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError
from agentworks.machine_output import (
    JsonObject,
    JsonValue,
    project_instance_references,
    project_origin,
    project_references,
)
from agentworks.resources.inspect import used_by_for
from agentworks.resources.render import format_origin_line, format_reference_entry
from agentworks.secrets.kinds import SECRET_KIND_NAME
from agentworks.secrets.preview import PreviewCategory, ResolutionPreview, preview_resolution
from agentworks.secrets.sources import SourceProvenance, source_provenance

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.origin import Origin
    from agentworks.resources import Registry
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.reference import ReferenceEntry


@dataclass(frozen=True, slots=True)
class SecretSourceCell:
    """One (secret, source) cell in the list table."""

    source: str
    would_attempt: bool
    """False = this backend won't attempt this secret (mapping=false or no
    default convention and no explicit mapping). True = backend will try."""
    identifier: str | None
    """Backend's lookup identifier for this secret (env var name, op:// URI,
    vault path, ...). None means the backend has no static identifier --
    prompt always attempts but doesn't know what to look up until run time."""
    not_ready_reason: str | None
    """The backend node's stored readiness reason (off the graph), or None when
    the backend is ready. Orthogonal to ``would_attempt`` (readiness is host
    usability; would-attempt is a pure (secret, mapping) function): a not-ready
    backend still "would attempt" a mapped secret, but cannot run here, so the
    grid shows ``not ready: <reason>`` and it wins over the identifier (R9.7)."""


@dataclass(frozen=True)
class SecretRow:
    """One declared secret, with a cell per active source.

    ``description`` is the operator-supplied text for operator-declared
    secrets, or the framework-synthesized ``"auto-declared by k:n[ (and
    N more)]"`` for auto-declared ones (set during ``Registry.finalize``
    so the list view's Description column is always populated).
    """

    name: str
    description: str
    cells: tuple[SecretSourceCell, ...]


@dataclass(frozen=True, slots=True)
class SecretTable:
    """Full table for ``agw secret list``.

    ``sources`` lists the columns (source names) in the configured
    chain order (precedence order). ``rows`` is one per
    Registry-published secret (operator-declared and auto-declared
    alike). ``operator_count`` / ``auto_count`` drive the header
    summary.
    """

    sources: tuple[str, ...]
    rows: tuple[SecretRow, ...]
    operator_count: int
    auto_count: int


def secret_table_data(table: SecretTable) -> JsonObject:
    """Project list facts into the closed ``secret.list`` JSON data shape."""
    return {
        "sources": list(table.sources),
        "secrets": [
            {
                "name": row.name,
                "description": row.description,
                "sources": [
                    {
                        "source": cell.source,
                        "would_attempt": cell.would_attempt,
                        "identifier": cell.identifier,
                        "not_ready_reason": cell.not_ready_reason,
                    }
                    for cell in row.cells
                ],
            }
            for row in table.rows
        ],
        "counts": {
            "operator_declared": table.operator_count,
            "auto_declared": table.auto_count,
        },
    }


def build_secret_table(config: Config, registry: Registry) -> SecretTable:
    """Build a (secrets x sources) table.

    The table iterates the Registry's ``"secret"`` kind so auto-declared
    secrets surface in ``agw secret list`` alongside operator-declared
    ones. Each row carries an Origin string so operators can
    tell which secret came from where; the header summary reports the
    counts.

    Walks active sources in precedence order and consumes only their pure
    lookup projections. No client is constructed and no value is read.
    """
    from agentworks.secrets.resolve import _BackendProtocolError, _lookup_projection, active_sources

    sources = active_sources(config, registry)
    source_names = tuple(source.name for source in sources)

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
        # built-in is not yet a path for secrets; other publishers emit
        # built-in origins (bundled manifests, capability rows) but only
        # for non-secret kinds.

        cells: list[SecretSourceCell] = []
        for source in sources:
            try:
                request, identifier = _lookup_projection(decl, source)
            except _BackendProtocolError:
                raise StateError(f"secret source {source.name!r} violated the preview contract") from None
            would_attempt = request is not None
            cells.append(
                SecretSourceCell(
                    source=source.name,
                    would_attempt=would_attempt,
                    identifier=identifier,
                    not_ready_reason=source.readiness.reason,
                )
            )
        rows.append(
            SecretRow(
                name=decl.name,
                description=decl.description,
                cells=tuple(cells),
            )
        )

    return SecretTable(
        sources=source_names,
        rows=tuple(rows),
        operator_count=operator_count,
        auto_count=auto_count,
    )


_BACKEND_CELL_WIDTH = 40
# Wide enough for every practical name (the auto-declared git-token-* family
# included) while keeping the list view scannable when a name approaches the
# 253-char secret cap.
_NAME_CELL_WIDTH = 50
"""Cap for the per-backend identifier columns in the LIST view, so a long
``op://`` reference (optionally account-prefixed) or env-var name does not
blow the table width out. The single-secret DETAIL view is left uncapped."""


def render_secret_table(table: SecretTable) -> None:
    """Emit the table as operator-friendly output.

    Empty-state messages so an operator who runs ``agw secret list``
    on a fresh config sees one of:

    - ``No secrets in the resource registry.`` -- nothing declared or
      auto-declared.
    - ``No active secret sources.`` -- ``[secret_config].sources = []``.

    Otherwise a header + table with one column per active (opted-in) backend
    in chain order. Cell semantics, per R9.7 (never the overloaded
    ``enabled`` / ``disabled`` literals):

    - ``won't attempt`` when ``would_attempt`` is False (a ``false`` opt-out,
      or a mapping-required backend with no mapping);
    - ``not ready: <reason>`` when the backend's node is not-ready on this host
      (wins over the identifier: it cannot run here, R9.7);
    - the explicit identifier (``AW_SECRET_X``, ``op://...``) when the backend
      would attempt, is ready, and has a static lookup key;
    - ``would attempt`` when it would attempt and is ready but has no static
      key (e.g. ``prompt``).
    """
    if not table.rows:
        output.info("No secrets in the resource registry.")
        return
    if not table.sources:
        output.info(
            "No active secret sources. Set [secret_config].sources to source "
            "names (or leave it unset to use env-var then prompt).",
        )
        return

    # Header summary: total + per-origin counts.
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
        # Cap the name column too: secret names may run to
        # MAX_SECRET_NAME_LENGTH (253) since #275, and an uncapped name
        # would blow the table out exactly like a long op:// ref. The
        # DETAIL view (secret describe) keeps the full name.
        cells: list[str] = [output.truncate(row.name, _NAME_CELL_WIDTH), row.description]
        for cell in row.cells:
            if not cell.would_attempt:
                # Opted out for THIS secret (readiness is moot: it would not
                # attempt regardless of whether the host tool is present).
                cells.append("won't attempt")
            elif cell.not_ready_reason is not None:
                # Not-ready wins over the identifier (R9.7): a mapped backend
                # that cannot run here shows why, not the ref it can't use.
                cells.append(output.truncate(f"not ready: {cell.not_ready_reason}", _BACKEND_CELL_WIDTH))
            elif cell.identifier is not None:
                # Cap the identifier column so a long op:// ref (or
                # account-prefixed one) does not blow the table out. The
                # DETAIL view keeps the full identifier.
                cells.append(output.truncate(cell.identifier, _BACKEND_CELL_WIDTH))
            else:
                cells.append("would attempt")
        rendered.append(tuple(cells))

    headers = ("NAME", "DESCRIPTION", *table.sources)
    widths = [max(len(headers[i]), *(len(r[i]) for r in rendered)) for i in range(len(headers))]

    def _fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    output.info(_fmt(headers))
    output.info(_fmt(tuple("-" * w for w in widths)))
    for r in rendered:
        output.info(_fmt(r))


# -- agw secret describe ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceMapping:
    """One source's view of a secret for the describe mapping table.

    Fields express what the backend would do at resolution time without
    actually resolving (no I/O):

    - ``source``: the configured source name.
    - ``backend``: the backend name (``"env-var"``, ``"prompt"``, ...).
    - ``would_attempt``: True if the backend would try this secret at
      resolution time. False = explicit opt-out via
      ``backend_mappings.<source> = false``, or the selected backend has no
      default convention for this secret and no operator override.
    - ``identifier``: the backend's lookup identifier (env-var name,
      ``op://`` URI, vault path, etc.) when meaningful. ``None`` for
      backends with no static identifier (prompt) or for backends that
      won't attempt.
    """

    source: str
    backend: str
    provenance: SourceProvenance
    would_attempt: bool
    identifier: str | None
    not_ready_reason: str | None
    """The backend node's stored readiness reason, or None when ready. The
    mapping is still shown when not-ready (the config is real; it just cannot
    run here now), annotated ``(not ready: <reason>)`` (R9.1)."""


@dataclass(frozen=True)
class SecretDescription:
    """Structured per-secret detail view backing ``agw secret describe``.

    ``origin`` is the raw structured ``Origin``; the renderer formats it
    as a multi-line block (variant + sub-fields). ``description`` is the
    operator-supplied text or the framework-synthesized text for
    auto-declared secrets (set during ``Registry.finalize``). ``hint``
    is the operator-set prompt hint from the ``secret`` manifest,
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
    source_mappings: tuple[SourceMapping, ...]
    resolution: ResolutionPreview


def secret_description_data(description: SecretDescription) -> JsonObject:
    """Project describe facts into the closed ``secret.describe`` JSON data shape."""
    references: list[JsonValue] = [reference for reference in project_references(description.references)]
    used_by: list[JsonValue] | None = (
        None
        if description.used_by is None
        else [reference for reference in project_instance_references(description.used_by)]
    )
    return {
        "secret": {
            "name": description.name,
            "kind": description.kind,
            "origin": project_origin(description.origin),
            "description": description.description,
            "hint": description.hint,
            "references": references,
            "used_by": used_by,
            "source_mappings": [
                {
                    "source": mapping.source,
                    "backend": mapping.backend,
                    "provenance": mapping.provenance.value,
                    "would_attempt": mapping.would_attempt,
                    "identifier": mapping.identifier,
                    "not_ready_reason": mapping.not_ready_reason,
                }
                for mapping in description.source_mappings
            ],
            "resolution": {
                "category": description.resolution.category.value,
                "source": description.resolution.source,
                "identifier": description.resolution.identifier,
                "skipped_not_ready": [
                    {"source": skipped.source, "reason": skipped.reason}
                    for skipped in description.resolution.skipped_not_ready
                ],
            },
        },
    }


def describe_secret(
    config: Config,
    registry: Registry,
    name: str,
    db: Database | None = None,
) -> SecretDescription:
    """Build a ``SecretDescription`` for one secret in the registry.

    No prompting and no resolution for display. Raises
    ``NotFoundError`` if ``name`` isn't a
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
    # Plain reads: ``decl`` is a validated ``SecretDecl``, whose
    # ``description`` the kind re-declares as REQUIRED. A getattr default
    # here would turn a future rename into an empty string on screen
    # instead of an error (FR15: nothing downstream re-defaults a
    # modeled field).
    origin = decl.origin
    description = decl.description
    # Inbound references come off the dependency graph (one entry per
    # reference that resolved here), populated by the finalize pass.
    references: tuple[ReferenceEntry, ...] = registry.graph.dependents_of(SECRET_KIND_NAME, name)

    from agentworks.secrets.resolve import _BackendProtocolError, _lookup_projection, active_sources

    sources = active_sources(config, registry)
    mappings: list[SourceMapping] = []
    for source in sources:
        try:
            request, identifier = _lookup_projection(decl, source)
        except _BackendProtocolError:
            raise StateError(f"secret source {source.name!r} violated the preview contract") from None
        mappings.append(
            SourceMapping(
                source=source.name,
                backend=source.backend_class.name,
                provenance=source_provenance(source.source),
                would_attempt=request is not None,
                identifier=identifier,
                not_ready_reason=source.readiness.reason,
            )
        )
    resolution = preview_resolution(decl, sources)

    return SecretDescription(
        name=name,
        kind=SECRET_KIND_NAME,
        origin=origin,
        description=description,
        hint=decl.hint,
        references=references,
        used_by=used_by_for(db, registry, SECRET_KIND_NAME, decl),
        source_mappings=tuple(mappings),
        resolution=resolution,
    )


def render_secret_description(desc: SecretDescription) -> None:
    """Emit a ``SecretDescription`` as operator-friendly sections:
    header, referenced by, used by (when db provided), backend
    mappings, resolution preview.
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
        seen: set[str] = set()
        for entry in desc.references:
            line = format_reference_entry(entry)
            if line in seen:
                continue
            seen.add(line)
            output.detail(f"- {line}")

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
                    output.detail(f"- {instance_kind}/{instance_name}")

    # --- Backend mappings ---
    output.info("")
    output.info("Backend mappings:")
    if not desc.source_mappings:
        output.detail("(no active sources in [secret_config].sources)")
    else:
        provenance_labels = {
            SourceProvenance.OPERATOR_OVERRIDE: "operator override of synthesized default",
            SourceProvenance.SYNTHESIZED_DEFAULT: "synthesized default",
            SourceProvenance.DECLARED: "declared",
        }
        for mapping in desc.source_mappings:
            if not mapping.would_attempt:
                status = "won't attempt"
            elif mapping.identifier is not None:
                status = mapping.identifier
            else:
                status = "(prompt at resolution time)"
            # A would-attempt backend that cannot run here keeps its mapping
            # shown (the config is real) but is flagged not-ready (R9.1).
            if mapping.would_attempt and mapping.not_ready_reason is not None:
                status += f" (not ready: {mapping.not_ready_reason})"
            output.detail(f"- {mapping.source} ({mapping.backend}, {provenance_labels[mapping.provenance]}): {status}")

    # --- Resolution preview ---
    output.info("")
    output.info("Resolution preview:")
    # The honest offline layer first: backends the walk skips because they are
    # not-ready here (R9.6), then the optimistic would-resolve verdict under it.
    for skipped in desc.resolution.skipped_not_ready:
        output.detail(f"- skipped {skipped.source}: not ready: {skipped.reason}")
    if desc.resolution.category is not PreviewCategory.ATTEMPTABLE:
        output.detail("not attemptable through any active source")
    else:
        output.detail(f"would attempt via {desc.resolution.source}")
