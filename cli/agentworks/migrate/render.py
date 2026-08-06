"""Preview rendering for ``agw resource migrate`` (plan and dry-run)."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.migrate.planning import MigrationPlan


def render_preview(plan: MigrationPlan) -> list[str]:
    """The confirmation-prompt summary: what would be written and edited."""
    lines: list[str] = []
    if plan.units:
        lines.append(f"Migrating {len(plan.units)} resource(s) from config.toml:")
        for unit in plan.units:
            target = plan.targets.get((unit.kind, unit.name), "?")
            lines.append(f"  {unit.kind}/{unit.name} to {target}")
        for write in plan.writes:
            action = "append to" if write.exists else "create"
            lines.append(f"  {action} {write.path} ({len(write.documents)} document(s))")
        for rewrite in plan.rewrites:
            # A target file that is also being upgraded has no FileWrite of
            # its own (the two coalesce into one replacement), so its
            # documents are accounted for here instead of vanishing.
            if rewrite.appended:
                lines.append(f"  append to {rewrite.path} ({rewrite.appended} document(s), within its upgrade below)")
        verb = "commented out in" if plan.toml_mode == "comment" else "deleted from"
        lines.append(f"  migrated sections will be {verb} {plan.config_path}")
    if plan.rewrites:
        count = len(plan.rewritten_resources)
        lines.append(f"Upgrading {count} manifest resource(s) off the retired capability shape:")
        for rewrite in plan.rewrites:
            lines.append(f"  rewrite {rewrite.path}: {', '.join(rewrite.resources)}")
    if plan.drops_secret_backends:
        lines.append(
            "  deprecated [secret_backends.*] sections will be dropped "
            "(no-ops; the built-in backends ship with agentworks)"
        )
    return lines


def render_dry_run(plan: MigrationPlan, *, full: bool = False) -> list[str]:
    """Dry-run output: the summary, plus (with ``full``) the would-be
    YAML documents and the TOML diff. Summary-only is the default --
    the full content of a whole-config run is unusably long as a
    first answer (maintainer ruling, 2026-07-05)."""
    lines = render_preview(plan)
    if not full:
        lines.append("")
        lines.append("(Pass --full to include the YAML documents and the config.toml diff.)")
        return lines
    for write in plan.writes:
        verb = "appended to" if write.exists else "written to"
        lines.append("")
        lines.append(f"Documents {verb} {write.path}:")
        if write.header is not None:
            # A created file opens with the editor modeline, so a dry run
            # that omitted it would not be showing what lands.
            lines.append(write.header)
        for index, document in enumerate(write.documents):
            if index or write.exists:
                lines.append("---")
            lines.extend(document.rstrip("\n").splitlines())
    for rewrite in plan.rewrites:
        lines.append("")
        lines.append(f"Manifest changes: {rewrite.path}")
        lines.extend(
            difflib.unified_diff(
                rewrite.old_bytes.decode("utf-8").splitlines(),
                rewrite.new_text.splitlines(),
                fromfile=f"{rewrite.path} (current)",
                tofile=f"{rewrite.path} (after)",
                lineterm="",
            )
        )
    diff = list(
        difflib.unified_diff(
            plan.old_toml_text.splitlines(),
            plan.new_toml_text.splitlines(),
            fromfile=f"{plan.config_path} (current)",
            tofile=f"{plan.config_path} (after)",
            lineterm="",
        )
    )
    if diff:
        lines.append("")
        lines.append("Config.toml changes:")
        lines.extend(diff)
    return lines
