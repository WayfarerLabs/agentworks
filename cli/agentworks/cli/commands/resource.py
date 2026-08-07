"""``agentworks resource`` -- cross-kind inspection of the Resource Registry.

Stops at framework-uniform fields (kind, name, origin, usage,
description). Kind-specific detail (secret backend mappings, template
inheritance chains, etc.) lives in the per-kind commands
(``agw secret describe``, ...).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

import click
import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db

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
    help="Cross-kind inspection of the Resource Registry.",
    no_args_is_help=True,
)
app.add_typer(resource_app)

_LAYOUT_CHOICES = click.Choice(["per-kind", "single", "per-resource"])
_TOML_CHOICES = click.Choice(["comment", "delete"])
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
) -> None:
    """List every Resource in the Registry across all kinds.

    Columns: KIND, NAME, ORIGIN (with detail), REFS (static config
    references count), USED BY (live DB instances depending on this
    resource per current config; ``-`` for kinds with no instance
    concept), DESCRIPTION. Description is reliably populated:
    operator-declared resources carry the operator's text, and
    auto-declared defaults get a framework-synthesized text (the
    registry's auto-declared polish). Every declarable kind carries a
    description field (see ``DeclaredResource``); only capability kinds
    whose registration record has none show empty.
    """
    from agentworks import output
    from agentworks.bootstrap import load_request_registry
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
    registry = load_request_registry(config)
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
    render_resource_table(listing)


@resource_app.command("kinds")
def resource_kinds(
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help=("Emit one kind name per line (no header, no formatting). Used by shell completion."),
    ),
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
    from agentworks.resources.inspect import list_kinds, render_kind_table

    config = load_config()
    registry = load_request_registry(config)
    render_kind_table(list_kinds(registry))


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
    from agentworks.bootstrap import load_request_registry
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
    registry = load_request_registry(config)
    db = get_db()
    desc = describe_resource(registry, kind, name, db=db)
    render_resource_description(desc)


@resource_app.command("describe-kind")
def resource_describe_kind(
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
    document to edit; `agw resource describe KIND/NAME` describes a
    declared resource rather than a kind.
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
    from agentworks.errors import ValidationError
    from agentworks.resources.inspect import edit_location

    kind, slash, name = ref.partition("/")
    if not slash or not name:
        raise ValidationError(
            f"expected KIND/NAME, got {ref!r}",
            hint="Example: agw resource edit secret/npm-token",
        )

    # Same $EDITOR contract as `agw config edit`; checked before the
    # (comparatively slow) registry build so the common misconfiguration
    # fails fast.
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        typer.echo("Error: $EDITOR is not set. Set it to your preferred editor.", err=True)
        raise typer.Exit(1)

    from agentworks.errors import ConfigError

    try:
        config = load_config()
        registry = load_request_registry(config)
        path, line = edit_location(registry, kind, name)
    except ConfigError as exc:
        # The fix-it path: a config failing validation is exactly when
        # the operator needs edit most, so fall back to a tolerant,
        # validation-free scan of the manifests directory. Only
        # ConfigError (config-broken) triggers this; ValidationError /
        # NotFoundError (wrong invocation, wrong name) propagate with
        # their better messages.
        from agentworks.config import load_config as load_settings
        from agentworks.manifests import RESOURCES_DIRNAME
        from agentworks.manifests.loader import locate_document

        settings = load_settings(resources=False)
        resources_dir = settings.source_path.parent / RESOURCES_DIRNAME
        found = locate_document(resources_dir, kind, name)
        if found.location is None:
            if found.unreadable:
                files = ", ".join(str(p) for p in found.unreadable)
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
    # simple, per the maintainer's scope ruling.)
    output.info(f"Editing {kind}/{name} ({path}:{line})")
    raise typer.Exit(subprocess.call([editor, str(path)]))


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
            help=("With --dry-run: include the full YAML documents and the config.toml diff in the output."),
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Migrate TOML-declared resources to YAML manifests, upgrading retired shapes.

    The summary is deliberately ONE line: completion specs take
    `.split("\\n")[0]` of it, so a summary wrapped in the source shows up
    truncated mid-sentence in every shell.

    A recurring, incremental mover: run it any time you want to move
    resources (or a subset) from TOML to YAML. New
    TOML-derived documents append without rewriting existing files.

    Every run also upgrades existing manifests still naming a capability
    in the retired sibling shape (`platform: lima` plus `platform_config:`)
    to the tagged table, preserving comments. That part is not scoped by
    the selectors: the old shape does not load, so leaving one document
    behind would leave the whole resources directory unloadable.

    The original config.toml is backed up first, every manifest this run
    replaces is snapshotted beside it, and every real run verifies the
    resulting registry is identical before it counts as done.
    """
    from agentworks import output
    from agentworks.config import load_config
    from agentworks.errors import UserAbort, ValidationError
    from agentworks.migrate import execute_plan, plan_migration
    from agentworks.migrate.render import render_dry_run, render_preview

    if full and not dry_run:
        raise ValidationError(
            "--full only applies to --dry-run",
            hint="A real run prints the summary and asks for confirmation.",
        )

    # This command IS the remediation for the resource-section hard error, so
    # it loads settings-only (resources=False) to read a config that still
    # carries resource sections. Planning is pure over the config text (no
    # registry build); the post-run verification builds its own registry from
    # the rewritten config.
    config = load_config(resources=False)
    plan = plan_migration(
        config,
        list(selectors or []),
        all_resources=all_resources,
        layout=layout,
        toml_mode=toml,
    )

    if plan.nothing_to_do:
        output.info(
            "Nothing to migrate: no TOML-declared resources remain, and every manifest is on the current shape."
        )
        return

    if dry_run:
        for line in render_dry_run(plan, full=full):
            output.info(line)
        output.info("")
        output.info("Dry run: nothing was written.")
        # Say which of the real run's two checks this reached. Planning
        # ran the load precondition over the tree this would produce, so
        # a dry run no longer reports success where the real run refuses;
        # what is left is the registry-equivalence check, which needs the
        # files on disk and answers a migrator-bug question rather than a
        # config one. Claiming or implying both would be the old bug.
        output.detail(
            "Checked: the config loads and the registry builds with this migration applied. A "
            "real run repeats that and then verifies the rebuilt registry MATCHES the one it "
            "replaced, which needs the files on disk."
        )
        return

    for line in render_preview(plan):
        output.info(line)
    if not yes and not output.confirm("Proceed?", default=False):
        raise UserAbort("migration cancelled")

    output.info("Applying migration...")
    result = execute_plan(plan, config)
    for path in result.created:
        output.detail(f"Created {path}")
    for path in result.appended:
        output.detail(f"Appended to {path}")
    for path in result.replaced:
        output.detail(f"Upgraded {path}")
    if result.config_rewritten:
        output.detail(f"Rewrote {plan.config_path} (backup: {result.backup_path})")
    else:
        output.detail(f"Backup: {result.backup_path}")
    if result.yaml_backup_path is not None:
        output.detail(f"YAML recovery copies: {result.yaml_backup_path}")
    if result.schema_dir is not None:
        output.detail(f"Editor schemas: {result.schema_dir} (created files reference them)")
    if result.dropped_secret_backends:
        output.detail("Dropped deprecated [secret_backends.*] sections.")
    # Phrased as what the operator just watched happen, not as the
    # internal comparison. "registry unchanged" was true and read as
    # "it did nothing" straight after a list of files it had rewritten,
    # which is the opposite of the reassurance this line exists to give.
    output.result(
        f"verified: the {result.verified_rows} migrated resource(s) load from the new files exactly as before"
    )


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
            help=(
                "Print every kind's sample. Required for the full set; a "
                "bare invocation is an error, matching `resource migrate`."
            ),
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

    # Settings-only load: --write needs nothing but source_path to locate
    # the resources directory, so the resource sections (and their
    # deprecation nudge -- this command is the remediation path) stay out.
    config = load_config(resources=False)
    resources_dir = config.source_path.parent / RESOURCES_DIRNAME
    path, appended = write_sample(resources_dir, write, kind, all_kinds=all_kinds)
    verb = "Appended sample to" if appended else "Wrote sample to"
    output.info(f"{verb} {path}")
    output.info("Uncomment the document lines (delete one leading '#') to activate.")
    if not appended:
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
    write: Annotated[
        bool,
        typer.Option(
            "--write",
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
    `agw resource sample --write` and `agw resource migrate` already
    carry the association, as a `# yaml-language-server: $schema=...`
    line; add that line to a hand-written manifest to get the same.

    The schema describes THIS host: a capability contributed by a plugin
    appears in it once that plugin is installed, so re-run --write after
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

    if not write:
        schema = envelope_schema() if kind is None else document_schema(kind)
        output.info(schema_json(schema).rstrip("\n"))
        return

    if kind is not None:
        raise ValidationError(
            "--write writes the whole schema set, so it takes no kind",
            hint=(
                "A partial set would leave some manifest's modeline pointing "
                "at a file that is not there. Drop the kind, or print one "
                "kind's schema without --write."
            ),
        )

    from agentworks.config import load_config
    from agentworks.manifests.loader import RESOURCES_DIRNAME

    # Settings-only, like `sample --write`: locating the resources
    # directory needs `source_path` and nothing else.
    config = load_config(resources=False)
    schema_dir = config.source_path.parent / RESOURCES_DIRNAME / SCHEMA_DIRNAME
    written = write_schema_set(schema_dir)
    output.info(f"Wrote {len(written)} schemas to {schema_dir}")
