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

    from nerftools.manifest import ArgSpec, FlagSpec, NerfManifest, ToolSpec

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


# -- Public API ----------------------------------------------------------------


def build_scripts(
    manifests: list[NerfManifest],
    output_dir: Path,
    *,
    keep_existing: bool = False,
    prefix: str = "nerf-",
) -> list[Path]:
    """Generate shell scripts for all tools in all manifests.

    By default, all files in output_dir are removed before writing so stale
    tools do not linger. Pass keep_existing=True to preserve unmanaged files.

    The prefix is prepended to every generated script filename and tool name
    within the script (usage, header comment). Defaults to "nerf-".

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
            full_name = prefix + tool_name
            script = _build_script(full_name, manifest.package.name, tool_spec)
            out = output_dir / full_name
            out.write_bytes(script.encode("utf-8"))
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

    has_positional = bool(tool_spec.args)

    if tool_spec.flags:
        parts.append("")
        parts.append(_var_declarations(tool_spec.flags))
        parts.append("")
        parts.append(_flag_parser(tool_spec.flags, has_positional=has_positional))

    if tool_spec.args:
        parts.append("")
        parts.append(_positional_parser(tool_spec.args))

    if tool_spec.flags:
        validations = _flag_validations(tool_spec.flags)
        if validations.strip():
            parts.append("")
            parts.append(validations)

    if tool_spec.args:
        validations = _arg_validations(tool_spec.args)
        if validations.strip():
            parts.append("")
            parts.append(validations)

    if tool_spec.env:
        parts.append("")
        parts.append(_env_exports(tool_spec.env))

    if tool_spec.guards:
        parts.append("")
        parts.append(_guard_checks(tool_spec))

    parts.append("")
    parts.append(_exec_line(tool_spec))

    return "\n".join(parts) + "\n"


def _usage_function(tool_name: str, tool_spec: ToolSpec) -> str:
    usage_parts = [tool_name]
    for name, p in tool_spec.flags.items():
        flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
        if p.boolean:
            usage_parts.append(f"[{flag_display}]")
        else:
            flag_str = f"{flag_display} <{name}>"
            usage_parts.append(flag_str if p.required else f"[{flag_str}]")
    for name, spec in tool_spec.args.items():
        token = f"<{name}...>" if spec.variadic else f"<{name}>"
        usage_parts.append(token if spec.required else f"[{token}]")
    usage_line = " ".join(usage_parts)

    lines = [f"Usage: {usage_line}", ""]

    if tool_spec.flags:
        for name, p in tool_spec.flags.items():
            required_marker = " (required)" if p.required else ""
            flag_display = f"{p.flag}|{p.short}" if p.short else p.flag
            if p.boolean:
                lines.append(f"  {flag_display}")
            else:
                lines.append(f"  {flag_display} <{name}>{required_marker}")
            lines.append(f"      {p.description}")
            _append_constraints(lines, p.pattern, p.allow, p.deny, indent="      ")
        lines.append("")

    if tool_spec.args:
        for name, spec in tool_spec.args.items():
            required_marker = " (required)" if spec.required else ""
            arg_label = f"<{name}...>" if spec.variadic else f"<{name}>"
            lines.append(f"  {arg_label}{required_marker}")
            lines.append(f"      {spec.description}")
            _append_constraints(lines, spec.pattern, spec.allow, spec.deny, indent="      ")
        lines.append("")

    lines.append(tool_spec.description + ".")

    body = "\n".join(lines)
    return f"usage() {{\n  cat >&2 <<'EOF'\n{body}\nEOF\n  exit 1\n}}"


def _append_constraints(
    lines: list[str],
    pattern: str | None,
    allow: tuple[str, ...],
    deny: tuple[str, ...],
    indent: str,
) -> None:
    if pattern:
        lines.append(f"{indent}Must match: {pattern}")
    if allow:
        lines.append(f"{indent}Allowed values: {', '.join(allow)}")
    if deny:
        lines.append(f"{indent}Not allowed: {', '.join(deny)}")


def _var_declarations(flags: dict[str, FlagSpec]) -> str:
    lines = []
    for name in flags:
        var = _var_name(name)
        lines.append(f'{var}=""')
    return "\n".join(lines)


def _flag_parser(flags: dict[str, FlagSpec], *, has_positional: bool) -> str:
    cases = []
    for name, p in flags.items():
        var = _var_name(name)
        pattern = f"{p.flag}|{p.short}" if p.short else p.flag
        if p.boolean:
            cases.append(f'    {pattern}) {var}="true"; shift 1 ;;')
        else:
            cases.append(f'    {pattern}) {var}="$2"; shift 2 ;;')
    cases.append("    -h|--help) usage ;;")
    if has_positional:
        cases.append("    *) break ;;")
    else:
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


def _positional_parser(args: dict[str, ArgSpec]) -> str:
    lines = []
    for name, spec in args.items():
        var = _var_name(name)
        if spec.variadic:
            lines.append(f'{var}=("$@")')
        else:
            lines.append(f'{var}="${{1:-}}"')
            lines.append("shift 2>/dev/null || true")
    return "\n".join(lines)


def _flag_validations(flags: dict[str, FlagSpec]) -> str:
    lines: list[str] = []
    for name, p in flags.items():
        var = _var_name(name)

        if p.required:
            lines.append(f'if [[ -z "${{{var}}}" ]]; then')
            lines.append(f'  echo "error: {p.flag} is required" >&2; usage')
            lines.append("fi")
            lines.append("")

        if p.pattern:
            safe_pattern = p.pattern.replace("'", "'\"'\"'")
            lines.append(f'if [[ -n "${{{var}}}" ]] && ! [[ "${{{var}}}" =~ {safe_pattern} ]]; then')
            lines.append(f'  echo "error: {p.flag} must match {p.pattern}" >&2; exit 1')
            lines.append("fi")
            lines.append("")

        if p.allow:
            allow_checks = " && ".join(f'"${{{var}}}" != "{v}"' for v in p.allow)
            vals = ", ".join(p.allow)
            lines.append(f'if [[ -n "${{{var}}}" ]] && [[ {allow_checks} ]]; then')
            lines.append(f'  echo "error: {p.flag} must be one of: {vals}" >&2; exit 1')
            lines.append("fi")
            lines.append("")

        if p.deny:
            for denied in p.deny:
                lines.append(f'if [[ "${{{var}}}" == "{denied}" ]]; then')
                lines.append(f'  echo "error: {p.flag} cannot be \\"{denied}\\"" >&2; exit 1')
                lines.append("fi")
            lines.append("")

    return "\n".join(lines).rstrip()


def _arg_validations(args: dict[str, ArgSpec]) -> str:
    lines: list[str] = []
    for name, spec in args.items():
        var = _var_name(name)

        if spec.variadic:
            lines.append(f'for _v in "${{{var}[@]}}"; do')
            lines.append('  if [[ "$_v" == -* ]]; then')
            lines.append(f'    echo "error: <{name}> values cannot start with \'-\'" >&2; exit 1')
            lines.append("  fi")
            lines.append("done")
            lines.append("")
            if spec.required:
                lines.append(f'if [[ ${{#{var}[@]}} -eq 0 ]]; then')
                lines.append(f'  echo "error: <{name}> is required" >&2; usage')
                lines.append("fi")
                lines.append("")
            if spec.pattern:
                safe_pattern = spec.pattern.replace("'", "'\"'\"'")
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                lines.append(f'  if ! [[ "$_v" =~ {safe_pattern} ]]; then')
                lines.append(f'    echo "error: <{name}> must match {spec.pattern}" >&2; exit 1')
                lines.append("  fi")
                lines.append("done")
                lines.append("")
            if spec.allow:
                allow_checks = " && ".join(f'"$_v" != "{v}"' for v in spec.allow)
                vals = ", ".join(spec.allow)
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                lines.append(f'  if [[ {allow_checks} ]]; then')
                lines.append(f'    echo "error: <{name}> must be one of: {vals}" >&2; exit 1')
                lines.append("  fi")
                lines.append("done")
                lines.append("")
            if spec.deny:
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                for denied in spec.deny:
                    lines.append(f'  if [[ "$_v" == "{denied}" ]]; then')
                    lines.append(f'    echo "error: <{name}> cannot be \\"{denied}\\"" >&2; exit 1')
                    lines.append("  fi")
                lines.append("done")
                lines.append("")
        else:
            lines.append(f'if [[ -n "${{{var}}}" ]] && [[ "${{{var}}}" == -* ]]; then')
            lines.append(f'  echo "error: <{name}> cannot start with \'-\'" >&2; exit 1')
            lines.append("fi")
            lines.append("")
            if spec.required:
                lines.append(f'if [[ -z "${{{var}}}" ]]; then')
                lines.append(f'  echo "error: <{name}> is required" >&2; usage')
                lines.append("fi")
                lines.append("")
            if spec.pattern:
                safe_pattern = spec.pattern.replace("'", "'\"'\"'")
                lines.append(f'if [[ -n "${{{var}}}" ]] && ! [[ "${{{var}}}" =~ {safe_pattern} ]]; then')
                lines.append(f'  echo "error: <{name}> must match {spec.pattern}" >&2; exit 1')
                lines.append("fi")
                lines.append("")
            if spec.allow:
                allow_checks = " && ".join(f'"${{{var}}}" != "{v}"' for v in spec.allow)
                vals = ", ".join(spec.allow)
                lines.append(f'if [[ -n "${{{var}}}" ]] && [[ {allow_checks} ]]; then')
                lines.append(f'  echo "error: <{name}> must be one of: {vals}" >&2; exit 1')
                lines.append("fi")
                lines.append("")
            if spec.deny:
                for denied in spec.deny:
                    lines.append(f'if [[ "${{{var}}}" == "{denied}" ]]; then')
                    lines.append(f'  echo "error: <{name}> cannot be \\"{denied}\\"" >&2; exit 1')
                    lines.append("fi")
                lines.append("")

    return "\n".join(lines).rstrip()


def _env_exports(env: dict[str, str]) -> str:
    return "\n".join(f'export {k}="{v}"' for k, v in env.items())


def _substitute_command(
    command: tuple[str, ...],
    flags: dict[str, FlagSpec],
    args: dict[str, ArgSpec],
) -> list[str]:
    """Substitute {{param}} placeholders in a command word list.

    Required flags/args use "$VAR" (always present).
    Optional flags/single-args use ${VAR:+"$VAR"} (omitted when empty).
    Required variadic args use "${VAR[@]}".
    Optional variadic args use ${VAR[@]+"${VAR[@]}"}.
    Boolean flags use ${VAR:+"--flag"}.
    """
    result: list[str] = []
    for part in command:
        m = _PLACEHOLDER_RE.fullmatch(part)
        if m:
            name = m.group(1)
            var = _var_name(name)
            if name in flags:
                p = flags[name]
                if p.boolean:
                    result.append("${" + var + ':+"' + p.flag + '"' + "}")
                elif p.required:
                    result.append(f'"${{{var}}}"')
                else:
                    result.append("${" + var + ':+"$' + var + '"}')
            elif name in args:
                spec = args[name]
                if spec.variadic:
                    if spec.required:
                        result.append(f'"${{{var}[@]}}"')
                    else:
                        result.append('${' + var + '[@]+"${' + var + '[@]}"}')
                else:
                    if spec.required:
                        result.append(f'"${{{var}}}"')
                    else:
                        result.append("${" + var + ':+"$' + var + '"}')
            else:
                result.append(f'"${{{var}}}"')
        else:
            result.append(part)
    return result


def _substitute_script(
    script: str,
    flags: dict[str, FlagSpec],
    args: dict[str, ArgSpec],
) -> str:
    """Substitute {{param}} placeholders inline within a bash script string.

    Each {{name}} becomes ${VAR} without extra quoting -- the script author
    is responsible for quoting around the placeholder as needed.
    """

    def replace(m: re.Match) -> str:  # type: ignore[type-arg]
        name = m.group(1)
        return "${" + _var_name(name) + "}"

    return _PLACEHOLDER_RE.sub(replace, script)


def _exec_line(tool_spec: ToolSpec) -> str:
    args = _substitute_command(tool_spec.command, tool_spec.flags, tool_spec.args)
    return "exec " + " ".join(args)


def _guard_checks(tool_spec: ToolSpec) -> str:
    """Generate pre-flight guard checks that run before exec.

    Both command and script guards use the same pattern:
      <check> || { echo 'error: <message>' >&2; exit 1; }

    Command guards suppress output. Script guards run as-is (the script is
    responsible for its own redirection if silence is desired).
    Multi-line scripts are wrapped in a subshell.
    """
    lines: list[str] = []
    for guard in tool_spec.guards:
        safe_msg = guard.fail_message.replace("'", "'\"'\"'")

        if guard.command is not None:
            cmd_args = _substitute_command(guard.command, tool_spec.flags, tool_spec.args)
            check = " ".join(cmd_args) + " > /dev/null 2>&1"
            lines.append(f"{check} || {{ echo 'error: {safe_msg}' >&2; exit 1; }}")
        else:
            script_text = _substitute_script(guard.script or "", tool_spec.flags, tool_spec.args)
            script_lines = script_text.strip().splitlines()
            if len(script_lines) == 1:
                lines.append(f"( {script_lines[0]} ) || {{ echo 'error: {safe_msg}' >&2; exit 1; }}")
            else:
                lines.append("(")
                for sl in script_lines:
                    lines.append(f"  {sl}")
                lines.append(f") || {{ echo 'error: {safe_msg}' >&2; exit 1; }}")

    return "\n".join(lines)


def _var_name(param_name: str) -> str:
    return param_name.upper().replace("-", "_")
