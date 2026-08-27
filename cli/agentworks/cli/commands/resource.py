"""``agentworks resource`` inventory, inspection, explanation, authoring, and editing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, ordinary_tty_interaction_access
from agentworks.machine_output import OutputFormat

# Module-level because three commands in this file render a host path and
# `path_rendering` is a leaf module (pathlib only), so hoisting it costs
# no startup time.
from agentworks.path_rendering import format_host_path

# Module-level so the sample-kind Choice below can be built at decoration
# time. This is intentional and adds no startup cost over the pre-fix
# code: that imported SAMPLE_KINDS from `agentworks.manifests.samples`,
# which already pulls the full `agentworks.resources` capability chain
# transitively, so `agw`, `agw --help`, and completion loaded the same
# modules before this fix.
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from agentworks.resources.inspect import OriginFilter

resource_app = typer.Typer(
    name="resource",
    help="Inventory, inspect, explain, author, and edit Resource Registry entries.",
    no_args_is_help=True,
)
app.add_typer(resource_app)

# The sample-kind argument is deliberately a plain string, not a
# click.Choice: ANY kind the operator types (a capability kind, a typo,
# anything) must reach the service layer, which rejects with a clean,
# kind-aware domain error either way (see
# manifests.samples._validated_kinds). A Choice would intercept
# out-of-set strings at parse time, the raw-traceback escape #276 is
# about. Completions still steer via the completion spec.


@resource_app.command("list")
def resource_list(
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=("Filter to one or more kinds (CSV: --kind secret,vm-template). Default: all kinds in the registry."),
    ),
    origin_filter: str | None = typer.Option(
        None,
        "--origin",
        help=("Filter by origin variant: operator, auto, builtin, or plugin. Default: all origins."),
    ),
    include_disabled: bool = typer.Option(
        False,
        "--include-disabled",
        help=("Also show disabled resources, for example a not-enabled plugin's rows. Default: hidden."),
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=("Emit one kind/name per line (no header, no formatting). Used by shell completion."),
    ),
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """List every Resource in the Registry across all kinds.

    Columns: KIND, NAME, ORIGIN (with detail), REFS (static config
    references count), USED BY (the stable current live-usage projection,
    or ``-`` for kinds without one), DESCRIPTION. Description is reliably populated:
    operator-declared resources carry the operator's text, and
    auto-declared defaults get a framework-synthesized text (the
    registry's auto-declared polish). Every declarable kind carries a
    description field (see ``DeclaredResource``); only capability kinds
    whose registration record has none show empty.
    """
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")

    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.inspect import (
        list_resources,
        render_resource_table,
        resource_listing_data,
    )

    # Parse --kind here (CLI's job: turn argv shape into the service's
    # ``tuple[str, ...]``). ``list_resources`` then validates: empty tuple
    # raises ``ValidationError``, bad ``origin_filter`` raises too.
    kinds: tuple[str, ...] | None = None
    if kind is not None:
        kinds = tuple(k.strip() for k in kind.split(",") if k.strip())

    # Never reads the operator's SSH key files; see load_config's
    # workload_gated_issues_fatal doc.
    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    if names_only:
        from agentworks import db as db_module
        from agentworks.db import open_completion_database

        completion_db = open_completion_database(db_module.DB_PATH)
        try:
            registry = load_request_registry(
                config,
                warn=output_format is OutputFormat.HUMAN,
                include_live_resources=completion_db is not None,
                live_database=completion_db,
            )
        finally:
            if completion_db is not None:
                completion_db.close()
    else:
        db = get_db()
        registry = load_request_registry(
            config,
            warn=output_format is OutputFormat.HUMAN,
            live_database=db,
        )
    # ``list_resources`` validates ``origin_filter`` (typed
    # ``ValidationError`` from the service layer; see inspect.py); the
    # ``cast`` is purely a typing-layer bridge from typer's ``str | None``
    # to the ``OriginFilter`` Literal.
    listing = list_resources(
        registry,
        kinds=kinds,
        origin_filter=cast("OriginFilter | None", origin_filter),
        include_disabled=include_disabled,
    )
    # ``--names-only`` short-circuits the table render. Per the
    # cli-conventions ``--names-only`` rule, render-only work is skipped:
    # ``list_resources`` does no network or DB-heavy work (attribute
    # access over already-published Resources, plus a read of each row's
    # stored readiness verdict off the graph: a cheap dict lookup, no
    # recompute), so the cost up to here is completion-cheap.
    # Keep it that way: heavier per-row work belongs after this check.
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
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(
            MachineOutputCommand.RESOURCE_LIST,
            resource_listing_data(listing),
            get_binary_stream("stdout"),
        )
        return
    render_resource_table(listing)


@resource_app.command("show")
def resource_show(
    ref: Annotated[
        str,
        typer.Argument(
            help="Resource as KIND/NAME (e.g. secret/npm-token, vm-template/dev).",
        ),
    ],
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show complete focused facts for one loaded resource."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.access import parse_resource_identity
    from agentworks.resources.show import (
        render_resource_show,
        resource_show_data,
        show_resource,
    )

    identity = parse_resource_identity(ref)
    warn = output_format is OutputFormat.HUMAN
    # Never reads the operator's SSH key files; see load_config's
    # workload_gated_issues_fatal doc.
    config = load_config(warn_issues=warn, workload_gated_issues_fatal=False)
    db = get_db()
    registry = load_request_registry(config, warn=warn, live_database=db)
    shown = show_resource(
        config,
        registry,
        identity,
        tty_access=ordinary_tty_interaction_access(),
    )

    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(
            MachineOutputCommand.RESOURCE_SHOW,
            resource_show_data(shown),
            get_binary_stream("stdout"),
        )
        return
    render_resource_show(shown)


@resource_app.command("kinds")
def resource_kinds(
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=("Emit one kind name per line (no header, no formatting). Used by shell completion."),
    ),
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """List every resource kind the app defines.

    Read-only and code-defined: kinds are baked into the app; plugins
    publish resources of existing kinds (declarable and capability
    alike), never new kinds. CATEGORY is per-kind by construction --
    `declarable` kinds hold data (operator TOML/YAML, auto-declared,
    built-in); `capability` kinds hold read-only rows backed by
    registered code. RESOURCES counts the current registry rows per
    kind.
    """
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")

    from agentworks import output

    # The names-only path needs no config and no registry: kinds are
    # static code. Keeps completion fast and working even with a broken
    # or absent config. KIND_REGISTRY is imported at module level.
    if names_only:
        for name in sorted(KIND_REGISTRY):
            output.info(name)
        return

    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.inspect import list_kinds, render_kind_table, resource_kinds_data

    # Never reads the operator's SSH key files; see load_config's
    # workload_gated_issues_fatal doc.
    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    registry = load_request_registry(
        config,
        warn=output_format is OutputFormat.HUMAN,
        include_live_resources=False,
    )
    rows = list_kinds(registry)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(MachineOutputCommand.RESOURCE_KINDS, resource_kinds_data(rows), get_binary_stream("stdout"))
        return
    render_kind_table(rows)


@resource_app.command("explain")
def resource_explain(
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "What to document: a kind (secret, vm-site, vm-platform) or "
                "one implementation of a capability kind "
                "(vm-platform/azure-vm)."
            ),
        ),
    ],
) -> None:
    """Show what a kind (or one capability implementation) accepts.

    The field reference: every field an operator may write, with its type,
    whether it is required, its default, and what it means, rendered from
    the same declaration the loader validates against and the editor
    schema is emitted from. A capability kind lists its implementations;
    naming one documents its config.

    Reads no config and builds no registry, so it answers on a host whose
    config is broken, and it documents a capability whose plugin is not
    enabled. `agw resource sample KIND` prints the same fields as a
    document to edit.
    """
    from agentworks.manifests.describe import render_reference
    from agentworks.manifests.reference import reference_for

    render_reference(reference_for(target))


@resource_app.command("edit")
def resource_edit(
    ref: Annotated[
        str,
        typer.Argument(
            help="Resource as KIND/NAME (e.g. secret/npm-token, vm-template/dev).",
        ),
    ],
) -> None:
    """Open the YAML manifest declaring a resource in $EDITOR.

    Only operator-declared YAML resources are editable here: built-in /
    auto-declared resources have no file to open.
    """
    import os
    import subprocess

    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.access import parse_resource_identity
    from agentworks.resources.inspect import edit_location

    identity = parse_resource_identity(ref)
    kind, name = identity.kind, identity.name

    # Same $EDITOR contract as `agw config edit`; checked before the
    # (comparatively slow) registry build so the common misconfiguration
    # fails fast.
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        typer.echo("Error: $EDITOR is not set. Set it to your preferred editor.", err=True)
        raise typer.Exit(1)

    from agentworks.errors import ConfigError
    from agentworks.schema import location_text
    from agentworks.source_location import SourceLocation

    # Never reads the operator's SSH key files; see load_config's
    # workload_gated_issues_fatal doc.
    config = load_config(workload_gated_issues_fatal=False)
    try:
        registry = load_request_registry(config, include_live_resources=False)
        path, line = edit_location(registry, kind, name)
    except ConfigError as exc:
        # The fix-it path: a manifest set failing validation is exactly when
        # the operator needs edit most, so fall back to a tolerant,
        # validation-free scan of the manifests directory. Config loading has
        # already succeeded; a ConfigError from the registry path triggers
        # this fallback, while ValidationError / NotFoundError (wrong
        # invocation, wrong name) propagate with their better messages.
        from agentworks.manifests import RESOURCES_DIRNAME
        from agentworks.manifests.loader import locate_document

        resources_dir = config.source_path.parent / RESOURCES_DIRNAME
        found = locate_document(resources_dir, kind, name)
        if found.location is None:
            if found.unreadable:
                files = ", ".join(format_host_path(p) for p in found.unreadable)
                exc.hint = (
                    f"{exc.hint + ' ' if exc.hint else ''}Also: {files} "
                    f"failed to parse and could not be searched; edit "
                    f"the file directly if the resource lives there."
                )
            raise
        output.warn(f"config is currently failing validation ({exc}); opening the declaring manifest anyway")
        path, line = found.location.file, found.location.line
    # Per-kind layout files hold many documents; the line tells the
    # operator where to look. (No editor +line heuristics -- keep it
    # simple, per the maintainer's scope ruling.) Framed by the shared
    # helper so this reads the same as the errors that sent them here.
    output.info(f"Editing {kind}/{name} ({location_text(SourceLocation(file=path, line=line))})")
    raise typer.Exit(subprocess.call([editor, str(path)]))


@resource_app.command("sample")
def resource_sample(
    kind: Annotated[
        str | None,
        typer.Argument(
            help=("Kind to print a sample manifest for (e.g. secret, vm-template). Required unless --all is passed."),
        ),
    ] = None,
    all_kinds: Annotated[
        bool,
        typer.Option(
            "--all",
            help=("Print every kind's sample. Required for the full set; a bare invocation is an error."),
        ),
    ] = False,
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
        output.info(sample_text(kind, all_kinds=all_kinds).rstrip("\n"))
        return

    from agentworks.config import load_config

    config = load_config(workload_gated_issues_fatal=False)
    resources_dir = config.source_path.parent / RESOURCES_DIRNAME
    path, outcome = write_sample(resources_dir, write, kind, all_kinds=all_kinds)
    verb = "Appended sample to" if outcome == "appended" else "Wrote sample to"
    output.info(f"{verb} {format_host_path(path)}")
    output.info("Uncomment the document lines (delete one leading '#') to activate.")
    # Each arm names something that IS in the file. A blank file that was
    # already there ("filled") gets neither a separator nor a modeline, so
    # it gets neither line; claiming the separator there pointed the
    # operator at a '#---' that was not written.
    if outcome == "appended":
        output.detail(
            "The '#---' above the new sample is one of those document lines: it separates it from what was already "
            "in the file, so uncomment it too."
        )
    elif outcome == "created":
        output.detail(
            "The first line associates a schema, so a schema-aware editor checks the file as you type. Leave it as a "
            "comment."
        )


@resource_app.command("schema")
def resource_schema(
    kind: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Kind to print the schema for (e.g. secret, vm-template). "
                "Omit for the any-kind schema, which describes a manifest "
                "document of every kind at once."
            ),
        ),
    ] = None,
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help=(
                "Write the whole set (the any-kind schema plus one per kind) "
                "under the resources directory instead of printing. The "
                "destination is fixed, because the modeline written into "
                "manifests refers to it by that path."
            ),
        ),
    ] = False,
) -> None:
    """Print (or write) the JSON Schema for resource manifests.

    Point a schema-aware editor at these and manifests get completions,
    hover docs, and diagnostics as you type. Files written by
    `agw resource sample --write` already carry the association, as a
    `# yaml-language-server: $schema=...` line; add that line to a
    hand-written manifest to get the same.

    The schema describes THIS host: a capability contributed by a plugin
    appears in it once that plugin is installed, so re-run --install after
    installing one.
    """
    from agentworks import output
    from agentworks.errors import ValidationError
    from agentworks.manifests.emit import (
        SCHEMA_DIRNAME,
        document_schema,
        envelope_schema,
        schema_json,
        write_schema_set,
    )

    if not install:
        schema = envelope_schema() if kind is None else document_schema(kind)
        output.info(schema_json(schema).rstrip("\n"))
        return

    if kind is not None:
        raise ValidationError(
            "--install writes the whole schema set, so it takes no kind",
            hint=(
                "A partial set would leave some manifest's modeline pointing "
                "at a file that is not there. Drop the kind, or print one "
                "kind's schema without --install."
            ),
        )

    from agentworks.config import load_config
    from agentworks.manifests.loader import RESOURCES_DIRNAME

    config = load_config(workload_gated_issues_fatal=False)
    schema_dir = config.source_path.parent / RESOURCES_DIRNAME / SCHEMA_DIRNAME
    written = write_schema_set(schema_dir)
    output.info(f"Wrote {len(written)} schemas to {format_host_path(schema_dir)}")
