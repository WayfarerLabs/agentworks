"""Rulesync skill generation from nerf manifests.

Generates a markdown skill file per package. The skill describes all tools in
the package so AI coding assistants know how to use them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import ArgSpec, FlagSpec, NerfManifest, ToolSpec

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

    return written


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
        "Invoke these tools via the `$AGENTWORKS_NERF_BIN` environment variable "
        "(e.g. `$AGENTWORKS_NERF_BIN/<tool-name>`). Do not assume they are on PATH."
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
    parts.append(f"**Maps to:** `{maps_to}`")
    parts.append("")

    has_params = bool(tool_spec.flags) or bool(tool_spec.args)

    if has_params:
        parts.append("**Arguments:**")
        parts.append("")
        for name, p in tool_spec.flags.items():
            parts.append(_flag_line(name, p))
        for name, spec in tool_spec.args.items():
            parts.append(_arg_line(name, spec))
        parts.append("")
    else:
        parts.append("No arguments.")
        parts.append("")

    parts.append("---")
    parts.append("")

    return "\n".join(parts)


def _usage_line(tool_name: str, tool_spec: ToolSpec) -> str:
    parts = [f"$AGENTWORKS_NERF_BIN/{tool_name}"]
    for name, p in tool_spec.flags.items():
        flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
        if p.boolean:
            parts.append(f"[{flag_display}]")
        else:
            token = f"{flag_display} <{name}>"
            parts.append(token if p.required else f"[{token}]")
    for name, spec in tool_spec.args.items():
        token = f"<{name}...>" if spec.variadic else f"<{name}>"
        parts.append(token if spec.required else f"[{token}]")
    return " ".join(parts)


def _maps_to_line(tool_spec: ToolSpec) -> str:
    """Show the underlying command with placeholders replaced by <name>."""
    import re

    parts: list[str] = []
    for token in tool_spec.command:
        parts.append(re.sub(r"\{\{(\w+)\}\}", r"<\1>", token))
    return " ".join(parts)


def _flag_line(name: str, p: FlagSpec) -> str:
    flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
    desc = p.description

    if p.boolean:
        return f"- `{flag_display}` (boolean): {desc}"

    required = "required" if p.required else "optional"

    constraints: list[str] = []
    if p.pattern:
        constraints.append(f"must match `{p.pattern}`")
    if p.allow:
        vals = ", ".join(f"`{v}`" for v in p.allow)
        constraints.append(f"one of {vals}")
    if p.deny:
        vals = ", ".join(f"`{v}`" for v in p.deny)
        constraints.append(f"not {vals}")

    suffix = ". " + "; ".join(constraints) if constraints else ""
    return f"- `{flag_display}` ({required}): {desc}{suffix}"


def _arg_line(name: str, spec: ArgSpec) -> str:
    required = "required" if spec.required else "optional"
    label = f"<{name}...>" if spec.variadic else f"<{name}>"
    desc = spec.description

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
    return f"- `{label}` ({required}): {desc}{suffix}"
