"""Nerf manifest loading and validation (v1).

A nerf manifest is a YAML file that defines a package of scoped tool wrappers.
It is the single source of truth for tool definitions, parameter specs, safety
guardrails, threat metadata, and AI skill metadata.

Version 1 introduces three execution modes (template, passthrough, script),
a 2D threat model (read/write), and refined parameter types (switches, options,
arguments).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class ManifestError(Exception):
    """Raised when a manifest is invalid."""


# -- Enums ---------------------------------------------------------------------


class ThreatLevel(Enum):
    """Ordered threat scope from narrow to broad."""

    NONE = "none"
    WORKSPACE = "workspace"
    MACHINE = "machine"
    REMOTE = "remote"
    ADMIN = "admin"

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ThreatLevel):
            return NotImplemented
        return _THREAT_ORDER[self] <= _THREAT_ORDER[other]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ThreatLevel):
            return NotImplemented
        return _THREAT_ORDER[self] < _THREAT_ORDER[other]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ThreatLevel):
            return NotImplemented
        return _THREAT_ORDER[self] >= _THREAT_ORDER[other]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ThreatLevel):
            return NotImplemented
        return _THREAT_ORDER[self] > _THREAT_ORDER[other]


_THREAT_ORDER: dict[ThreatLevel, int] = {member: i for i, member in enumerate(ThreatLevel)}

THREAT_LEVEL_NAMES = tuple(t.value for t in ThreatLevel)


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class PackageMeta:
    name: str
    description: str
    skill_group: str
    skill_intro: str = ""


@dataclass(frozen=True)
class ThreatSpec:
    """Two-dimensional threat profile: what a tool reads and writes."""

    read: ThreatLevel
    write: ThreatLevel


@dataclass(frozen=True)
class SwitchSpec:
    """A boolean flag, present or absent, with no value. Always optional.

    When repeatable=True, the switch can be passed multiple times and the
    generated script stores the count (e.g. -v -v -> VERBOSE=2).
    """

    description: str
    flag: str  # e.g. --verbose
    short: str | None = None
    repeatable: bool = False


@dataclass(frozen=True)
class OptionSpec:
    """A named flag that takes exactly one value (option flag + option value).

    When repeatable=True, the option can be passed multiple times and the
    generated script accumulates flag-value pairs in an array so
    "${VAR[@]}" expands to --flag val1 --flag val2.
    """

    description: str
    flag: str  # e.g. --remote
    short: str | None = None
    required: bool = False
    repeatable: bool = False
    pattern: str | None = None
    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ArgSpec:
    """A positional argument identified by position, not by a flag.

    For variadic arguments, allow_flags controls whether flag-like tokens
    (starting with -) are accepted. Default is False (rejected) for safety.
    Set allow_flags=True when forwarding to a tool that has its own flags
    (e.g. pytest, ruff).
    """

    description: str
    required: bool = False
    variadic: bool = False
    allow_flags: bool = False
    pattern: str | None = None
    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TemplateSpec:
    """Build a command from an explicit template with {{param}} placeholders."""

    command: tuple[str, ...]
    npm_pkgrun: bool = False


@dataclass(frozen=True)
class PassthroughSpec:
    """Forward all tokens after deny-list scan."""

    command: str
    deny: tuple[str, ...] = field(default_factory=tuple)
    prefix: tuple[str, ...] = field(default_factory=tuple)
    suffix: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GuardSpec:
    """A pre-flight check run before the main command.

    Exactly one of command or script must be set:
      - command: a list of args run as a subprocess (output suppressed).
      - script: an inline bash snippet (single or multi-line).

    The check passes when it exits zero. Exits non-zero causes the wrapper
    script to print fail_message and exit 1.

    {{param}} placeholders in command parts and script text are substituted
    with the tool's parameter values.
    """

    fail_message: str
    command: tuple[str, ...] | None = None
    script: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    description: str
    threat: ThreatSpec

    # Execution mode (exactly one set):
    template: TemplateSpec | None = None
    passthrough: PassthroughSpec | None = None
    script: str | None = None

    # Parameters (template + script only):
    switches: dict[str, SwitchSpec] = field(default_factory=dict)
    options: dict[str, OptionSpec] = field(default_factory=dict)
    arguments: dict[str, ArgSpec] = field(default_factory=dict)

    # Lifecycle:
    pre: str | None = None
    guards: tuple[GuardSpec, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        """Return the active execution mode name."""
        if self.template is not None:
            return "template"
        if self.passthrough is not None:
            return "passthrough"
        return "script"


@dataclass(frozen=True)
class NerfManifest:
    version: int
    package: PackageMeta
    tools: dict[str, ToolSpec]
    source_path: Path | None = None


# -- Loading -------------------------------------------------------------------

# Matches {{name}} or {{kind.name}} where kind is switches/options/arguments
PLACEHOLDER_RE = re.compile(r"\{\{(\w+(?:\.\w+)?)\}\}")

_VALID_PLACEHOLDER_KINDS = {"switches", "options", "arguments"}


def load_manifest(path: Path) -> NerfManifest:
    """Load and validate a nerf manifest from a YAML file."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: YAML parse error: {e}") from e

    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: manifest must be a YAML mapping")

    version = raw.get("version")
    if version is None:
        raise ManifestError(f"{path}: 'version' is required")
    if not isinstance(version, int) or version != 1:
        raise ManifestError(f"{path}: unsupported manifest version: {version} (expected 1)")

    package = _load_package(raw, path)
    tools = _load_tools(raw, path)

    return NerfManifest(version=version, package=package, tools=tools, source_path=path)


def _load_package(raw: dict[str, Any], path: Path) -> PackageMeta:
    pkg = raw.get("package")
    if not isinstance(pkg, dict):
        raise ManifestError(f"{path}: 'package' section is required")
    ctx = f"{path}:package"

    name = _require_str(pkg, "name", ctx)
    description = _require_str(pkg, "description", ctx)
    skill_group = _require_str(pkg, "skill_group", ctx)
    skill_intro = str(pkg.get("skill_intro", "")).strip()

    return PackageMeta(
        name=name,
        description=description,
        skill_group=skill_group,
        skill_intro=skill_intro,
    )


def _load_tools(raw: dict[str, Any], path: Path) -> dict[str, ToolSpec]:
    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, dict):
        raise ManifestError(f"{path}: 'tools' section is required")

    tools: dict[str, ToolSpec] = {}
    for tool_name, tool_raw in tools_raw.items():
        if not isinstance(tool_raw, dict):
            raise ManifestError(f"{path}:tools.{tool_name}: must be a mapping")
        tools[tool_name] = _load_tool(tool_raw, path, tool_name)

    return tools


def _load_tool(raw: dict[str, Any], path: Path, tool_name: str) -> ToolSpec:
    ctx = f"{path}:tools.{tool_name}"

    description = _require_str(raw, "description", ctx)
    threat = _load_threat(raw, path, tool_name)

    # Execution mode
    template = _load_template(raw, path, tool_name) if "template" in raw else None
    passthrough = _load_passthrough(raw, path, tool_name) if "passthrough" in raw else None
    script: str | None = None
    if "script" in raw:
        script = str(raw["script"]).strip()
        if not script:
            raise ManifestError(f"{ctx}: 'script' must not be empty")

    modes = sum(x is not None for x in (template, passthrough, script))
    if modes == 0:
        raise ManifestError(f"{ctx}: exactly one of 'template', 'passthrough', or 'script' is required")
    if modes > 1:
        raise ManifestError(f"{ctx}: only one of 'template', 'passthrough', or 'script' may be set")

    # Parameters
    switches = _load_switches(raw, path, tool_name)
    options = _load_options(raw, path, tool_name)
    arguments = _load_arguments(raw, path, tool_name)

    if passthrough is not None and (switches or options or arguments):
        raise ManifestError(f"{ctx}: switches/options/arguments are not allowed in passthrough mode")

    # Lifecycle
    pre = str(raw["pre"]).strip() if "pre" in raw else None
    guards = _load_guards(raw, path, tool_name)
    env = _load_env(raw, path, tool_name)

    tool = ToolSpec(
        description=description,
        threat=threat,
        template=template,
        passthrough=passthrough,
        script=script,
        switches=switches,
        options=options,
        arguments=arguments,
        pre=pre,
        guards=guards,
        env=env,
    )

    _validate_tool(tool, ctx)
    return tool


def _load_threat(raw: dict[str, Any], path: Path, tool_name: str) -> ThreatSpec:
    ctx = f"{path}:tools.{tool_name}"
    threat_raw = raw.get("threat")
    if not isinstance(threat_raw, dict):
        raise ManifestError(f"{ctx}: 'threat' is required and must be a mapping")

    read_str = threat_raw.get("read")
    write_str = threat_raw.get("write")
    if read_str is None:
        raise ManifestError(f"{ctx}.threat: 'read' is required")
    if write_str is None:
        raise ManifestError(f"{ctx}.threat: 'write' is required")

    valid = ", ".join(THREAT_LEVEL_NAMES)
    try:
        read = ThreatLevel(str(read_str))
    except ValueError:
        raise ManifestError(f"{ctx}.threat: invalid read level '{read_str}' (expected one of {valid})") from None
    try:
        write = ThreatLevel(str(write_str))
    except ValueError:
        raise ManifestError(f"{ctx}.threat: invalid write level '{write_str}' (expected one of {valid})") from None

    return ThreatSpec(read=read, write=write)


def _load_template(raw: dict[str, Any], path: Path, tool_name: str) -> TemplateSpec:
    ctx = f"{path}:tools.{tool_name}.template"
    tmpl_raw = raw["template"]
    if not isinstance(tmpl_raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    command_raw = tmpl_raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ManifestError(f"{ctx}: 'command' must be a non-empty list")
    command = tuple(str(c) for c in command_raw)
    npm_pkgrun = bool(tmpl_raw.get("npm_pkgrun", False))

    return TemplateSpec(command=command, npm_pkgrun=npm_pkgrun)


def _load_passthrough(raw: dict[str, Any], path: Path, tool_name: str) -> PassthroughSpec:
    ctx = f"{path}:tools.{tool_name}.passthrough"
    pt_raw = raw["passthrough"]
    if not isinstance(pt_raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    command = _require_str(pt_raw, "command", ctx)
    for key in ("deny", "prefix", "suffix"):
        val = pt_raw.get(key)
        if val is not None and not isinstance(val, list):
            raise ManifestError(f"{ctx}: '{key}' must be a list, got {type(val).__name__}")
    deny = tuple(str(d) for d in pt_raw.get("deny", []))
    prefix = tuple(str(p) for p in pt_raw.get("prefix", []))
    suffix = tuple(str(s) for s in pt_raw.get("suffix", []))

    return PassthroughSpec(command=command, deny=deny, prefix=prefix, suffix=suffix)


def _load_switches(raw: dict[str, Any], path: Path, tool_name: str) -> dict[str, SwitchSpec]:
    switches_raw = raw.get("switches", {})
    if not isinstance(switches_raw, dict):
        raise ManifestError(f"{path}:tools.{tool_name}: 'switches' must be a mapping")

    switches: dict[str, SwitchSpec] = {}
    for name, spec_raw in switches_raw.items():
        ctx = f"{path}:tools.{tool_name}.switches.{name}"
        if not isinstance(spec_raw, dict):
            raise ManifestError(f"{ctx}: must be a mapping")

        description = _require_str(spec_raw, "description", ctx)
        flag = str(spec_raw["flag"]) if "flag" in spec_raw else f"--{name.replace('_', '-')}"
        short = str(spec_raw["short"]) if "short" in spec_raw else None
        repeatable = bool(spec_raw.get("repeatable", False))

        if not re.fullmatch(r"-{1,2}[a-zA-Z][a-zA-Z0-9-]*", flag):
            raise ManifestError(f"{ctx}: 'flag' must match -<name> or --<name> pattern, got {flag!r}")
        if short is not None and not re.fullmatch(r"-[a-zA-Z]", short):
            raise ManifestError(f"{ctx}: 'short' must be a single-character flag like -v, got {short!r}")

        switches[name] = SwitchSpec(description=description, flag=flag, short=short, repeatable=repeatable)

    return switches


def _load_options(raw: dict[str, Any], path: Path, tool_name: str) -> dict[str, OptionSpec]:
    options_raw = raw.get("options", {})
    if not isinstance(options_raw, dict):
        raise ManifestError(f"{path}:tools.{tool_name}: 'options' must be a mapping")

    options: dict[str, OptionSpec] = {}
    for name, spec_raw in options_raw.items():
        ctx = f"{path}:tools.{tool_name}.options.{name}"
        if not isinstance(spec_raw, dict):
            raise ManifestError(f"{ctx}: must be a mapping")

        description = _require_str(spec_raw, "description", ctx)
        flag = str(spec_raw["flag"]) if "flag" in spec_raw else f"--{name.replace('_', '-')}"
        short = str(spec_raw["short"]) if "short" in spec_raw else None
        required = bool(spec_raw.get("required", False))
        repeatable = bool(spec_raw.get("repeatable", False))
        pattern = str(spec_raw["pattern"]) if "pattern" in spec_raw else None
        for key in ("allow", "deny"):
            val = spec_raw.get(key)
            if val is not None and not isinstance(val, list):
                raise ManifestError(f"{ctx}: '{key}' must be a list, got {type(val).__name__}")
        allow = tuple(str(v) for v in spec_raw.get("allow", []))
        deny = tuple(str(v) for v in spec_raw.get("deny", []))

        if not re.fullmatch(r"-{1,2}[a-zA-Z][a-zA-Z0-9-]*", flag):
            raise ManifestError(f"{ctx}: 'flag' must match -<name> or --<name> pattern, got {flag!r}")
        if short is not None and not re.fullmatch(r"-[a-zA-Z]", short):
            raise ManifestError(f"{ctx}: 'short' must be a single-character flag like -r, got {short!r}")
        if allow and deny:
            raise ManifestError(f"{ctx}: 'allow' and 'deny' cannot both be set")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ManifestError(f"{ctx}: invalid 'pattern' regex: {e}") from e

        options[name] = OptionSpec(
            description=description, flag=flag, short=short,
            required=required, repeatable=repeatable,
            pattern=pattern, allow=allow, deny=deny,
        )

    return options


def _load_arguments(raw: dict[str, Any], path: Path, tool_name: str) -> dict[str, ArgSpec]:
    args_raw = raw.get("arguments", {})
    if not isinstance(args_raw, dict):
        raise ManifestError(f"{path}:tools.{tool_name}: 'arguments' must be a mapping")

    arguments: dict[str, ArgSpec] = {}
    for name, spec_raw in args_raw.items():
        ctx = f"{path}:tools.{tool_name}.arguments.{name}"
        if not isinstance(spec_raw, dict):
            raise ManifestError(f"{ctx}: must be a mapping")

        description = _require_str(spec_raw, "description", ctx)
        required = bool(spec_raw.get("required", False))
        variadic = bool(spec_raw.get("variadic", False))
        allow_flags = bool(spec_raw.get("allow_flags", False))
        pattern = str(spec_raw["pattern"]) if "pattern" in spec_raw else None
        for key in ("allow", "deny"):
            val = spec_raw.get(key)
            if val is not None and not isinstance(val, list):
                raise ManifestError(f"{ctx}: '{key}' must be a list, got {type(val).__name__}")
        allow = tuple(str(v) for v in spec_raw.get("allow", []))
        deny = tuple(str(v) for v in spec_raw.get("deny", []))

        if allow_flags and not variadic:
            raise ManifestError(f"{ctx}: 'allow_flags' is only valid on variadic arguments")
        if allow and deny:
            raise ManifestError(f"{ctx}: 'allow' and 'deny' cannot both be set")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ManifestError(f"{ctx}: invalid 'pattern' regex: {e}") from e

        arguments[name] = ArgSpec(
            description=description, required=required, variadic=variadic,
            allow_flags=allow_flags, pattern=pattern, allow=allow, deny=deny,
        )

    return arguments


def _load_guards(raw: dict[str, Any], path: Path, tool_name: str) -> tuple[GuardSpec, ...]:
    guards_raw = raw.get("guards", [])
    if not isinstance(guards_raw, list):
        raise ManifestError(f"{path}:tools.{tool_name}: 'guards' must be a list")
    return tuple(_load_guard(g, path, tool_name, i) for i, g in enumerate(guards_raw))


def _load_guard(raw: Any, path: Path, tool_name: str, index: int) -> GuardSpec:
    ctx = f"{path}:tools.{tool_name}.guards[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    fail_message = _require_str(raw, "fail_message", ctx)
    has_command = "command" in raw
    has_script = "script" in raw

    if has_command and has_script:
        raise ManifestError(f"{ctx}: 'command' and 'script' cannot both be set")
    if not has_command and not has_script:
        raise ManifestError(f"{ctx}: one of 'command' or 'script' is required")

    if has_command:
        command_raw = raw["command"]
        if not isinstance(command_raw, list) or not command_raw:
            raise ManifestError(f"{ctx}: 'command' must be a non-empty list")
        return GuardSpec(fail_message=fail_message, command=tuple(str(c) for c in command_raw))

    script = str(raw["script"]).strip()
    if not script:
        raise ManifestError(f"{ctx}: 'script' must not be empty")
    return GuardSpec(fail_message=fail_message, script=script)


def _load_env(raw: dict[str, Any], path: Path, tool_name: str) -> dict[str, str]:
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ManifestError(f"{path}:tools.{tool_name}: 'env' must be a mapping")
    env: dict[str, str] = {}
    for k, v in env_raw.items():
        key = str(k)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ManifestError(
                f"{path}:tools.{tool_name}.env: key '{key}' must match [A-Z_][A-Z0-9_]*"
            )
        env[key] = str(v)
    return env


# -- Validation ----------------------------------------------------------------


def resolve_placeholder(ref: str, tool: ToolSpec) -> tuple[str, str] | None:
    """Resolve a qualified placeholder reference to (kind, name).

    Placeholders must use {{kind.name}} syntax where kind is one of
    switches, options, or arguments. Returns None if the reference
    cannot be resolved.
    """
    if "." not in ref:
        return None
    kind, name = ref.split(".", 1)
    if kind not in _VALID_PLACEHOLDER_KINDS:
        return None
    if kind == "switches":
        return (kind, name) if name in tool.switches else None
    if kind == "options":
        return (kind, name) if name in tool.options else None
    if kind == "arguments":
        return (kind, name) if name in tool.arguments else None
    return None


def _validate_tool(tool: ToolSpec, ctx: str) -> None:
    """Cross-reference validation across fields."""
    sw_names = set(tool.switches.keys())
    opt_names = set(tool.options.keys())
    arg_names = set(tool.arguments.keys())
    all_params = sw_names | opt_names | arg_names

    for a, b, a_label, b_label in [
        (sw_names, opt_names, "switches", "options"),
        (sw_names, arg_names, "switches", "arguments"),
        (opt_names, arg_names, "options", "arguments"),
    ]:
        overlap = a & b
        if overlap:
            raise ManifestError(f"{ctx}: names overlap between {a_label} and {b_label}: {', '.join(sorted(overlap))}")

    if tool.template is not None:
        _validate_template_refs(tool, all_params, ctx)

    arg_names_list = list(tool.arguments.keys())
    for name in arg_names_list[:-1]:
        if tool.arguments[name].variadic:
            raise ManifestError(f"{ctx}: argument '{name}' is variadic but is not the last argument")

    # Guard and pre placeholder validation
    _validate_placeholder_refs(tool, tool.guards, ctx)


def _validate_placeholder_refs(tool: ToolSpec, guards: tuple[GuardSpec, ...], ctx: str) -> None:
    """Validate placeholders in guards and pre scripts resolve to defined params."""
    for i, guard in enumerate(guards):
        parts: list[str] = list(guard.command) if guard.command else [guard.script or ""]
        for part in parts:
            for match in PLACEHOLDER_RE.finditer(part):
                ref = match.group(1)
                if resolve_placeholder(ref, tool) is None:
                    raise ManifestError(
                        f"{ctx}: guards[{i}] references '{{{{{ref}}}}}' but it cannot be resolved"
                    )

    if tool.pre:
        for match in PLACEHOLDER_RE.finditer(tool.pre):
            ref = match.group(1)
            if resolve_placeholder(ref, tool) is None:
                raise ManifestError(
                    f"{ctx}: pre references '{{{{{ref}}}}}' but it cannot be resolved"
                )


def _validate_template_refs(tool: ToolSpec, all_params: set[str], ctx: str) -> None:
    """Validate that template command placeholders and params match."""
    assert tool.template is not None
    command = tool.template.command

    # All {{kind.name}} in command must resolve
    referenced_names: set[str] = set()
    for part in command:
        for match in PLACEHOLDER_RE.finditer(part):
            ref = match.group(1)
            resolved = resolve_placeholder(ref, tool)
            if resolved is None:
                raise ManifestError(
                    f"{ctx}: template command references '{{{{{ref}}}}}' but it cannot be resolved"
                )
            referenced_names.add(resolved[1])

    # All params must be referenced in command
    for name in all_params:
        if name not in referenced_names:
            raise ManifestError(f"{ctx}: '{name}' is defined but not referenced in template command")

    # Variadic arg placeholder must be last element in command
    arg_names = list(tool.arguments.keys())
    if arg_names:
        last_arg = arg_names[-1]
        if tool.arguments[last_arg].variadic:
            last_cmd = command[-1] if command else ""
            placeholder = "{{arguments." + last_arg + "}}"
            if last_cmd != placeholder:
                raise ManifestError(
                    f"{ctx}: variadic argument '{last_arg}' placeholder must be the last element in template command"
                )


# -- Merging -------------------------------------------------------------------


def merge_manifests(manifests: list[NerfManifest]) -> list[NerfManifest]:
    """Merge manifests, with later entries winning on tool name collision.

    Tools within the same package are merged; a tool from a later manifest
    replaces the same-named tool from an earlier manifest.
    """
    packages: dict[str, PackageMeta] = {}
    versions: dict[str, int] = {}
    tools_by_package: dict[str, dict[str, ToolSpec]] = {}
    source_by_package: dict[str, Path | None] = {}

    for manifest in manifests:
        pkg_name = manifest.package.name
        if pkg_name not in packages:
            packages[pkg_name] = manifest.package
            versions[pkg_name] = manifest.version
            tools_by_package[pkg_name] = {}
            source_by_package[pkg_name] = manifest.source_path
        tools_by_package[pkg_name].update(manifest.tools)

    return [
        NerfManifest(
            version=versions[pkg_name],
            package=packages[pkg_name],
            tools=tools_by_package[pkg_name],
            source_path=source_by_package[pkg_name],
        )
        for pkg_name in packages
    ]


# -- Helpers -------------------------------------------------------------------


def _require_str(data: dict[str, Any], key: str, ctx: str) -> str:
    if key not in data:
        raise ManifestError(f"{ctx}: '{key}' is required")
    return str(data[key])
