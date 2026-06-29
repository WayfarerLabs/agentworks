"""``agentworks resource`` -- cross-kind inspection of the Resource Registry.

Stops at framework-uniform fields (kind, name, origin, usage,
description). Kind-specific detail (secret backend mappings, template
inheritance chains, etc.) lives in the per-kind commands
(``agw secret describe``, ...). See FRD R12 / Phase 2c.
"""

from __future__ import annotations

import typer

from agentworks.cli._app import app

resource_app = typer.Typer(
    name="resource",
    help="Cross-kind inspection of the Resource Registry.",
    no_args_is_help=True,
)
app.add_typer(resource_app)


_VALID_ORIGIN_FILTERS = ("operator", "auto", "code")


@resource_app.command("list")
def resource_list(
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=(
            "Filter to one or more kinds (CSV: ``--kind secret,vm_template``). "
            "Default: all kinds in the registry."
        ),
    ),
    origin_filter: str | None = typer.Option(
        None,
        "--origin",
        help=(
            "Filter by origin variant: ``operator``, ``auto``, or ``code``. "
            "Default: all origins."
        ),
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=(
            "Emit one ``kind:name`` per line (no header, no formatting). "
            "Used by shell completion."
        ),
    ),
) -> None:
    """List every Resource in the Registry across all kinds.

    Columns: kind, name, origin (with detail), usage count, description.
    Description is reliably populated -- operator-declared resources
    carry the operator's text, auto-declared resources get a framework-
    synthesized text (Phase 2a's polish), and kinds whose Resource type
    has no description field show empty.
    """
    from agentworks import output
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.errors import ValidationError
    from agentworks.resources.inspect import list_resources, render_resource_table

    if origin_filter is not None and origin_filter not in _VALID_ORIGIN_FILTERS:
        raise ValidationError(
            f"--origin must be one of {list(_VALID_ORIGIN_FILTERS)}; got {origin_filter!r}",
            entity_kind="resource",
        )

    kinds: tuple[str, ...] | None = None
    if kind is not None:
        kinds = tuple(k.strip() for k in kind.split(",") if k.strip())

    config = load_config()
    registry = build_registry(config)
    listing = list_resources(
        registry,
        kinds=kinds,
        origin_filter=origin_filter,  # type: ignore[arg-type]
    )
    if names_only:
        for row in listing.rows:
            output.info(f"{row.kind}:{row.name}")
        return
    render_resource_table(listing)


@resource_app.command("describe")
def resource_describe(
    kind: str = typer.Argument(..., help="Resource kind (e.g. ``secret``, ``vm_template``)."),
    name: str = typer.Argument(..., help="Resource name within the kind."),
) -> None:
    """Show the full per-resource detail view.

    Three sections: header (kind, name, description, origin), usages
    (one row per requirement). Stops at framework-uniform fields; reach
    for ``agw secret describe`` etc. for kind-specific detail (backend
    mappings, inheritance chains, resolution preview).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.inspect import (
        describe_resource,
        render_resource_description,
    )

    config = load_config()
    registry = build_registry(config)
    desc = describe_resource(registry, kind, name)
    render_resource_description(desc)
