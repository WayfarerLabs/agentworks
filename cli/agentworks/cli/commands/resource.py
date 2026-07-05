"""``agentworks resource`` -- cross-kind inspection of the Resource Registry.

Stops at framework-uniform fields (kind, name, origin, usage,
description). Kind-specific detail (secret backend mappings, template
inheritance chains, etc.) lives in the per-kind commands
(``agw secret describe``, ...). See FRD R12 / Phase 2c.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

import click
import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db
from agentworks.manifests.samples import SAMPLE_KINDS

if TYPE_CHECKING:
    from agentworks.resources.inspect import OriginFilter

resource_app = typer.Typer(
    name="resource",
    help="Cross-kind inspection of the Resource Registry.",
    no_args_is_help=True,
)
app.add_typer(resource_app)

_LAYOUT_CHOICES = click.Choice(["per-kind", "single", "per-resource"])
_TOML_CHOICES = click.Choice(["comment", "delete"])
_SAMPLE_KIND_CHOICES = click.Choice(list(SAMPLE_KINDS))


@resource_app.command("list")
def resource_list(
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=(
            "Filter to one or more kinds (CSV: --kind secret,vm-template). "
            "Default: all kinds in the registry."
        ),
    ),
    origin_filter: str | None = typer.Option(
        None,
        "--origin",
        help=(
            "Filter by origin variant: operator, auto, or builtin. "
            "Default: all origins."
        ),
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=(
            "Emit one kind/name per line (no header, no formatting). "
            "Used by shell completion."
        ),
    ),
) -> None:
    """List every Resource in the Registry across all kinds.

    Columns: KIND, NAME, ORIGIN (with detail), REFS (static config
    references count), USED BY (live DB instances depending on this
    resource per current config; ``-`` for kinds with no instance
    concept), DESCRIPTION. Description is reliably populated --
    operator-declared resources carry the operator's text,
    auto-declared resources get a framework-synthesized text (Phase
    2a's polish), and kinds whose Resource type has no description
    field show empty.
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
    db = get_db()
    # ``list_resources`` validates ``origin_filter`` (typed
    # ``ValidationError`` from the service layer; see inspect.py); the
    # ``cast`` is purely a typing-layer bridge from typer's ``str | None``
    # to the ``OriginFilter`` Literal. ``db`` lets the service populate
    # each row's ``used_by_count`` via the kind's ``instances`` hook.
    listing = list_resources(
        registry,
        db,
        kinds=kinds,
        origin_filter=cast("OriginFilter | None", origin_filter),
    )
    # ``--names-only`` short-circuits the table render. Per the
    # cli-conventions ``--names-only`` rule, render-only work is skipped:
    # ``list_resources`` does no I/O (pure dict + attribute access over
    # already-published Resources), so the cost up to here is negligible.
    # The cross-kind divergence from the rule: we emit ``kind/name``
    # rather than bare ``name`` because two kinds can publish resources
    # with the same name; completion snippets ``awk -F/`` the prefix.
    # ``/`` is the parse-safe separator: it cannot appear in names
    # (enforced at Registry.add), while ``:`` can. Empty result emits
    # nothing (no friendly "No resources" message), matching the rule so
    # completion candidate sets stay clean.
    if names_only:
        for row in listing.rows:
            output.info(f"{row.kind}/{row.name}")
        return
    render_resource_table(listing)


@resource_app.command("describe")
def resource_describe(
    ref: Annotated[
        str,
        typer.Argument(
            help="Resource as KIND/NAME (e.g. secret/npm-token, vm-template/dev).",
        ),
    ],
) -> None:
    """Show the full per-resource detail view.

    Three sections: a header (kind, name, description, origin), a
    ``Referenced by:`` list (one row per inbound config reference), and
    a ``Used by (per current config):`` list (one row per live DB
    instance whose subgraph reaches this resource, grouped by
    ``instance_kind``). Stops at framework-uniform fields; reach for
    ``agw secret describe`` etc. for kind-specific detail (backend
    mappings, inheritance chains, resolution preview).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.errors import ValidationError
    from agentworks.resources.inspect import (
        describe_resource,
        render_resource_description,
    )

    # One KIND/NAME grammar across the resource group (same token shape
    # as `resource migrate` selectors); '/' cannot appear in names, so
    # the first-slash split is unambiguous.
    kind, slash, name = ref.partition("/")
    if not slash or not name:
        raise ValidationError(
            f"expected KIND/NAME, got {ref!r}",
            hint="Example: agw resource describe secret/npm-token",
        )

    config = load_config()
    registry = build_registry(config)
    db = get_db()
    desc = describe_resource(registry, kind, name, db=db)
    render_resource_description(desc)


@resource_app.command("migrate")
def resource_migrate(
    selectors: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "What to migrate: KIND (one kind) or KIND/NAME (one "
                "resource). Repeatable; overlaps union. Required unless "
                "--all is passed."
            ),
        ),
    ] = None,
    all_resources: Annotated[
        bool,
        typer.Option(
            "--all",
            help=(
                "Migrate every TOML-declared resource. Required for a "
                "whole-config run; a bare invocation is an error, never "
                "an accidental full migration."
            ),
        ),
    ] = False,
    layout: Annotated[
        str,
        typer.Option(
            "--layout",
            click_type=_LAYOUT_CHOICES,
            help=(
                "How resources map to files: per-kind (default; "
                "vm-templates.yaml), single (resources.yaml), or per-resource "
                "(vm-template/small.yaml)."
            ),
        ),
    ] = "per-kind",
    toml: Annotated[
        str,
        typer.Option(
            "--toml",
            click_type=_TOML_CHOICES,
            help=(
                "What happens to the migrated TOML sections: comment "
                "(default; commented out in place with a marker) or delete."
            ),
        ),
    ] = "comment",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Print what would migrate where; write nothing. Summary by "
                "default; add --full for the YAML documents and the "
                "config.toml diff."
            ),
        ),
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                "With --dry-run: include the full YAML documents and the "
                "config.toml diff in the output."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Move resources from config.toml to YAML manifests.

    A recurring, incremental mover: run it any time you want to move
    resources (or a subset) from TOML to YAML. Output is append-only
    (existing YAML files are never rewritten), the original config.toml
    is backed up first, and every real run verifies the resulting
    registry is identical before it counts as done.
    """
    from agentworks import output
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.errors import UserAbort, ValidationError
    from agentworks.migrate import execute_plan, plan_migration
    from agentworks.migrate.render import render_dry_run, render_preview

    if full and not dry_run:
        raise ValidationError(
            "--full only applies to --dry-run",
            hint="A real run prints the summary and asks for confirmation.",
        )

    config = load_config()
    registry = build_registry(config)
    plan = plan_migration(
        config,
        registry,
        list(selectors or []),
        all_resources=all_resources,
        layout=layout,
        toml_mode=toml,
    )

    if plan.nothing_to_do:
        output.info("Nothing to migrate: no TOML-declared resources remain.")
        return

    if dry_run:
        for line in render_dry_run(plan, full=full):
            output.info(line)
        output.info("")
        output.info("Dry run: nothing was written.")
        return

    for line in render_preview(plan):
        output.info(line)
    if not yes and not output.confirm("Proceed?", default=False):
        raise UserAbort("migration cancelled")

    result = execute_plan(plan, config)
    for path in result.created:
        output.info(f"Created {path}")
    for path in result.appended:
        output.info(f"Appended to {path}")
    output.info(f"Rewrote {plan.config_path} (backup: {result.backup_path})")
    if result.dropped_secret_backends:
        output.info("Dropped deprecated [secret_backends.*] sections.")
    output.info(f"verified: registry unchanged ({result.verified_rows} resources)")


@resource_app.command("sample")
def resource_sample(
    kind: Annotated[
        str | None,
        typer.Argument(
            click_type=_SAMPLE_KIND_CHOICES,
            help=(
                "Kind to print a sample manifest for (e.g. secret, "
                "vm-template). Default: samples for every kind."
            ),
        ),
    ] = None,
    write: Annotated[
        str | None,
        typer.Option(
            "--write",
            help=(
                "Save to this filename under the resources directory instead "
                "of stdout (relative .yaml/.yml path; appends if the file "
                "exists)."
            ),
        ),
    ] = None,
) -> None:
    """Print (or save) commented sample resource manifests.

    Samples are fully commented out: saved files are inert until you
    uncomment and edit them, so --write can never create a live
    resource or a duplicate. The TOML settings-file counterpart is
    `agw config sample`.
    """
    from agentworks import output
    from agentworks.manifests.loader import RESOURCES_DIRNAME
    from agentworks.manifests.samples import sample_text, write_sample

    if write is None:
        output.info(sample_text(kind).rstrip("\n"))
        return

    from agentworks.config import load_config

    config = load_config()
    resources_dir = config.source_path.parent / RESOURCES_DIRNAME
    path, appended = write_sample(resources_dir, write, kind)
    verb = "Appended sample to" if appended else "Wrote sample to"
    output.info(f"{verb} {path}")
    output.info("Uncomment the document lines (delete one leading '#') to activate.")
