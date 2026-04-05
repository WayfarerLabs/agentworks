"""Rulesync skill generation from nerf manifests (v1).

Generates a markdown skill file per package. The skill describes all tools in
the package so AI coding assistants know how to use them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import ArgSpec, NerfManifest, OptionSpec, SwitchSpec, ToolSpec

# -- Public API ----------------------------------------------------------------


def build_skills(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
    prefix: str = "nerf-",
) -> list[Path]:
    """Generate rulesync skill files for all manifests.

    Each package gets a <prefix><skill_group>/SKILL.md directory+file.

    By default, all subdirectories in output_dir are removed before writing so
    stale skill groups do not linger. Pass keep_existing=True to preserve them.

    The prefix is prepended to the skill group directory name and all tool names
    within the skill file. Defaults to "nerf-".

    Returns written paths.
    """
    import shutil

    output_dir.mkdir(parents=True, exist_ok=True)

    if not keep_existing:
        for d in output_dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d)

    written: list[Path] = []

    for manifest in manifests:
        text = build_skill_text(manifest, prefix=prefix)
        skill_dir = output_dir / (prefix + manifest.package.skill_group)
        skill_dir.mkdir(exist_ok=True)
        out = skill_dir / "SKILL.md"
        out.write_text(text)
        written.append(out)

    # Generate nerftools overview skill
    if manifests:
        overview_text = build_overview_text(manifests, prefix=prefix)
        overview_dir = output_dir / "nerftools"
        overview_dir.mkdir(exist_ok=True)
        out = overview_dir / "SKILL.md"
        out.write_text(overview_text)
        written.append(out)

    return written


def build_overview_text(manifests: list[NerfManifest], prefix: str = "") -> str:
    """Return the generated nerftools overview SKILL.md."""
    parts: list[str] = []

    parts.append("---")
    parts.append("name: nerftools")
    parts.append('description: "Nerf tools overview and usage guidance"')
    parts.append('targets: ["*"]')
    parts.append("---")
    parts.append("")
    parts.append("# Nerf Tools")
    parts.append("")
    parts.append(
        "This environment has nerf tools installed -- scoped, safety-constrained wrappers for "
        "common CLI operations like git, az, and other tools. They enforce guardrails (validated "
        "parameters, restricted flags, pre-flight checks) that keep operations safe and auditable."
    )
    parts.append("")
    parts.append(
        "When a nerf tool exists that covers the operation you need, prefer it over invoking the "
        "underlying tool directly. Shape your workflow to take advantage of them. For example, "
        "stage files with the nerf git-add tool and then commit with the nerf git-commit tool, "
        "rather than using raw `git` commands."
    )
    parts.append("")
    parts.append(
        "To find the nerf bin directory, resolve the `$AGENTWORKS_NERF_BIN` environment variable "
        "(e.g. `echo $AGENTWORKS_NERF_BIN`). Then invoke tools using the resolved absolute path "
        "(e.g. `/opt/agentworks/nerf/bin/nerf-git-commit`). Using the absolute path is required "
        "so that permission entries can match the command exactly."
    )
    parts.append("")
    parts.append("## Available tool families")
    parts.append("")

    for manifest in manifests:
        group = prefix + manifest.package.skill_group
        parts.append(f"- **{group}**: {manifest.package.description}")

    parts.append("")
    parts.append("Use the corresponding `nerf-*` skill for full usage details on each family.")

    return "\n".join(parts).rstrip() + "\n"


def build_skill_text(manifest: NerfManifest, prefix: str = "") -> str:
    """Return the generated SKILL.md content for a manifest (for testing).

    The prefix is prepended to the skill group name and all tool names.
    Pass prefix="" (default) to get unprefixed output, as used in tests.
    """
    parts: list[str] = []

    skill_group = prefix + manifest.package.skill_group

    # Rulesync frontmatter
    parts.append("---")
    parts.append(f"name: {skill_group}")
    parts.append(f'description: "{manifest.package.description}"')
    parts.append('targets: ["*"]')
    parts.append("---")
    parts.append("")

    parts.append(f"# {skill_group}")
    parts.append("")
    parts.append(
        "Resolve `$AGENTWORKS_NERF_BIN` to get the nerf bin directory, then invoke tools "
        "using the resolved absolute path. Do not use the env var directly in commands."
    )
    parts.append("")

    if manifest.package.skill_intro:
        parts.append(manifest.package.skill_intro.strip())
        parts.append("")

    for tool_name, tool_spec in manifest.tools.items():
        parts.append(_tool_section(prefix + tool_name, tool_spec))

    return "\n".join(parts).rstrip() + "\n"


# -- Section generation --------------------------------------------------------


def _tool_section(tool_name: str, tool_spec: ToolSpec) -> str:
    parts: list[str] = []

    parts.append(f"## {tool_name}")
    parts.append("")
    parts.append(tool_spec.description + ".")
    parts.append("")

    usage = _usage_line(tool_name, tool_spec)
    parts.append(f"**Usage:** `{usage}`")

    maps_to = _maps_to_line(tool_spec)
    if maps_to:
        parts.append(f"**Maps to:** `{maps_to}`")
    parts.append("")

    # Passthrough deny patterns
    if tool_spec.passthrough is not None and tool_spec.passthrough.deny:
        denied = ", ".join(f"`{d}`" for d in tool_spec.passthrough.deny)
        parts.append(f"**Denied patterns:** {denied}")
        parts.append("")

    has_params = bool(tool_spec.switches) or bool(tool_spec.options) or bool(tool_spec.arguments)

    if has_params:
        if tool_spec.switches:
            parts.append("**Switches:**")
            parts.append("")
            for _name, sw in tool_spec.switches.items():
                parts.append(_switch_line(sw))
            parts.append("")

        if tool_spec.options:
            parts.append("**Options:**")
            parts.append("")
            for name, opt in tool_spec.options.items():
                parts.append(_option_line(name, opt))
            parts.append("")

        if tool_spec.arguments:
            parts.append("**Arguments:**")
            parts.append("")
            for name, spec in tool_spec.arguments.items():
                parts.append(_arg_line(name, spec))
            parts.append("")
    else:
        if tool_spec.passthrough is None:
            parts.append("No arguments.")
            parts.append("")

    parts.append("---")
    parts.append("")

    return "\n".join(parts)


def _usage_line(tool_name: str, tool_spec: ToolSpec) -> str:
    usage_parts = [f"<nerf-bin>/{tool_name}"]

    for _name, sw in tool_spec.switches.items():
        flag_display = f"{sw.flag}|{sw.short}" if sw.short else sw.flag
        usage_parts.append(f"[{flag_display}]")

    for name, opt in tool_spec.options.items():
        flag_display = f"{opt.flag}|{opt.short}" if opt.short else opt.flag
        token = f"{flag_display} <{name}>"
        usage_parts.append(token if opt.required else f"[{token}]")

    for name, spec in tool_spec.arguments.items():
        token = f"<{name}...>" if spec.variadic else f"<{name}>"
        usage_parts.append(token if spec.required else f"[{token}]")

    if tool_spec.passthrough is not None and not tool_spec.arguments:
        usage_parts.append("[tokens...]")

    return " ".join(usage_parts)


def _maps_to_line(tool_spec: ToolSpec) -> str | None:
    """Show the underlying command with placeholders replaced by <name>."""
    if tool_spec.template is not None:
        parts: list[str] = []
        if tool_spec.template.npm_pkgrun:
            parts.append("<runner>")
        for token in tool_spec.template.command:
            parts.append(re.sub(r"\{\{(\w+)\}\}", r"<\1>", token))
        return " ".join(parts)
    if tool_spec.passthrough is not None:
        pt = tool_spec.passthrough
        parts = [pt.command]
        parts.extend(pt.prefix)
        parts.append('"$@"')
        parts.extend(pt.suffix)
        return " ".join(parts)
    return None


def _switch_line(sw: SwitchSpec) -> str:
    flag_display = f"{sw.flag}, {sw.short}" if sw.short else sw.flag
    return f"- `{flag_display}`: {sw.description}"


def _option_line(name: str, opt: OptionSpec) -> str:
    flag_display = f"{opt.flag}|{opt.short}" if opt.short else opt.flag
    required = "required" if opt.required else "optional"

    constraints: list[str] = []
    if opt.pattern:
        constraints.append(f"must match `{opt.pattern}`")
    if opt.allow:
        vals = ", ".join(f"`{v}`" for v in opt.allow)
        constraints.append(f"one of {vals}")
    if opt.deny:
        vals = ", ".join(f"`{v}`" for v in opt.deny)
        constraints.append(f"not {vals}")

    suffix = ". " + "; ".join(constraints) if constraints else ""
    return f"- `{flag_display}` ({required}): {opt.description}{suffix}"


def _arg_line(name: str, spec: ArgSpec) -> str:
    required = "required" if spec.required else "optional"
    label = f"<{name}...>" if spec.variadic else f"<{name}>"

    constraints: list[str] = []
    if spec.pattern:
        constraints.append(f"must match `{spec.pattern}`")
    if spec.allow:
        vals = ", ".join(f"`{v}`" for v in spec.allow)
        constraints.append(f"one of {vals}")
    if spec.deny:
        vals = ", ".join(f"`{v}`" for v in spec.deny)
        constraints.append(f"not {vals}")

    suffix = ". " + "; ".join(constraints) if constraints else ""
    return f"- `{label}` ({required}): {spec.description}{suffix}"
