"""Shell script generation from nerf manifest tool specs.

Each tool becomes a self-contained bash script with all argument parsing,
validation, and error formatting inlined. Scripts use exec as the final
instruction so exit codes and signal handling match the underlying tool.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from nerftools.manifest import NerfManifest, ParamSpec, ToolSpec

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


# -- Public API ----------------------------------------------------------------


def build_scripts(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
) -> list[Path]:
    """Generate shell scripts for all tools in all manifests.

    By default, all files in output_dir are removed before writing so stale
    tools do not linger. Pass keep_existing=True to preserve unmanaged files.

    Returns the list of files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if not keep_existing:
        for f in output_dir.iterdir():
            if f.is_file():
                f.unlink()

    written: list[Path] = []

    for manifest in manifests:
        for tool_name, tool_spec in manifest.tools.items():
            script = _build_script(tool_name, manifest.package.name, tool_spec)
            out = output_dir / tool_name
            out.write_text(script)
            out.chmod(0o755)
            written.append(out)

    return written


def build_script_text(tool_name: str, package_name: str, tool_spec: ToolSpec) -> str:
    """Return the generated script text for a single tool (for testing)."""
    return _build_script(tool_name, package_name, tool_spec)


# -- Script generation ---------------------------------------------------------


def _build_script(tool_name: str, package_name: str, tool_spec: ToolSpec) -> str:
    parts: list[str] = []

    parts.append("#!/usr/bin/env bash")
    parts.append(f"# {tool_name} -- {tool_spec.description}")
    parts.append(f"# Generated from {package_name} manifest. Do not edit directly.")
    parts.append("")
    parts.append("set -euo pipefail")
    parts.append("")
    parts.append(_usage_function(tool_name, tool_spec))

    flag_params = {n: p for n, p in tool_spec.params.items() if p.flag}
    positional_params = {n: p for n, p in tool_spec.params.items() if p.positional}

    if flag_params:
        parts.append(_var_declarations(flag_params))
        parts.append("")
        parts.append(_arg_parser(flag_params))
        parts.append("")

    if positional_params:
        parts.append(_positional_parser(positional_params))
        parts.append("")

    if tool_spec.params:
        parts.append(_validations(tool_spec.params))

    if tool_spec.env:
        parts.append(_env_exports(tool_spec.env))
        parts.append("")

    parts.append(_exec_line(tool_spec))

    return "\n".join(parts) + "\n"


def _usage_function(tool_name: str, tool_spec: ToolSpec) -> str:
    flag_params = {n: p for n, p in tool_spec.params.items() if p.flag}
    positional_params = {n: p for n, p in tool_spec.params.items() if p.positional}

    usage_parts = [tool_name]
    for name, p in flag_params.items():
        val = f"<{name}>"
        flag_str = f"{p.flag} {val}"
        usage_parts.append(flag_str if p.required else f"[{flag_str}]")
    for name, p in positional_params.items():
        usage_parts.append(f"<{name}>" if p.required else f"[<{name}>]")
    usage_line = " ".join(usage_parts)

    lines = [f"Usage: {usage_line}", ""]

    if flag_params:
        for name, p in flag_params.items():
            required_marker = " (required)" if p.required else ""
            lines.append(f"  {p.flag} <{name}>{required_marker}")
            lines.append(f"      {p.description}")
            _append_constraints(lines, p, indent="      ")
        lines.append("")

    if positional_params:
        for name, p in positional_params.items():
            required_marker = " (required)" if p.required else ""
            lines.append(f"  <{name}>{required_marker}")
            lines.append(f"      {p.description}")
            _append_constraints(lines, p, indent="      ")
        lines.append("")

    lines.append(tool_spec.description + ".")

    if tool_spec.example:
        lines.append("")
        lines.append(f"Example: {tool_spec.example}")

    body = "\n".join(lines)
    return f"usage() {{\n  cat >&2 <<'EOF'\n{body}\nEOF\n  exit 1\n}}"


def _append_constraints(lines: list[str], p: ParamSpec, indent: str) -> None:
    if p.pattern:
        lines.append(f"{indent}Must match: {p.pattern}")
    if p.allow:
        lines.append(f"{indent}Allowed values: {', '.join(p.allow)}")
    if p.deny:
        lines.append(f"{indent}Not allowed: {', '.join(p.deny)}")
    if p.default is not None:
        lines.append(f"{indent}Default: {p.default}")


def _var_declarations(flag_params: dict[str, ParamSpec]) -> str:
    lines = []
    for name, p in flag_params.items():
        var = _var_name(name)
        default = p.default if p.default is not None else ""
        lines.append(f'{var}="{default}"')
    return "\n".join(lines)


def _arg_parser(flag_params: dict[str, ParamSpec]) -> str:
    cases = []
    for name, p in flag_params.items():
        var = _var_name(name)
        cases.append(f'    {p.flag}) {var}="$2"; shift 2 ;;')
    cases.append("    -h|--help) usage ;;")
    cases.append('    *) echo "error: unknown argument: $1" >&2; usage ;;')

    return "\n".join(
        [
            "while [[ $# -gt 0 ]]; do",
            '  case "$1" in',
            *cases,
            "  esac",
            "done",
        ]
    )


def _positional_parser(positional_params: dict[str, ParamSpec]) -> str:
    lines = []
    for name in positional_params:
        var = _var_name(name)
        lines.append(f'{var}="${{1:-}}"')
        lines.append("shift 2>/dev/null || true")
    return "\n".join(lines)


def _validations(params: dict[str, ParamSpec]) -> str:
    lines = []
    for name, p in params.items():
        var = _var_name(name)

        if p.required:
            lines.append(f'if [[ -z "${{{var}}}" ]]; then')
            if p.flag:
                lines.append(f'  echo "error: {p.flag} is required" >&2; usage')
            else:
                lines.append(f'  echo "error: <{name}> is required" >&2; usage')
            lines.append("fi")
            lines.append("")

        if p.pattern:
            safe_pattern = p.pattern.replace("'", "'\"'\"'")
            lines.append(f'if [[ -n "${{{var}}}" ]] && ! [[ "${{{var}}}" =~ {safe_pattern} ]]; then')
            lines.append(f'  echo "error: {p.flag or f"<{name}>"} must match {p.pattern}" >&2; exit 1')
            lines.append("fi")
            lines.append("")

        if p.allow:
            allow_checks = " && ".join(f'"${{{var}}}" != "{v}"' for v in p.allow)
            lines.append(f'if [[ -n "${{{var}}}" ]] && [[ {allow_checks} ]]; then')
            vals = ", ".join(p.allow)
            lines.append(f'  echo "error: {p.flag or f"<{name}>"} must be one of: {vals}" >&2; exit 1')
            lines.append("fi")
            lines.append("")

        if p.deny:
            for denied in p.deny:
                lines.append(f'if [[ "${{{var}}}" == "{denied}" ]]; then')
                lines.append(f'  echo "error: {p.flag or f"<{name}>"} cannot be \\"{denied}\\"" >&2; exit 1')
                lines.append("fi")
            lines.append("")

    return "\n".join(lines)


def _env_exports(env: dict[str, str]) -> str:
    return "\n".join(f'export {k}="{v}"' for k, v in env.items())


def _exec_line(tool_spec: ToolSpec) -> str:
    args: list[str] = []
    for part in tool_spec.command:
        m = _PLACEHOLDER_RE.fullmatch(part)
        if m:
            args.append(f'"${{{_var_name(m.group(1))}}}"')
        else:
            args.append(part)
    return "exec " + " ".join(args)


def _var_name(param_name: str) -> str:
    return param_name.upper().replace("-", "_")
