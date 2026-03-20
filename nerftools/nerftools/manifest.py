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
class ParamSpec:
    description: str
    flag: str | None = None
    positional: bool = False
    required: bool = False
    pattern: str | None = None
    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)
    default: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    description: str
    command: tuple[str, ...]
    params: dict[str, ParamSpec] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    example: str | None = None


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

    example = str(raw["example"]) if "example" in raw else None

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ManifestError(f"{ctx}: 'env' must be a mapping")
    env = {str(k): str(v) for k, v in env_raw.items()}

    params_raw = raw.get("params", {})
    if not isinstance(params_raw, dict):
        raise ManifestError(f"{ctx}: 'params' must be a mapping")
    params = {k: _load_param(v, path, tool_name, k) for k, v in params_raw.items()}

    _validate_tool(command, params, ctx)

    return ToolSpec(
        description=description,
        command=command,
        params=params,
        env=env,
        example=example,
    )


def _load_param(raw: Any, path: Path, tool_name: str, param_name: str) -> ParamSpec:
    ctx = f"{path}:tools.{tool_name}.params.{param_name}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: must be a mapping")

    flag = str(raw["flag"]) if "flag" in raw else None
    positional = bool(raw.get("positional", False))
    description = _require_str(raw, "description", ctx)
    required = bool(raw.get("required", False))
    pattern = str(raw["pattern"]) if "pattern" in raw else None
    allow = tuple(str(v) for v in raw.get("allow", []))
    deny = tuple(str(v) for v in raw.get("deny", []))
    default = str(raw["default"]) if "default" in raw else None

    if flag and positional:
        raise ManifestError(f"{ctx}: 'flag' and 'positional' are mutually exclusive")
    if not flag and not positional:
        raise ManifestError(f"{ctx}: one of 'flag' or 'positional' must be set")
    if allow and deny:
        raise ManifestError(f"{ctx}: 'allow' and 'deny' cannot both be set")
    if default is not None and required:
        raise ManifestError(f"{ctx}: 'default' cannot be set when 'required' is true")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ManifestError(f"{ctx}: invalid 'pattern' regex: {e}") from e

    return ParamSpec(
        flag=flag,
        positional=positional,
        description=description,
        required=required,
        pattern=pattern,
        allow=allow,
        deny=deny,
        default=default,
    )


def _validate_tool(command: tuple[str, ...], params: dict[str, ParamSpec], ctx: str) -> None:
    referenced: set[str] = set()
    for part in command:
        for match in _PLACEHOLDER_RE.finditer(part):
            referenced.add(match.group(1))

    for name in referenced:
        if name not in params:
            raise ManifestError(f"{ctx}: command references '{{{{ {name} }}}}' but no param '{name}' is defined")

    for name in params:
        if not any(f"{{{name}}}" in part for part in command):
            raise ManifestError(f"{ctx}: param '{name}' is defined but not referenced in command")


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
