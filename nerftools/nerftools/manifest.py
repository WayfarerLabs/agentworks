"""Nerf manifest loading and validation.

A nerf manifest is a YAML file that defines a family of scoped tool wrappers.
It is the single source of truth for tool definitions, parameter specs, and
rulesync skill metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class ManifestError(Exception):
    """Raised when a manifest is invalid."""


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class PackageMeta:
    name: str
    description: str
    skill_group: str
    skill_intro: str = ""


@dataclass(frozen=True)
class FlagSpec:
    """Specification for a named flag argument (e.g. --remote <value>).

    The flag string is auto-derived from the param name if not set explicitly
    (e.g. name 'remote' -> '--remote'). Flags are required by default; set
    optional=True to make them optional.
    """

    flag: str
    description: str
    optional: bool = False
    short: str | None = None
    pattern: str | None = None
    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required(self) -> bool:
        return not self.optional


@dataclass(frozen=True)
class ArgSpec:
    """Specification for a positional argument.

    Positional args are optional by default. Set required=True to require them.
    Set variadic=True to collect all remaining arguments into a bash array;
    variadic must be the last arg and is mutually exclusive with other args
    that come after it.
    """

    description: str
    required: bool = False
    variadic: bool = False
    pattern: str | None = None
    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GuardSpec:
    """A pre-flight check run before the main command.

    The guard command is run with the same param substitution as the main
    command. If it exits non-zero, the script prints fail_message and exits.
    """

    command: tuple[str, ...]
    fail_message: str


@dataclass(frozen=True)
class ToolSpec:
    description: str
    command: tuple[str, ...]
    flags: dict[str, FlagSpec] = field(default_factory=dict)
    args: dict[str, ArgSpec] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    guards: tuple[GuardSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NerfManifest:
    package: PackageMeta
    tools: dict[str, ToolSpec]
    source_path: Path | None = None


# -- Loading -------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def load_manifest(path: Path) -> NerfManifest:
    """Load and validate a nerf manifest from a YAML file."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: YAML parse error: {e}") from e

    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: manifest must be a YAML mapping")

    package = _load_package(raw, path)
    tools = _load_tools(raw, path)

    return NerfManifest(package=package, tools=tools, source_path=path)


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
    command_raw = raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ManifestError(f"{ctx}: 'command' must be a non-empty list")
    command = tuple(str(c) for c in command_raw)

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ManifestError(f"{ctx}: 'env' must be a mapping")
    env = {str(k): str(v) for k, v in env_raw.items()}

    flags_raw = raw.get("flags", {})
    if not isinstance(flags_raw, dict):
        raise ManifestError(f"{ctx}: 'flags' must be a mapping")
    flags = {k: _load_flag(v, path, tool_name, k) for k, v in flags_raw.items()}

    args_raw = raw.get("args", {})
    if not isinstance(args_raw, dict):
        raise ManifestError(f"{ctx}: 'args' must be a mapping")
    args = {k: _load_arg(v, path, tool_name, k) for k, v in args_raw.items()}

    guards_raw = raw.get("guards", [])
    if not isinstance(guards_raw, list):
        raise ManifestError(f"{ctx}: 'guards' must be a list")
    guards = tuple(_load_guard(g, path, tool_name, i) for i, g in enumerate(guards_raw))

    _validate_tool(command, guards, flags, args, ctx)

    return ToolSpec(
        description=description,
        command=command,
        flags=flags,
        args=args,
        env=env,
        guards=guards,
    )


def _load_flag(raw: Any, path: Path, tool_name: str, flag_name: str) -> FlagSpec:
    ctx = f"{path}:tools.{tool_name}.flags.{flag_name}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    # Auto-derive --flag-name from the dict key if not specified explicitly
    flag = str(raw["flag"]) if "flag" in raw else f"--{flag_name.replace('_', '-')}"
    description = _require_str(raw, "description", ctx)
    optional = bool(raw.get("optional", False))
    short = str(raw["short"]) if "short" in raw else None
    pattern = str(raw["pattern"]) if "pattern" in raw else None
    allow = tuple(str(v) for v in raw.get("allow", []))
    deny = tuple(str(v) for v in raw.get("deny", []))

    if short is not None and not re.fullmatch(r"-[a-zA-Z]", short):
        raise ManifestError(f"{ctx}: 'short' must be a single-character flag like -r, got {short!r}")
    if allow and deny:
        raise ManifestError(f"{ctx}: 'allow' and 'deny' cannot both be set")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ManifestError(f"{ctx}: invalid 'pattern' regex: {e}") from e

    return FlagSpec(
        flag=flag,
        description=description,
        optional=optional,
        short=short,
        pattern=pattern,
        allow=allow,
        deny=deny,
    )


def _load_arg(raw: Any, path: Path, tool_name: str, arg_name: str) -> ArgSpec:
    ctx = f"{path}:tools.{tool_name}.args.{arg_name}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    description = _require_str(raw, "description", ctx)
    required = bool(raw.get("required", False))
    variadic = bool(raw.get("variadic", False))
    pattern = str(raw["pattern"]) if "pattern" in raw else None
    allow = tuple(str(v) for v in raw.get("allow", []))
    deny = tuple(str(v) for v in raw.get("deny", []))

    if allow and deny:
        raise ManifestError(f"{ctx}: 'allow' and 'deny' cannot both be set")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ManifestError(f"{ctx}: invalid 'pattern' regex: {e}") from e

    return ArgSpec(
        description=description,
        required=required,
        variadic=variadic,
        pattern=pattern,
        allow=allow,
        deny=deny,
    )


def _load_guard(raw: Any, path: Path, tool_name: str, index: int) -> GuardSpec:
    ctx = f"{path}:tools.{tool_name}.guards[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")
    command_raw = raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ManifestError(f"{ctx}: 'command' must be a non-empty list")
    fail_message = _require_str(raw, "fail_message", ctx)
    return GuardSpec(command=tuple(str(c) for c in command_raw), fail_message=fail_message)


def _validate_tool(
    command: tuple[str, ...],
    guards: tuple[GuardSpec, ...],
    flags: dict[str, FlagSpec],
    args: dict[str, ArgSpec],
    ctx: str,
) -> None:
    # Check for name collision between flags and args
    overlap = set(flags.keys()) & set(args.keys())
    if overlap:
        raise ManifestError(
            f"{ctx}: names defined in both flags and args: {', '.join(sorted(overlap))}"
        )

    all_params = set(flags.keys()) | set(args.keys())

    # All {param} in command must be defined
    referenced: set[str] = set()
    for part in command:
        for match in _PLACEHOLDER_RE.finditer(part):
            referenced.add(match.group(1))

    for name in referenced:
        if name not in all_params:
            raise ManifestError(
                f"{ctx}: command references '{{{name}}}' but '{name}' is not defined in flags or args"
            )

    # All flags and args must be referenced in command
    for name in all_params:
        if not any(f"{{{name}}}" in part for part in command):
            raise ManifestError(f"{ctx}: '{name}' is defined but not referenced in command")

    # Variadic arg must be last
    arg_names = list(args.keys())
    for name in arg_names[:-1]:
        if args[name].variadic:
            raise ManifestError(f"{ctx}: arg '{name}' is variadic but is not the last arg")

    # Guard command placeholders must reference defined params
    for i, guard in enumerate(guards):
        for part in guard.command:
            for match in _PLACEHOLDER_RE.finditer(part):
                name = match.group(1)
                if name not in all_params:
                    raise ManifestError(
                        f"{ctx}: guards[{i}] references '{{{name}}}' but '{name}' is not defined in flags or args"
                    )


# -- Merging -------------------------------------------------------------------


def merge_manifests(manifests: list[NerfManifest]) -> list[NerfManifest]:
    """Merge manifests, with later entries winning on tool name collision.

    Tools within the same package are merged; a tool from a later manifest
    replaces the same-named tool from an earlier manifest.
    """
    packages: dict[str, PackageMeta] = {}
    tools_by_package: dict[str, dict[str, ToolSpec]] = {}
    source_by_package: dict[str, Path | None] = {}

    for manifest in manifests:
        pkg_name = manifest.package.name
        if pkg_name not in packages:
            packages[pkg_name] = manifest.package
            tools_by_package[pkg_name] = {}
            source_by_package[pkg_name] = manifest.source_path
        tools_by_package[pkg_name].update(manifest.tools)

    return [
        NerfManifest(
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
