"""Shell script generation from nerf manifest tool specs (v1).

Each tool becomes a self-contained bash script with all argument parsing,
validation, and error formatting inlined. Three execution modes are supported:
template (exec with substituted params), passthrough (deny-scan + exec), and
script (inline bash).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nerftools.manifest import PLACEHOLDER_RE, resolve_placeholder
from nerftools.rendering import maps_to_text, usage_tokens

if TYPE_CHECKING:
    import re
    from pathlib import Path

    from nerftools.manifest import ArgSpec, NerfManifest, ToolSpec


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
    parts.append(f"# nerf:threat:read={tool_spec.threat.read.value}")
    parts.append(f"# nerf:threat:write={tool_spec.threat.write.value}")
    parts.append("")
    parts.append("set -euo pipefail")
    parts.append("")
    parts.append('_NERF_DRY_RUN=""')
    parts.append("")
    parts.append(_usage_function(tool_name, tool_spec))

    has_params = bool(tool_spec.switches) or bool(tool_spec.options)
    has_positional = bool(tool_spec.arguments)

    if has_params:
        parts.append("")
        parts.append(_var_declarations(tool_spec))

    # Flag parser is always emitted (at minimum for --nerf-dry-run and --help)
    is_passthrough = tool_spec.passthrough is not None
    parts.append("")
    parts.append(_flag_parser(
        tool_spec, has_positional=has_positional, is_passthrough=is_passthrough,
    ))

    if has_positional:
        parts.append("")
        parts.append(_positional_parser(tool_spec.arguments))

    if has_params:
        validations = _param_validations(tool_name, tool_spec)
        if validations.strip():
            parts.append("")
            parts.append(validations)

    if has_positional:
        validations = _arg_validations(tool_name, tool_spec.arguments)
        if validations.strip():
            parts.append("")
            parts.append(validations)

    if tool_spec.env:
        parts.append("")
        parts.append(_env_exports(tool_spec.env))

    if tool_spec.guards:
        parts.append("")
        parts.append(_guard_checks(tool_name, tool_spec))

    if tool_spec.pre:
        parts.append("")
        parts.append(_pre_hook(tool_name, tool_spec))

    if tool_spec.template is not None:
        if tool_spec.template.npm_pkgrun:
            parts.append("")
            parts.append(_npm_pkgrun_resolver())
        parts.append("")
        parts.append(_dry_run_check(tool_name, tool_spec))
        parts.append("")
        parts.append(_template_exec(tool_spec))
    elif tool_spec.passthrough is not None:
        parts.append("")
        parts.append(_passthrough_exec(tool_name, tool_spec))
    elif tool_spec.script is not None:
        parts.append("")
        parts.append(_dry_run_check(tool_name, tool_spec))
        parts.append("")
        parts.append(tool_spec.script.rstrip())

    parts.append("")
    return "\n".join(parts)


# -- Usage function ------------------------------------------------------------


def _usage_function(tool_name: str, tool_spec: ToolSpec) -> str:
    usage_line = " ".join([tool_name, *usage_tokens(tool_spec)])
    lines = [f"Usage: {usage_line}", ""]

    # Switches
    if tool_spec.switches:
        lines.append("Switches:")
        for _name, sw in tool_spec.switches.items():
            flag_display = f"{sw.flag}, {sw.short}" if sw.short else f"{sw.flag}"
            lines.append(f"  {flag_display}")
            lines.append(f"      {sw.description}")
        lines.append("")

    # Options
    if tool_spec.options:
        lines.append("Options:")
        for name, opt in tool_spec.options.items():
            flag_display = f"{opt.flag}, {opt.short}" if opt.short else f"{opt.flag}"
            required_marker = " (required)" if opt.required else ""
            lines.append(f"  {flag_display} <{name}>{required_marker}")
            lines.append(f"      {opt.description}")
            _append_constraints(lines, opt.pattern, opt.allow, opt.deny, indent="      ")
        lines.append("")

    # Arguments
    if tool_spec.arguments:
        lines.append("Arguments:")
        for name, spec in tool_spec.arguments.items():
            required_marker = " (required)" if spec.required else ""
            arg_label = f"<{name}...>" if spec.variadic else f"<{name}>"
            lines.append(f"  {arg_label}{required_marker}")
            lines.append(f"      {spec.description}")
            _append_constraints(lines, spec.pattern, spec.allow, spec.deny, indent="      ")
        lines.append("")

    # Maps to (template and passthrough only)
    maps_to = maps_to_text(tool_spec)
    if maps_to:
        lines.append(f"Maps to: {maps_to}")
        lines.append("")

    # Passthrough deny list
    if tool_spec.passthrough is not None and tool_spec.passthrough.deny:
        denied = ", ".join(tool_spec.passthrough.deny)
        lines.append(f"Denied patterns: {denied}")
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


# -- Variable declarations and parsing ----------------------------------------


def _var_declarations(tool_spec: ToolSpec) -> str:
    lines = []
    for name, sw in tool_spec.switches.items():
        var = _var_name(name)
        if sw.repeatable:
            lines.append(f"{var}=0")
        else:
            lines.append(f'{var}=""')
    for name, opt in tool_spec.options.items():
        var = _var_name(name)
        if opt.repeatable:
            lines.append(f"{var}=()")
        else:
            lines.append(f'{var}=""')
    return "\n".join(lines)


def _flag_parser(tool_spec: ToolSpec, *, has_positional: bool, is_passthrough: bool = False) -> str:
    cases = []

    for name, sw in tool_spec.switches.items():
        var = _var_name(name)
        pattern = f"{sw.flag}|{sw.short}" if sw.short else sw.flag
        if sw.repeatable:
            cases.append(f"    {pattern}) {var}=$(({var} + 1)); shift 1 ;;")
        else:
            dup_check = (
                f'if [[ -n "${{{var}}}" ]]; then '
                f'echo "error: {sw.flag} can only be specified once" >&2; exit 1; fi; '
            )
            cases.append(f'    {pattern}) {dup_check}{var}="true"; shift 1 ;;')

    for name, opt in tool_spec.options.items():
        var = _var_name(name)
        pattern = f"{opt.flag}|{opt.short}" if opt.short else opt.flag
        if opt.repeatable:
            cases.append(f'    {pattern}) {var}+=("{opt.flag}" "$2"); shift 2 ;;')
        else:
            dup_check = (
                f'if [[ -n "${{{var}}}" ]]; then '
                f'echo "error: {opt.flag} can only be specified once" >&2; exit 1; fi; '
            )
            cases.append(f'    {pattern}) {dup_check}{var}="$2"; shift 2 ;;')

    cases.append('    --nerf-dry-run) _NERF_DRY_RUN="true"; shift 1 ;;')
    cases.append("    -h|--help) usage ;;")
    cases.append("    --) shift; break ;;")
    if has_positional or is_passthrough:
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


def _positional_parser(arguments: dict[str, ArgSpec]) -> str:
    lines = []
    for name, spec in arguments.items():
        var = _var_name(name)
        if spec.variadic:
            lines.append(f'{var}=("$@")')
        else:
            lines.append(f'{var}="${{1:-}}"')
            lines.append("shift 2>/dev/null || true")
    return "\n".join(lines)


# -- Validations ---------------------------------------------------------------


def _param_validations(tool_name: str, tool_spec: ToolSpec) -> str:
    lines: list[str] = []

    for name, opt in tool_spec.options.items():
        var = _var_name(name)

        if opt.required:
            lines.append(f'if [[ -z "${{{var}}}" ]]; then')
            lines.append(f'  echo "error: {tool_name}: missing required option {opt.flag}" >&2')
            lines.append(f'  echo "  hint: provide {opt.flag} <value>" >&2')
            lines.append("  usage")
            lines.append("fi")
            lines.append("")

        if opt.pattern:
            anchored = _anchored_pattern(opt.pattern)
            lines.append(f"_NERF_PATTERN='{_shell_escape_sq(anchored)}'")
            lines.append(f'if [[ -n "${{{var}}}" ]] && ! [[ "${{{var}}}" =~ $_NERF_PATTERN ]]; then')
            lines.append(f'  echo "error: {tool_name}: option {opt.flag} does not match required pattern" >&2')
            lines.append(f'  echo "  value:   \\"${{{var}}}\\"" >&2')
            lines.append(f'  echo "  pattern: {opt.pattern}" >&2')
            lines.append(f'  echo "  hint: value must match {opt.pattern}" >&2')
            lines.append("  exit 1")
            lines.append("fi")
            lines.append("")

        if opt.allow:
            allow_checks = " && ".join(f'"${{{var}}}" != "{_shell_escape_dq(v)}"' for v in opt.allow)
            vals = ", ".join(opt.allow)
            lines.append(f'if [[ -n "${{{var}}}" ]] && [[ {allow_checks} ]]; then')
            lines.append(f'  echo "error: {tool_name}: option {opt.flag} is not an allowed value" >&2')
            lines.append(f'  echo "  value:   \\"${{{var}}}\\"" >&2')
            lines.append(f'  echo "  allowed: {_shell_escape_dq(vals)}" >&2')
            lines.append('  echo "  hint: use one of the allowed values" >&2')
            lines.append("  exit 1")
            lines.append("fi")
            lines.append("")

        if opt.deny:
            for denied in opt.deny:
                escaped = _shell_escape_dq(denied)
                lines.append(f'if [[ "${{{var}}}" == "{escaped}" ]]; then')
                lines.append(f'  echo "error: {tool_name}: option {opt.flag} is not allowed" >&2')
                lines.append(f'  echo "  value:  \\"{escaped}\\"" >&2')
                lines.append(f'  echo "  denied: {_shell_escape_dq(", ".join(opt.deny))}" >&2')
                lines.append('  echo "  hint: use a different value" >&2')
                lines.append("  exit 1")
                lines.append("fi")
            lines.append("")

    return "\n".join(lines).rstrip()


def _arg_validations(tool_name: str, arguments: dict[str, ArgSpec]) -> str:
    lines: list[str] = []
    for name, spec in arguments.items():
        var = _var_name(name)

        if spec.variadic:
            if not spec.allow_flags:
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                lines.append('  if [[ "$_v" == -* ]]; then')
                lines.append(f"    echo \"error: {tool_name}: <{name}> values cannot start with '-'\" >&2")
                lines.append('    echo "  hint: use -- before positional arguments if needed" >&2')
                lines.append("    exit 1")
                lines.append("  fi")
                lines.append("done")
                lines.append("")
            if spec.required:
                lines.append(f"if [[ ${{#{var}[@]}} -eq 0 ]]; then")
                lines.append(f'  echo "error: {tool_name}: missing required argument <{name}>" >&2')
                lines.append('  echo "  hint: provide at least one value" >&2')
                lines.append("  usage")
                lines.append("fi")
                lines.append("")
            if spec.pattern:
                anchored = _anchored_pattern(spec.pattern)
                lines.append(f"_NERF_PATTERN='{_shell_escape_sq(anchored)}'")
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                lines.append('  if ! [[ "$_v" =~ $_NERF_PATTERN ]]; then')
                lines.append(f'    echo "error: {tool_name}: argument <{name}> does not match required pattern" >&2')
                lines.append('    echo "  value:   \\"$_v\\"" >&2')
                lines.append(f'    echo "  pattern: {spec.pattern}" >&2')
                lines.append(f'    echo "  hint: value must match {spec.pattern}" >&2')
                lines.append("    exit 1")
                lines.append("  fi")
                lines.append("done")
                lines.append("")
            if spec.allow:
                allow_checks = " && ".join(f'"$_v" != "{_shell_escape_dq(v)}"' for v in spec.allow)
                vals = ", ".join(spec.allow)
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                lines.append(f"  if [[ {allow_checks} ]]; then")
                lines.append(f'    echo "error: {tool_name}: argument <{name}> is not an allowed value" >&2')
                lines.append('    echo "  value:   \\"$_v\\"" >&2')
                lines.append(f'    echo "  allowed: {_shell_escape_dq(vals)}" >&2')
                lines.append('    echo "  hint: use one of the allowed values" >&2')
                lines.append("    exit 1")
                lines.append("  fi")
                lines.append("done")
                lines.append("")
            if spec.deny:
                lines.append(f'for _v in "${{{var}[@]}}"; do')
                for denied in spec.deny:
                    escaped = _shell_escape_dq(denied)
                    lines.append(f'  if [[ "$_v" == "{escaped}" ]]; then')
                    lines.append(f'    echo "error: {tool_name}: argument <{name}> is not allowed" >&2')
                    lines.append(f'    echo "  value:  \\"{escaped}\\"" >&2')
                    lines.append(f'    echo "  denied: {_shell_escape_dq(", ".join(spec.deny))}" >&2')
                    lines.append('    echo "  hint: use a different value" >&2')
                    lines.append("    exit 1")
                    lines.append("  fi")
                lines.append("done")
                lines.append("")
        else:
            lines.append(f'if [[ -n "${{{var}}}" ]] && [[ "${{{var}}}" == -* ]]; then')
            lines.append(f"  echo \"error: {tool_name}: <{name}> cannot start with '-'\" >&2")
            lines.append('  echo "  hint: use -- before positional arguments if needed" >&2')
            lines.append("  exit 1")
            lines.append("fi")
            lines.append("")
            if spec.required:
                lines.append(f'if [[ -z "${{{var}}}" ]]; then')
                lines.append(f'  echo "error: {tool_name}: missing required argument <{name}>" >&2')
                lines.append(f'  echo "  hint: provide a value for <{name}>" >&2')
                lines.append("  usage")
                lines.append("fi")
                lines.append("")
            if spec.pattern:
                anchored = _anchored_pattern(spec.pattern)
                lines.append(f"_NERF_PATTERN='{_shell_escape_sq(anchored)}'")
                lines.append(f'if [[ -n "${{{var}}}" ]] && ! [[ "${{{var}}}" =~ $_NERF_PATTERN ]]; then')
                lines.append(f'  echo "error: {tool_name}: argument <{name}> does not match required pattern" >&2')
                lines.append(f'  echo "  value:   \\"${{{var}}}\\"" >&2')
                lines.append(f'  echo "  pattern: {spec.pattern}" >&2')
                lines.append(f'  echo "  hint: value must match {spec.pattern}" >&2')
                lines.append("  exit 1")
                lines.append("fi")
                lines.append("")
            if spec.allow:
                allow_checks = " && ".join(f'"${{{var}}}" != "{_shell_escape_dq(v)}"' for v in spec.allow)
                vals = ", ".join(spec.allow)
                lines.append(f'if [[ -n "${{{var}}}" ]] && [[ {allow_checks} ]]; then')
                lines.append(f'  echo "error: {tool_name}: argument <{name}> is not an allowed value" >&2')
                lines.append(f'  echo "  value:   \\"${{{var}}}\\"" >&2')
                lines.append(f'  echo "  allowed: {_shell_escape_dq(vals)}" >&2')
                lines.append('  echo "  hint: use one of the allowed values" >&2')
                lines.append("  exit 1")
                lines.append("fi")
                lines.append("")
            if spec.deny:
                for denied in spec.deny:
                    escaped = _shell_escape_dq(denied)
                    lines.append(f'if [[ "${{{var}}}" == "{escaped}" ]]; then')
                    lines.append(f'  echo "error: {tool_name}: argument <{name}> is not allowed" >&2')
                    lines.append(f'  echo "  value:  \\"{escaped}\\"" >&2')
                    lines.append(f'  echo "  denied: {_shell_escape_dq(", ".join(spec.deny))}" >&2')
                    lines.append('  echo "  hint: use a different value" >&2')
                    lines.append("  exit 1")
                    lines.append("fi")
                lines.append("")

    return "\n".join(lines).rstrip()


# -- Environment ---------------------------------------------------------------


def _env_exports(env: dict[str, str]) -> str:
    lines = []
    for k, v in env.items():
        lines.append(f"export {k}='{_shell_escape_sq(v)}'")
    return "\n".join(lines)


# -- Guard checks --------------------------------------------------------------


def _guard_checks(tool_name: str, tool_spec: ToolSpec) -> str:
    lines: list[str] = []
    for guard in tool_spec.guards:
        safe_msg = guard.fail_message.replace("'", "'\"'\"'")

        if guard.command is not None:
            cmd_args = _substitute_template_command(guard.command, tool_spec)
            check = " ".join(cmd_args) + " > /dev/null 2>&1"
            lines.append(f"{check} || {{ echo 'error: {tool_name}: {safe_msg}' >&2; exit 1; }}")
        else:
            script_text = _substitute_script(guard.script or "", tool_spec)
            script_lines = script_text.strip().splitlines()
            if len(script_lines) == 1:
                lines.append(f"( {script_lines[0]} ) || {{ echo 'error: {tool_name}: {safe_msg}' >&2; exit 1; }}")
            else:
                lines.append("(")
                for sl in script_lines:
                    lines.append(f"  {sl}")
                lines.append(f") || {{ echo 'error: {tool_name}: {safe_msg}' >&2; exit 1; }}")

    return "\n".join(lines)


# -- Pre-hook ------------------------------------------------------------------


def _pre_hook(tool_name: str, tool_spec: ToolSpec) -> str:
    pre_body = _substitute_script(tool_spec.pre or "", tool_spec)
    lines = [
        "_nerf_pre() {",
    ]
    for line in pre_body.strip().splitlines():
        lines.append(f"  {line}")
    lines.append("}")
    lines.append("")
    lines.append("_nerf_pre_rc=0")
    lines.append("_nerf_pre || _nerf_pre_rc=$?")
    lines.append("if [ $_nerf_pre_rc -ne 0 ]; then")
    lines.append(f'  echo "error: {tool_name}: pre-hook failed (exit code $_nerf_pre_rc)" >&2')
    lines.append("  exit $_nerf_pre_rc")
    lines.append("fi")
    return "\n".join(lines)


# -- Execution modes -----------------------------------------------------------


def _dry_run_check(tool_name: str, tool_spec: ToolSpec) -> str:
    """Generate the --nerf-dry-run output block.

    Only called for template and script modes. Passthrough mode handles
    dry-run inline in _passthrough_exec (after the deny scan).
    """
    lines = ['if [[ "$_NERF_DRY_RUN" == "true" ]]; then']

    if tool_spec.template is not None:
        exec_args = _substitute_template_command(tool_spec.template.command, tool_spec)
        if tool_spec.template.npm_pkgrun:
            cmd = 'echo "dry-run: $_PKGRUN ' + " ".join(exec_args) + '"'
        else:
            cmd = 'echo "dry-run: ' + " ".join(exec_args) + '"'
        lines.append(f"  {cmd}")
    else:
        lines.append(f'  echo "dry-run: {tool_name} would run inline script"')

    lines.append("  exit 0")
    lines.append("fi")
    return "\n".join(lines)


def _template_exec(tool_spec: ToolSpec) -> str:
    """Generate the exec line for template mode."""
    assert tool_spec.template is not None
    args = _substitute_template_command(tool_spec.template.command, tool_spec)
    if tool_spec.template.npm_pkgrun:
        return 'exec "$_PKGRUN" ' + " ".join(args)
    return "exec " + " ".join(args)


def _passthrough_exec(tool_name: str, tool_spec: ToolSpec) -> str:
    """Generate the deny scan and exec for passthrough mode."""
    assert tool_spec.passthrough is not None
    pt = tool_spec.passthrough
    lines: list[str] = []

    if pt.deny:
        deny_items = " ".join(f"'{_shell_escape_sq(d)}'" for d in pt.deny)
        lines.append(f"_NERF_DENY_PATTERNS=({deny_items})")
        lines.append("")
        lines.append('for _tok in "$@"; do')
        lines.append('  for _pat in "${_NERF_DENY_PATTERNS[@]}"; do')
        lines.append('    case "$_tok" in')
        lines.append("      $_pat)")
        lines.append(
            f'        echo "error: {tool_name}:'
            " token '$_tok' is not allowed"
            ' (matched deny pattern \'$_pat\')" >&2'
        )
        lines.append('        echo "  denied patterns: ${_NERF_DENY_PATTERNS[*]}" >&2')
        lines.append('        echo "  hint: remove \'$_tok\' and retry" >&2')
        lines.append("        exit 1")
        lines.append("        ;;")
        lines.append("    esac")
        lines.append("  done")
        lines.append("done")

    exec_parts = [pt.command]
    exec_parts.extend(f"'{_shell_escape_sq(p)}'" for p in pt.prefix)
    exec_parts.append('"$@"')
    exec_parts.extend(f"'{_shell_escape_sq(s)}'" for s in pt.suffix)
    exec_str = " ".join(exec_parts)

    if lines:
        lines.append("")

    # Dry-run check after deny scan but before exec
    lines.append('if [[ "$_NERF_DRY_RUN" == "true" ]]; then')
    lines.append(f'  echo "dry-run: {exec_str}"')
    lines.append("  exit 0")
    lines.append("fi")
    lines.append("")
    lines.append("exec " + exec_str)

    return "\n".join(lines)


# -- Substitution helpers ------------------------------------------------------


def _substitute_template_command(
    command: tuple[str, ...],
    tool: ToolSpec,
) -> list[str]:
    """Substitute {{param}} placeholders in a command word list.

    Tokens that are exactly a placeholder get type-aware expansion (conditional
    flags, array expansion, etc.). Tokens with inline placeholders (e.g. a URL
    like "repos/{owner}/{repo}/pulls/{{arguments.pr}}/comments") get simple
    variable substitution within a double-quoted string.
    """
    result: list[str] = []
    for part in command:
        m = PLACEHOLDER_RE.fullmatch(part)
        if m:
            # Whole-token placeholder: type-aware expansion
            ref = m.group(1)
            resolved = resolve_placeholder(ref, tool)
            if resolved is None:
                result.append(part)
                continue
            kind, name = resolved
            var = _var_name(name)

            if kind == "switches":
                sw = tool.switches[name]
                if sw.repeatable:
                    result.append(
                        "$(for _ in $(seq 1 $" + var + " 2>/dev/null); do"
                        f" echo -n '{_shell_escape_sq(sw.flag)} '; done)"
                    )
                else:
                    result.append("${" + var + ':+"' + sw.flag + '"' + "}")

            elif kind == "options":
                opt = tool.options[name]
                if opt.repeatable:
                    result.append("${" + var + '[@]+"${' + var + '[@]}"}')
                elif opt.required:
                    result.append(f'"${{{var}}}"')
                else:
                    result.append("${" + var + ':+"' + opt.flag + '"}')
                    result.append("${" + var + ':+"$' + var + '"}')

            elif kind == "arguments":
                spec = tool.arguments[name]
                if spec.variadic:
                    if spec.required:
                        result.append(f'"${{{var}[@]}}"')
                    else:
                        result.append("${" + var + '[@]+"${' + var + '[@]}"}')
                else:
                    if spec.required:
                        result.append(f'"${{{var}}}"')
                    else:
                        result.append("${" + var + ':+"$' + var + '"}')

        elif PLACEHOLDER_RE.search(part):
            # Inline placeholder: simple variable substitution in a quoted string
            def _inline_replace(match: re.Match) -> str:  # type: ignore[type-arg]
                ref: str = match.group(1)
                resolved = resolve_placeholder(ref, tool)
                if resolved is None:
                    return str(match.group(0))
                _kind, name = resolved
                return "${" + _var_name(name) + "}"
            result.append('"' + PLACEHOLDER_RE.sub(_inline_replace, part) + '"')

        else:
            result.append(part)
    return result


def _substitute_script(script: str, tool: ToolSpec) -> str:
    """Substitute {{param}} placeholders inline within a bash script string."""

    def replace(m: re.Match) -> str:  # type: ignore[type-arg]
        ref: str = m.group(1)
        resolved = resolve_placeholder(ref, tool)
        if resolved is None:
            return str(m.group(0))
        _kind, name = resolved
        return "${" + _var_name(name) + "}"

    return PLACEHOLDER_RE.sub(replace, script)


def _npm_pkgrun_resolver() -> str:
    """Generate a preamble that resolves the best npm package runner."""
    return (
        "# Resolve npm package runner\n"
        '_PKGRUN=""\n'
        "for _candidate in bunx pnpx npx; do\n"
        '  if command -v "$_candidate" > /dev/null 2>&1; then\n'
        '    _PKGRUN="$_candidate"\n'
        "    break\n"
        "  fi\n"
        "done\n"
        'if [[ -z "$_PKGRUN" ]]; then\n'
        '  echo "error: no npm package runner found (tried bunx, pnpx, npx)" >&2\n'
        "  exit 1\n"
        "fi"
    )


# -- Helpers -------------------------------------------------------------------


def _var_name(param_name: str) -> str:
    return param_name.upper().replace("-", "_")


def _anchored_pattern(pattern: str) -> str:
    """Ensure a regex pattern is anchored for full-match in bash =~."""
    if not pattern.startswith("^"):
        pattern = "^" + pattern
    if not pattern.endswith("$"):
        pattern = pattern + "$"
    return pattern


def _shell_escape_dq(value: str) -> str:
    """Escape a string for embedding in double-quoted bash strings."""
    return (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _shell_escape_sq(value: str) -> str:
    """Escape a string for embedding in single-quoted bash strings."""
    return value.replace("'", "'\"'\"'")
