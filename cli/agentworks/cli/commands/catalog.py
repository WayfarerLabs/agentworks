"""`agentworks catalog` -- list and inspect catalog entries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import click
import typer

from agentworks.cli._app import app

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.cli._helpers import HasDescription

catalog_app = typer.Typer(
    name="catalog",
    help="List and inspect catalog entries (apt sources, apt packages, install commands).",
    no_args_is_help=True,
)
app.add_typer(catalog_app)

_TYPE_CHOICES = click.Choice(["apt-source", "apt-package", "system-install-cmd", "user-install-cmd"])

# Maps type labels to config attributes for source detection
_CONFIG_ATTR = {
    "apt-source": "apt_sources",
    "apt-package": "apt_packages",
    "system-install-cmd": "system_install_commands",
    "user-install-cmd": "user_install_commands",
}


@catalog_app.command("list")
def catalog_list(
    type_filter: Annotated[str | None, typer.Option("--type", help="Filter by type", click_type=_TYPE_CHOICES)] = None,
    source_filter: Annotated[
        str | None,
        typer.Option(
            "--source", help="Filter by source", click_type=click.Choice(["built-in", "custom"])
        ),
    ] = None,
) -> None:
    """List catalog entries from the built-in and custom catalog."""
    from agentworks.catalog import load_catalog
    from agentworks.config import load_config

    config = load_config()
    merged = load_catalog(config)

    rows: list[tuple[str, str, str, str]] = []  # (type, name, source, description)

    def _add_entries(
        type_label: str,
        merged_entries: Mapping[str, HasDescription],
    ) -> None:
        for name, entry in sorted(merged_entries.items()):
            # An entry is "custom" if it's declared in the user's config under
            # the matching section; otherwise it came from the built-in
            # catalog. The user's section overrides the built-in entry for
            # the same name (resolved during catalog merge).
            is_custom = name in getattr(config, _CONFIG_ATTR[type_label], {})
            source = "custom" if is_custom else "built-in"
            if source_filter is not None and source != source_filter:
                continue
            rows.append((type_label, name, source, entry.description))

    if type_filter is None or type_filter == "apt-source":
        _add_entries("apt-source", merged.apt_sources)
    if type_filter is None or type_filter == "apt-package":
        _add_entries("apt-package", merged.apt_packages)
    if type_filter is None or type_filter == "system-install-cmd":
        _add_entries("system-install-cmd", merged.system_install_commands)
    if type_filter is None or type_filter == "user-install-cmd":
        _add_entries("user-install-cmd", merged.user_install_commands)

    if not rows:
        typer.echo("No entries found.")
        return

    # Calculate column widths
    type_w = max(len(r[0]) for r in rows)
    name_w = max(len(r[1]) for r in rows)
    src_w = max(len(r[2]) for r in rows)

    header = f"{'TYPE':<{type_w}}  {'NAME':<{name_w}}  {'SOURCE':<{src_w}}  DESCRIPTION"
    typer.echo(header)
    typer.echo("-" * len(header))
    for type_label, name, source, desc in rows:
        typer.echo(f"{type_label:<{type_w}}  {name:<{name_w}}  {source:<{src_w}}  {desc}")


@catalog_app.command("describe")
def catalog_describe(
    name: Annotated[str, typer.Argument(help="Entry name")],
) -> None:
    """Show details of a catalog entry."""
    from agentworks.catalog import (
        AptPackageEntry,
        AptSourceEntry,
        SystemInstallCommandEntry,
        UserInstallCommandEntry,
        load_builtin_catalog,
        load_catalog,
    )
    from agentworks.config import load_config

    config = load_config()
    builtin = load_builtin_catalog()
    merged = load_catalog(config)

    # Search all four pools; Mapping[str, HasDescription] covers all catalog entry types
    # (all have description: str) and allows covariant use of the concrete dict types.
    pools: list[tuple[str, Mapping[str, HasDescription], Mapping[str, HasDescription], str]] = [
        ("apt-source", merged.apt_sources, builtin.apt_sources, "apt_sources"),
        ("apt-package", merged.apt_packages, builtin.apt_packages, "apt_packages"),
        (
            "system-install-cmd",
            merged.system_install_commands,
            builtin.system_install_commands,
            "system_install_commands",
        ),
        (
            "user-install-cmd",
            merged.user_install_commands,
            builtin.user_install_commands,
            "user_install_commands",
        ),
    ]
    for type_label, merged_entries, builtin_entries, config_attr in pools:
        if name not in merged_entries:
            continue

        entry = merged_entries[name]
        is_custom = name in getattr(config, config_attr, {})
        source = "custom" if is_custom else "built-in"
        overrides = name in builtin_entries and is_custom

        typer.echo(f"Name:        {name}")
        typer.echo(f"Type:        {type_label}")
        typer.echo(f"Source:      {source}")
        if overrides:
            typer.echo("             (overrides built-in)")
        typer.echo(f"Description: {entry.description}")

        if isinstance(entry, AptSourceEntry):
            typer.echo(f"Key URL:     {entry.key_url}")
            typer.echo(f"Key path:    {entry.key_path}")
            typer.echo(f"Source:      {entry.source}")
            typer.echo(f"Source file: {entry.source_file}")
            if entry.key_dearmor:
                typer.echo("Key dearmor: yes")
        elif isinstance(entry, AptPackageEntry):
            if entry.apt_sources:
                typer.echo(f"Apt sources: {', '.join(entry.apt_sources)}")
            typer.echo(f"Apt:         {', '.join(entry.apt)}")
        elif isinstance(entry, (SystemInstallCommandEntry, UserInstallCommandEntry)):
            typer.echo(f"Command:     {entry.command}")
            if entry.test_exec:
                typer.echo(f"Test exec:   {entry.test_exec}")
            if entry.test_file:
                typer.echo(f"Test file:   {entry.test_file}")
            if entry.test_dir:
                typer.echo(f"Test dir:    {entry.test_dir}")
            if entry.path:
                typer.echo(f"PATH:        {', '.join(entry.path)}")
        return

    typer.echo(f"Error: '{name}' not found in catalog", err=True)
    raise typer.Exit(1)
