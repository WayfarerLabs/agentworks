"""Rulesync skill generation from nerf manifests (v1).

Generates a markdown skill file per package. The skill describes all tools in
the package so AI coding assistants know how to use them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nerftools.rendering import arg_line, maps_to_text, option_line, switch_line, usage_tokens

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import NerfManifest, ToolSpec

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

    usage = " ".join([f"<nerf-bin>/{tool_name}", *usage_tokens(tool_spec)])
    parts.append(f"**Usage:** `{usage}`")

    maps_to = maps_to_text(tool_spec)
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
                parts.append(switch_line(sw))
            parts.append("")

        if tool_spec.options:
            parts.append("**Options:**")
            parts.append("")
            for name, opt in tool_spec.options.items():
                parts.append(option_line(name, opt))
            parts.append("")

        if tool_spec.arguments:
            parts.append("**Arguments:**")
            parts.append("")
            for name, spec in tool_spec.arguments.items():
                parts.append(arg_line(name, spec))
            parts.append("")
    else:
        if tool_spec.passthrough is None:
            parts.append("No arguments.")
            parts.append("")

    parts.append("---")
    parts.append("")

    return "\n".join(parts)


