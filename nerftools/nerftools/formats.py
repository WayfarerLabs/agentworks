"""Claude Code plugin builder for nerf tools (v1).

Generates a self-contained Claude Code plugin from nerf manifests, including
skills, scripts, plugin manifest, and marketplace metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nerftools.rendering import arg_line, maps_to_text, option_line, switch_line, usage_tokens

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import NerfManifest, ToolSpec

_NERFCTL_SKILLS = [
    {
        "dir_name": "nerfctl-grant-allow",
        "content": """\
---
name: nerfctl-grant-allow
description: Allow nerf tools without prompting (supports glob patterns like nerf-git-*)
argument-hint: <pattern> [--scope user|local]
disable-model-invocation: true
allowed-tools: Bash
---

Allow nerf tools matching the given pattern without prompting. Supports glob patterns
(e.g. `nerf-git-*` to allow all git tools). Default scope is user.

Quote all arguments so they are passed to the script unprocessed by the shell.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-grant-allow $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerfctl-grant-deny",
        "content": """\
---
name: nerfctl-grant-deny
description: Deny nerf tools entirely (supports glob patterns like nerf-git-*)
argument-hint: <pattern> [--scope user|local]
disable-model-invocation: true
allowed-tools: Bash
---

Deny nerf tools matching the given pattern entirely. Supports glob patterns
(e.g. `nerf-git-*` to deny all git tools). Default scope is user.

Quote all arguments so they are passed to the script unprocessed by the shell.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-grant-deny $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerfctl-grant-reset",
        "content": """\
---
name: nerfctl-grant-reset
description: Reset nerf tools to ask-every-time (supports glob patterns like nerf-git-*)
argument-hint: <pattern> [--scope user|local]
disable-model-invocation: true
allowed-tools: Bash
---

Reset permissions for nerf tools matching the given pattern back to the default
ask-every-time behavior. Supports glob patterns. Default scope is user.

Quote all arguments so they are passed to the script unprocessed by the shell.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-grant-reset $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerfctl-grant-by-threat",
        "content": """\
---
name: nerfctl-grant-by-threat
description: Allow/deny nerf tools by threat profile (read/write ceiling)
argument-hint: --read <level> --write <level> [--filter <glob>] [--outside deny|reset] [--scope user|local]
disable-model-invocation: true
allowed-tools: Bash
---

Allow or deny nerf tools based on their threat profile. Tools within the
threat box (read <= ceiling AND write <= ceiling) are allowed. Tools outside
are denied or reset.

Threat levels (narrow to broad): `none`, `workspace`, `machine`, `remote`, `admin`

Quote all arguments so they are passed to the script unprocessed by the shell.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-grant-by-threat $ARGUMENTS
```

Report the output to the user.
""",
    },
    {
        "dir_name": "nerfctl-grant-list",
        "content": """\
---
name: nerfctl-grant-list
description: List nerf tool permissions across all scopes
argument-hint: [--scope user|local]
disable-model-invocation: true
allowed-tools: Bash
---

List all nerf tool permissions. Shows all scopes unless a specific scope is requested.

Run this command:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nerfctl-grant-list $ARGUMENTS
```

Report the output to the user.
""",
    },
]


def build_claude_plugin(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
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

    # Always start clean
    output_dir.mkdir(parents=True, exist_ok=True)
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
    nerfctl_written = install_nerfctl(scripts_root)
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
    parts: list[str] = []
    parts.append(f"## {tool_name}")
    parts.append("")
    parts.append(tool_spec.description + ".")
    parts.append("")

    script_path = f"${{CLAUDE_PLUGIN_ROOT}}/skills/{skill_group}/scripts/{tool_name}"
    usage = " ".join([script_path, *usage_tokens(tool_spec)])
    parts.append(f"**Usage:** `{usage}`")

    maps_to = maps_to_text(tool_spec)
    if maps_to:
        parts.append(f"**Maps to:** `{maps_to}`")
    parts.append("")

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
