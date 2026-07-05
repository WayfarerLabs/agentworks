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
            lines.append(f"  {unit.kind}/{unit.name} -> {target}")
        for write in plan.writes:
            action = "append to" if write.exists else "create"
            lines.append(
                f"  {action} {write.path} ({len(write.documents)} document(s))"
            )
        verb = "commented out in" if plan.toml_mode == "comment" else "deleted from"
        lines.append(f"  migrated sections will be {verb} {plan.config_path}")
    if plan.drops_secret_backends:
        lines.append(
            "  deprecated [secret_backends.*] sections will be dropped "
            "(no-ops; the built-in backends ship with agentworks)"
        )
    return lines


def render_dry_run(plan: MigrationPlan) -> list[str]:
    """Full dry-run detail: the would-be YAML plus the TOML diff."""
    lines = render_preview(plan)
    for write in plan.writes:
        header = "appended to" if write.exists else "written to"
        lines.append("")
        lines.append(f"--- documents {header} {write.path} ---")
        for index, document in enumerate(write.documents):
            if index or write.exists:
                lines.append("---")
            lines.extend(document.rstrip("\n").splitlines())
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
        lines.append("--- config.toml changes ---")
        lines.extend(diff)
    return lines
