"""Output format builders for nerf tools.

Each format generates a complete, self-contained output directory from a set
of manifests. The format determines the directory layout, how skills reference
scripts, and what metadata is included.

Supported formats:
  - claude-plugin: Self-contained Claude Code plugin with skills and scripts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import NerfManifest, ToolSpec

KNOWN_FORMATS = ("claude-plugin",)

_NERFCTL_SKILLS = [
    {
        "dir_name": "nerf-grant",
        "content": """\
---
name: nerf-grant
description: Grant a nerf tool permission in Claude Code settings
argument-hint: <tool-name>
disable-model-invocation: true
allowed-tools: Bash
---

Grant permission for the specified nerf tool by running the nerfctl-claude-grant script.

Run this command:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-claude-grant $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerf-deny",
        "content": """\
---
name: nerf-deny
description: Deny a nerf tool permission in Claude Code settings
argument-hint: <tool-name>
disable-model-invocation: true
allowed-tools: Bash
---

Deny permission for the specified nerf tool by running the nerfctl-claude-deny script.

Run this command:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-claude-deny $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerf-reset",
        "content": """\
---
name: nerf-reset
description: Remove a nerf tool from Claude Code permission lists
argument-hint: <tool-name>
disable-model-invocation: true
allowed-tools: Bash
---

Reset permissions for the specified nerf tool by running the nerfctl-claude-reset script.

Run this command:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-claude-reset $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerf-list",
        "content": """\
---
name: nerf-list
description: List nerf tool permissions in Claude Code settings
disable-model-invocation: true
allowed-tools: Bash
---

List all nerf tool permissions by running the nerfctl-claude-list script.

Run this command:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-claude-list $ARGUMENTS
```

Report the output to the user.
""",
    },
]


def build_format(
    fmt: str,
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
    prefix: str = "nerf-",
) -> list[Path]:
    """Build all artifacts for a given output format.

    Args:
        fmt: Output format name (e.g. "claude-plugin").
        manifests: Loaded and merged nerf manifests.
        output_dir: Root directory for this format's output.
        keep_existing: If True, preserve unmanaged files in output_dir.
        prefix: Prefix for tool names and skill directories.

    Returns:
        List of paths written.
    """
    if fmt == "claude-plugin":
        return _build_claude_plugin(manifests, output_dir, keep_existing=keep_existing, prefix=prefix)
    msg = f"unknown output format '{fmt}'. Known formats: {', '.join(KNOWN_FORMATS)}"
    raise ValueError(msg)


# -- claude-plugin format ------------------------------------------------------


def _build_claude_plugin(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
    prefix: str = "nerf-",
) -> list[Path]:
    """Build a self-contained Claude Code plugin.

    Layout:
        output_dir/
        ├── .claude-plugin/
        │   ├── plugin.json
        │   └── marketplace.json
        └── skills/
            ├── nerftools/SKILL.md           (overview)
            ├── <prefix><group>/
            │   ├── SKILL.md
            │   └── scripts/<prefix><tool>   (executable scripts)
            └── ...
    """
    import json
    import shutil

    from nerftools import install_nerfctl
    from nerftools.builder import build_script_text

    written: list[Path] = []

    # Clean output dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if not keep_existing:
        for item in output_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            elif item.is_file():
                item.unlink()

    # Plugin manifest
    plugin_dir = output_dir / ".claude-plugin"
    plugin_dir.mkdir(exist_ok=True)

    plugin_json = {
        "name": "nerftools",
        "version": "1.0.0",
        "description": "Nerf tools -- scoped, safety-constrained CLI wrappers for AI agents",
        "skills": "./skills/",
    }
    p = plugin_dir / "plugin.json"
    p.write_text(json.dumps(plugin_json, indent=2) + "\n")
    written.append(p)

    marketplace_json = {
        "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
        "name": "agentworks-nerf-local",
        "description": "Local nerf tools plugin from agentworks",
        "owner": {"name": "Agentworks"},
        "plugins": [
            {
                "name": "nerftools",
                "description": "Nerf tools -- scoped, safety-constrained CLI wrappers for AI agents",
                "author": {"name": "Agentworks"},
                "source": "./",
                "category": "development",
            }
        ],
    }
    p = plugin_dir / "marketplace.json"
    p.write_text(json.dumps(marketplace_json, indent=2) + "\n")
    written.append(p)

    skills_dir = output_dir / "skills"
    skills_dir.mkdir(exist_ok=True)

    # Per-package: skill + scripts
    for manifest in manifests:
        group = prefix + manifest.package.skill_group
        skill_dir = skills_dir / group
        skill_dir.mkdir(exist_ok=True)
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)

        # Generate scripts into the skill's scripts/ dir
        for tool_name, tool_spec in manifest.tools.items():
            full_name = prefix + tool_name
            script_text = build_script_text(full_name, manifest.package.name, tool_spec)
            out = scripts_dir / full_name
            out.write_bytes(script_text.encode("utf-8"))
            out.chmod(0o755)
            written.append(out)

        # Generate skill with claude-plugin path references
        skill_text = _build_claude_plugin_skill_text(manifest, prefix=prefix)
        out = skill_dir / "SKILL.md"
        out.write_text(skill_text)
        written.append(out)

    # nerfctl scripts go in the plugin-level scripts/ dir
    scripts_root = output_dir / "scripts"
    scripts_root.mkdir(exist_ok=True)
    nerfctl_written = install_nerfctl("claude", scripts_root)
    written.extend(nerfctl_written)

    # nerfctl user-invokable skills (grant, deny, reset, list)
    for nerfctl_skill in _NERFCTL_SKILLS:
        skill_dir = skills_dir / nerfctl_skill["dir_name"]
        skill_dir.mkdir(exist_ok=True)
        out = skill_dir / "SKILL.md"
        out.write_text(nerfctl_skill["content"])
        written.append(out)

    # Overview skill
    if manifests:
        overview_text = _build_claude_plugin_overview_text(manifests, prefix=prefix)
        overview_dir = skills_dir / "nerftools"
        overview_dir.mkdir(exist_ok=True)
        out = overview_dir / "SKILL.md"
        out.write_text(overview_text)
        written.append(out)

    return written


def _build_claude_plugin_skill_text(manifest: NerfManifest, prefix: str = "") -> str:
    """Generate a SKILL.md for the claude-plugin format.

    Uses ${CLAUDE_PLUGIN_ROOT} for script paths so Claude Code resolves them
    to absolute paths before the agent sees them.
    """
    parts: list[str] = []
    skill_group = prefix + manifest.package.skill_group

    parts.append("---")
    parts.append(f"name: {skill_group}")
    parts.append(f'description: "{manifest.package.description}"')
    parts.append('targets: ["*"]')
    parts.append("---")
    parts.append("")
    parts.append(f"# {skill_group}")
    parts.append("")
    parts.append(
        "These tools are available as scripts within this plugin. "
        "Call them using the absolute paths shown in each usage line."
    )
    parts.append("")

    if manifest.package.skill_intro:
        parts.append(manifest.package.skill_intro.strip())
        parts.append("")

    for tool_name, tool_spec in manifest.tools.items():
        full_name = prefix + tool_name
        parts.append(_claude_plugin_tool_section(full_name, skill_group, tool_spec))

    return "\n".join(parts).rstrip() + "\n"


def _claude_plugin_tool_section(tool_name: str, skill_group: str, tool_spec: ToolSpec) -> str:
    """Generate a tool section for the claude-plugin format."""
    import re

    parts: list[str] = []
    parts.append(f"## {tool_name}")
    parts.append("")
    parts.append(tool_spec.description + ".")
    parts.append("")

    # Usage line with resolved plugin path
    script_path = f"${{CLAUDE_PLUGIN_ROOT}}/skills/{skill_group}/scripts/{tool_name}"
    usage_parts = [script_path]
    for name, p in tool_spec.flags.items():
        flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
        if p.boolean:
            usage_parts.append(f"[{flag_display}]")
        else:
            token = f"{flag_display} <{name}>"
            usage_parts.append(token if p.required else f"[{token}]")
    for name, spec in tool_spec.args.items():
        token = f"<{name}...>" if spec.variadic else f"<{name}>"
        usage_parts.append(token if spec.required else f"[{token}]")

    parts.append(f"**Usage:** `{' '.join(usage_parts)}`")

    # Maps-to line
    maps_parts: list[str] = []
    if tool_spec.npm_pkgrun:
        maps_parts.append("<runner>")
    for token in tool_spec.command:
        maps_parts.append(re.sub(r"\{\{(\w+)\}\}", r"<\1>", token))
    parts.append(f"**Maps to:** `{' '.join(maps_parts)}`")
    parts.append("")

    # Arguments
    has_params = bool(tool_spec.flags) or bool(tool_spec.args)
    if has_params:
        parts.append("**Arguments:**")
        parts.append("")
        for _name, p in tool_spec.flags.items():
            flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
            if p.boolean:
                parts.append(f"- `{flag_display}` (boolean): {p.description}")
            else:
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
                parts.append(f"- `{flag_display}` ({required}): {p.description}{suffix}")
        for name, spec in tool_spec.args.items():
            required = "required" if spec.required else "optional"
            label = f"<{name}...>" if spec.variadic else f"<{name}>"
            constraints = []
            if spec.pattern:
                constraints.append(f"must match `{spec.pattern}`")
            if spec.allow:
                vals = ", ".join(f"`{v}`" for v in spec.allow)
                constraints.append(f"one of {vals}")
            if spec.deny:
                vals = ", ".join(f"`{v}`" for v in spec.deny)
                constraints.append(f"not {vals}")
            suffix = ". " + "; ".join(constraints) if constraints else ""
            parts.append(f"- `{label}` ({required}): {spec.description}{suffix}")
        parts.append("")
    else:
        parts.append("No arguments.")
        parts.append("")

    parts.append("---")
    parts.append("")
    return "\n".join(parts)


def _build_claude_plugin_overview_text(manifests: list[NerfManifest], prefix: str = "") -> str:
    """Generate the nerftools overview SKILL.md for the claude-plugin format."""
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
        "Each tool's usage line shows the full absolute path to call it. Use that path directly in Bash commands."
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
