"""Rulesync skill generation from nerf manifests.

Generates a markdown skill file per package. The skill describes all tools in
the package so AI coding assistants know how to use them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import NerfManifest, ParamSpec, ToolSpec

# -- Public API ----------------------------------------------------------------


def build_skills(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
) -> list[Path]:
    """Generate rulesync skill files for all manifests.

    Each package gets a <skill_group>/SKILL.md directory+file.

    By default, all subdirectories in output_dir are removed before writing so
    stale skill groups do not linger. Pass keep_existing=True to preserve them.

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
        text = build_skill_text(manifest)
        skill_dir = output_dir / manifest.package.skill_group
        skill_dir.mkdir(exist_ok=True)
        out = skill_dir / "SKILL.md"
        out.write_text(text)
        written.append(out)

    return written


def build_skill_text(manifest: NerfManifest) -> str:
    """Return the generated SKILL.md content for a manifest (for testing)."""
    parts: list[str] = []

    # Rulesync frontmatter
    parts.append("---")
    parts.append(f"name: {manifest.package.skill_group}")
    parts.append(f'description: "{manifest.package.description}"')
    parts.append('targets: ["*"]')
    parts.append("---")
    parts.append("")

    parts.append(f"# {manifest.package.skill_group}")
    parts.append("")

    if manifest.package.skill_intro:
        parts.append(manifest.package.skill_intro.strip())
        parts.append("")

    for tool_name, tool_spec in manifest.tools.items():
        parts.append(_tool_section(tool_name, tool_spec))

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
    parts.append("")

    flag_params = {n: p for n, p in tool_spec.params.items() if p.flag}
    positional_params = {n: p for n, p in tool_spec.params.items() if p.positional}

    if flag_params or positional_params:
        parts.append("**Arguments:**")
        parts.append("")
        for name, p in flag_params.items():
            parts.append(_param_line(name, p))
        for name, p in positional_params.items():
            parts.append(_param_line(name, p))
        parts.append("")

    if not tool_spec.params:
        parts.append("No arguments.")
        parts.append("")

    if tool_spec.example:
        parts.append(f"**Example:** `{tool_spec.example}`")
        parts.append("")

    parts.append("---")
    parts.append("")

    return "\n".join(parts)


def _usage_line(tool_name: str, tool_spec: ToolSpec) -> str:
    parts = [tool_name]
    for name, p in tool_spec.params.items():
        if p.flag:
            token = f"{p.flag} <{name}>"
            parts.append(token if p.required else f"[{token}]")
        else:
            parts.append(f"<{name}>" if p.required else f"[<{name}>]")
    return " ".join(parts)


def _param_line(name: str, p: ParamSpec) -> str:
    label = p.flag if p.flag else f"<{name}>"
    required = "required" if p.required else "optional"
    desc = p.description

    constraints: list[str] = []
    if p.pattern:
        constraints.append(f"must match `{p.pattern}`")
    if p.allow:
        vals = ", ".join(f"`{v}`" for v in p.allow)
        constraints.append(f"one of {vals}")
    if p.deny:
        vals = ", ".join(f"`{v}`" for v in p.deny)
        constraints.append(f"not {vals}")
    if p.default is not None:
        constraints.append(f"default: `{p.default}`")

    suffix = ". " + "; ".join(constraints) if constraints else ""
    return f"- `{label}` ({required}): {desc}{suffix}"
