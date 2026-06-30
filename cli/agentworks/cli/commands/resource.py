"""``agentworks resource`` -- cross-kind inspection of the Resource Registry.

Stops at framework-uniform fields (kind, name, origin, usage,
description). Kind-specific detail (secret backend mappings, template
inheritance chains, etc.) lives in the per-kind commands
(``agw secret describe``, ...). See FRD R12 / Phase 2c.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import typer

from agentworks.cli._app import app

if TYPE_CHECKING:
    from agentworks.resources.inspect import OriginFilter

resource_app = typer.Typer(
    name="resource",
    help="Cross-kind inspection of the Resource Registry.",
    no_args_is_help=True,
)
app.add_typer(resource_app)


@resource_app.command("list")
def resource_list(
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=(
            "Filter to one or more kinds (CSV: --kind secret,vm_template). "
            "Default: all kinds in the registry."
        ),
    ),
    origin_filter: str | None = typer.Option(
        None,
        "--origin",
        help=(
            "Filter by origin variant: operator, auto, or code. "
            "Default: all origins."
        ),
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=(
            "Emit one kind:name per line (no header, no formatting). "
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
    from agentworks.resources.inspect import (
        list_resources,
        render_resource_table,
    )

    # Parse --kind here (CLI's job: turn argv shape into the service's
    # ``tuple[str, ...]``). ``list_resources`` then validates: empty tuple
    # raises ``ValidationError``, bad ``origin_filter`` raises too.
    kinds: tuple[str, ...] | None = None
    if kind is not None:
        kinds = tuple(k.strip() for k in kind.split(",") if k.strip())

    config = load_config()
    registry = build_registry(config)
    # ``list_resources`` validates ``origin_filter`` (typed
    # ``ValidationError`` from the service layer; see inspect.py); the
    # ``cast`` is purely a typing-layer bridge from typer's ``str | None``
    # to the ``OriginFilter`` Literal.
    listing = list_resources(
        registry,
        kinds=kinds,
        origin_filter=cast("OriginFilter | None", origin_filter),
    )
    # ``--names-only`` short-circuits the table render. Per the
    # cli-conventions ``--names-only`` rule, render-only work is skipped:
    # ``list_resources`` does no I/O (pure dict + attribute access over
    # already-published Resources), so the cost up to here is negligible.
    # The cross-kind divergence from the rule: we emit ``kind:name``
    # rather than bare ``name`` because two kinds can publish resources
    # with the same name; completion snippets ``awk -F:`` the prefix.
    # Empty result emits nothing (no friendly "No resources" message),
    # matching the rule so completion candidate sets stay clean.
    if names_only:
        for row in listing.rows:
            output.info(f"{row.kind}:{row.name}")
        return
    render_resource_table(listing)


@resource_app.command("describe")
def resource_describe(
    kind: str = typer.Argument(..., help="Resource kind (e.g. secret, vm_template)."),
    name: str = typer.Argument(..., help="Resource name within the kind."),
) -> None:
    """Show the full per-resource detail view.

    Two sections: header (kind, name, description, origin), usages
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
