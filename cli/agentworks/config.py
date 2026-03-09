"""User configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "agentworks"
CONFIG_PATH = CONFIG_DIR / "config.toml"

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")


def validate_name(name: str) -> None:
    """Validate a resource name, raising typer.Exit(1) on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end with
    alphanumeric. No consecutive hyphens (reserved for agent username separator).
    """
    import typer

    if not NAME_RE.match(name) or "--" in name:
        typer.echo(
            f"Error: invalid name '{name}'. Names must be lowercase alphanumeric "
            "with hyphens or underscores, must start and end with a letter or digit, "
            "and cannot contain consecutive hyphens (--).",
            err=True,
        )
        raise typer.Exit(1)


# Valid values for enum-like fields
VALID_PLATFORMS = ("lima", "azure", "wsl2")
VALID_GIT_HOST_TYPES = ("azdo", "github")


class ConfigError(Exception):
    """Raised when configuration is invalid."""


# -- Data classes ----------------------------------------------------------


@dataclass(frozen=True)
class UserConfig:
    ssh_public_key: Path
    ssh_private_key: Path
    shell: str = "zsh"


@dataclass(frozen=True)
class PathsConfig:
    local_workspaces: Path = field(default_factory=lambda: Path.home() / "workspaces")
    code_workspaces: Path = field(default_factory=lambda: Path.home() / "agentworks-workspaces")


@dataclass(frozen=True)
class DefaultsConfig:
    platform: str | None = None
    vm_host: str | None = None
    git_hosts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DotfilesConfig:
    enabled: bool = True
    source: Path = field(default_factory=lambda: Path.home() / ".dotfiles")
    install_cmd: str = "./install.sh"


@dataclass(frozen=True)
class VMConfig:
    apt: list[str] = field(default_factory=list)
    snap: list[str] = field(default_factory=list)
    install_commands: list[str] = field(default_factory=list)
    cpus: int = 4
    memory: int = 8  # GiB
    disk: int = 50  # GiB
    azure_vm_size: str = "Standard_D4s_v5"
    vm_user: str = "agentworks"


@dataclass(frozen=True)
class WorkspaceTemplate:
    name: str
    inherits: list[str] = field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)


@dataclass(frozen=True)
class GitHostConfig:
    name: str
    type: str
    org: str | None = None


@dataclass(frozen=True)
class AzureConfig:
    subscription_id: str
    resource_group: str
    region: str
    idle_timeout_hours: int = 2


@dataclass(frozen=True)
class Config:
    user: UserConfig
    paths: PathsConfig
    defaults: DefaultsConfig
    dotfiles: DotfilesConfig
    vm: VMConfig
    workspace_templates: dict[str, WorkspaceTemplate]
    git_hosts: dict[str, GitHostConfig]
    azure: AzureConfig | None = None


# -- Loading ---------------------------------------------------------------


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def _require(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _load_user(data: dict[str, object]) -> UserConfig:
    raw = data.get("user")
    if not isinstance(raw, dict):
        raise ConfigError("[user] section is required")

    pub = _expand(str(_require(raw, "ssh_public_key", "user")))
    priv = _expand(str(_require(raw, "ssh_private_key", "user")))

    if not pub.exists():
        raise ConfigError(f"user.ssh_public_key does not exist: {pub}")
    if not priv.exists():
        raise ConfigError(f"user.ssh_private_key does not exist: {priv}")

    return UserConfig(
        ssh_public_key=pub,
        ssh_private_key=priv,
        shell=str(raw.get("shell", "zsh")),
    )


def _load_paths(data: dict[str, object]) -> PathsConfig:
    raw = data.get("paths", {})
    if not isinstance(raw, dict):
        raise ConfigError("[paths] must be a table")
    defaults = PathsConfig()
    local_ws = _expand(str(raw["local_workspaces"])) if "local_workspaces" in raw else defaults.local_workspaces
    code_ws = _expand(str(raw["code_workspaces"])) if "code_workspaces" in raw else defaults.code_workspaces
    return PathsConfig(local_workspaces=local_ws, code_workspaces=code_ws)


def _load_defaults(data: dict[str, object], git_host_names: set[str]) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    platform = raw.get("platform")
    if platform is not None and platform not in VALID_PLATFORMS:
        raise ConfigError(f"defaults.platform must be one of {VALID_PLATFORMS}, got: {platform}")

    git_hosts_val = raw.get("git_hosts", [])
    if not isinstance(git_hosts_val, list):
        raise ConfigError("defaults.git_hosts must be a list")
    for gh in git_hosts_val:
        if gh not in git_host_names:
            raise ConfigError(f"defaults.git_hosts references unknown git host: {gh}")

    return DefaultsConfig(
        platform=str(platform) if platform is not None else None,
        vm_host=str(raw["vm_host"]) if "vm_host" in raw else None,
        git_hosts=list(git_hosts_val),
    )


def _load_dotfiles(data: dict[str, object]) -> DotfilesConfig:
    raw = data.get("dotfiles", {})
    if not isinstance(raw, dict):
        raise ConfigError("[dotfiles] must be a table")
    return DotfilesConfig(
        enabled=bool(raw.get("enabled", True)),
        source=_expand(str(raw["source"])) if "source" in raw else DotfilesConfig().source,
        install_cmd=str(raw.get("install_cmd", "./install.sh")),
    )


def _load_vm_config(data: dict[str, object]) -> VMConfig:
    vm_section = data.get("vm", {})
    if not isinstance(vm_section, dict):
        raise ConfigError("[vm] must be a table")
    raw = vm_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm.config] must be a table")
    return VMConfig(
        apt=list(raw.get("apt", [])),
        snap=list(raw.get("snap", [])),
        install_commands=list(raw.get("install_commands", [])),
        cpus=int(raw.get("cpus", 4)),
        memory=int(raw.get("memory", 8)),
        disk=int(raw.get("disk", 50)),
        azure_vm_size=str(raw.get("azure_vm_size", "Standard_D4s_v5")),
        vm_user=str(raw.get("vm_user", "agentworks")),
    )


def _load_workspace_templates(data: dict[str, object]) -> dict[str, WorkspaceTemplate]:
    raw = data.get("workspace_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[workspace_templates] must be a table")

    templates: dict[str, WorkspaceTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"workspace_templates.{name} must be a table")
        templates[name] = WorkspaceTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            repo=str(tdata["repo"]) if "repo" in tdata else None,
            tmuxinator=bool(tdata["tmuxinator"]) if "tmuxinator" in tdata else None,
        )

    # validate inherits references and cycles
    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates:
                raise ConfigError(f"workspace_templates.{name}.inherits references unknown template: {parent}")
    _detect_cycles(templates)

    return templates


def _detect_cycles(templates: dict[str, WorkspaceTemplate]) -> None:
    visited: set[str] = set()
    in_stack: set[str] = set()

    def visit(name: str) -> None:
        if name in in_stack:
            raise ConfigError(f"workspace template inheritance cycle detected involving: {name}")
        if name in visited:
            return
        in_stack.add(name)
        for parent in templates[name].inherits:
            visit(parent)
        in_stack.remove(name)
        visited.add(name)

    for name in templates:
        visit(name)


def _load_git_hosts(data: dict[str, object]) -> dict[str, GitHostConfig]:
    raw = data.get("git_hosts", {})
    if not isinstance(raw, dict):
        raise ConfigError("[git_hosts] must be a table")

    hosts: dict[str, GitHostConfig] = {}
    for name, hdata in raw.items():
        if not isinstance(hdata, dict):
            raise ConfigError(f"git_hosts.{name} must be a table")
        host_type = str(_require(hdata, "type", f"git_hosts.{name}"))
        if host_type not in VALID_GIT_HOST_TYPES:
            raise ConfigError(f"git_hosts.{name}.type must be one of {VALID_GIT_HOST_TYPES}, got: {host_type}")
        if host_type == "azdo" and "org" not in hdata:
            raise ConfigError(f"git_hosts.{name}.org is required for azdo type")
        hosts[name] = GitHostConfig(
            name=name,
            type=host_type,
            org=str(hdata["org"]) if "org" in hdata else None,
        )
    return hosts


def _load_azure(data: dict[str, object]) -> AzureConfig | None:
    raw = data.get("azure")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[azure] must be a table")
    return AzureConfig(
        subscription_id=str(_require(raw, "subscription_id", "azure")),
        resource_group=str(_require(raw, "resource_group", "azure")),
        region=str(_require(raw, "region", "azure")),
        idle_timeout_hours=int(raw.get("idle_timeout_hours", 2)),
    )


def load_config(path: Path | None = None) -> Config:
    """Load and validate the user configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing or invalid.
        SystemExit: If the config file does not exist.
    """
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        print("Create it to get started. See the documentation for the schema.", file=sys.stderr)
        raise SystemExit(1)

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    git_hosts = _load_git_hosts(data)

    return Config(
        user=_load_user(data),
        paths=_load_paths(data),
        defaults=_load_defaults(data, set(git_hosts.keys())),
        dotfiles=_load_dotfiles(data),
        vm=_load_vm_config(data),
        workspace_templates=_load_workspace_templates(data),
        git_hosts=git_hosts,
        azure=_load_azure(data),
    )
