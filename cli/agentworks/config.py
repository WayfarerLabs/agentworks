"""User configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.vms.templates import ResolvedVMTemplate

CONFIG_DIR = Path.home() / ".config" / "agentworks"
CONFIG_PATH = CONFIG_DIR / "config.toml"

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
# Linux username: alphanumeric, hyphens, underscores; 1-32 chars
VM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# SSH host prefix: alphanumeric, hyphens, underscores, dots
SSH_HOST_PREFIX_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

MAX_NAME_LENGTH = 30


def validate_name(name: str) -> None:
    """Validate a resource name, raising typer.Exit(1) on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end with
    alphanumeric. No consecutive hyphens (reserved for agent username separator).
    Max 30 characters (leaves room for agent username derivation within the
    32-character Linux username limit).
    """
    import typer

    if len(name) > MAX_NAME_LENGTH:
        typer.echo(
            f"Error: name '{name}' is too long ({len(name)} chars, max {MAX_NAME_LENGTH}).",
            err=True,
        )
        raise typer.Exit(1)
    if not NAME_RE.match(name) or "--" in name:
        typer.echo(
            f"Error: invalid name '{name}'. Names must be lowercase alphanumeric "
            "with hyphens or underscores, must start and end with a letter or digit, "
            "and cannot contain consecutive hyphens (--).",
            err=True,
        )
        raise typer.Exit(1)


def validate_admin_username(admin_username: str) -> None:
    """Validate an admin username for shell and OS safety."""
    import typer

    if not VM_USER_RE.match(admin_username):
        typer.echo(
            f"Error: invalid admin_username '{admin_username}'. Must be a valid Linux username "
            "(lowercase, alphanumeric/hyphens/underscores, max 32 chars).",
            err=True,
        )
        raise typer.Exit(1)


# Valid values for enum-like fields
VALID_PLATFORMS = ("lima", "azure", "wsl2")
VALID_GIT_CREDENTIAL_TYPES = ("azdo", "github")


class ConfigError(Exception):
    """Raised when configuration is invalid."""


# -- Data classes ----------------------------------------------------------


@dataclass(frozen=True)
class UserConfig:
    ssh_public_key: Path
    ssh_private_key: Path
    ssh_config: Path = field(default_factory=lambda: Path.home() / ".ssh" / "config")
    ssh_config_dir: bool = True
    ssh_host_prefix: str = "awvm--"
    extra_ssh_public_keys: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class PathsConfig:
    local_workspaces: Path = field(default_factory=lambda: Path.home() / "workspaces")
    vm_workspaces: str = "/opt/agentworks/workspaces"
    vscode_workspaces: Path = field(default_factory=lambda: Path.home() / "aw-vscode-workspaces")


@dataclass(frozen=True)
class DefaultsConfig:
    platform: str | None = None
    vm_host: str | None = None


@dataclass(frozen=True)
class VMTemplate:
    """VM template definition. All fields are optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    # Provisioning
    cpus: int | None = None
    memory: int | None = None
    disk: int | None = None
    azure_vm_size: str | None = None
    swap: int | None = None
    # System-wide initialization
    apt: list[str] | None = None
    apt_packages: list[str] | None = None
    snap: list[str] | None = None
    system_install_commands: list[str] | None = None
    install_mise: bool | None = None
    # Nerf tools
    install_nerf_tools: bool | None = None
    skip_nerf_defaults: bool | None = None
    nerf_addl_manifests: list[Path] | None = None
    nerf_keep_existing: bool | None = None
    nerf_bin_dir: str | None = None
    nerf_skills_dir: str | None = None


@dataclass(frozen=True)
class AdminConfig:
    """Per-user config for the admin user on VMs."""

    username: str = "agentworks"
    shell: str = "zsh"
    git_credentials: list[str] = field(default_factory=list)
    user_install_commands: list[str] = field(default_factory=list)
    dotfiles_source: str | None = None
    dotfiles_destination: str = "~/.dotfiles"
    dotfiles_install_cmd: str = "./install.sh"
    mise_activate: bool = True
    mise_packages: list[str] = field(default_factory=list)
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool = False
    mise_install_before: str = "7d"
    mise_prune_on_reinit: bool = True
    add_nerftools_to_path: bool = False


@dataclass(frozen=True)
class AgentTemplate:
    """Agent template definition. All fields are optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    shell: str | None = None
    git_credentials: list[str] | None = None
    user_install_commands: list[str] | None = None
    dotfiles_source: str | None = None
    dotfiles_destination: str | None = None
    dotfiles_install_cmd: str | None = None
    mise_activate: bool | None = None
    mise_packages: list[str] | None = None
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool | None = None
    mise_install_before: str | None = None
    mise_prune_on_reinit: bool | None = None
    add_nerftools_to_path: bool | None = None


@dataclass(frozen=True)
class WorkspaceTemplate:
    name: str
    inherits: list[str] = field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)


@dataclass(frozen=True)
class GitCredentialConfig:
    name: str
    type: str
    org: str | None = None


@dataclass(frozen=True)
class TaskTemplate:
    """Task template definition. All fields optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    command: str | None = None
    description: str | None = None
    restart_command: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class TaskConfig:
    history_limit: int = 50_000


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
    vm_templates: dict[str, VMTemplate]
    vm: ResolvedVMTemplate
    admin: AdminConfig
    agent_templates: dict[str, AgentTemplate]
    agent: ResolvedAgentTemplate
    task: TaskConfig
    task_templates: dict[str, TaskTemplate]
    workspace_templates: dict[str, WorkspaceTemplate]
    git_credentials: dict[str, GitCredentialConfig]
    apt_sources: dict[str, object] = field(default_factory=dict)
    apt_packages: dict[str, object] = field(default_factory=dict)
    system_install_commands: dict[str, object] = field(default_factory=dict)
    user_install_commands: dict[str, object] = field(default_factory=dict)
    azure: AzureConfig | None = None


# -- Loading ---------------------------------------------------------------


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def _require(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _warn_unexpected_keys(
    raw: dict[str, object],
    known: set[str],
    section: str,
) -> None:
    """Warn about unexpected keys in a config section.

    This catches the common TOML pitfall where a [section] header is
    commented out and its keys land in the previous section.
    """
    unexpected = set(raw.keys()) - known
    if unexpected:
        import warnings

        keys = ", ".join(sorted(unexpected))
        warnings.warn(
            f"Unexpected keys in [{section}]: {keys}. "
            "This usually means a [section] header is commented out "
            "but keys beneath it are not.",
            stacklevel=3,
        )


_USER_KEYS = {
    "ssh_public_key",
    "ssh_private_key",
    "ssh_config",
    "ssh_config_dir",
    "ssh_host_prefix",
    "extra_ssh_public_keys",
}


def _load_user(data: dict[str, object]) -> UserConfig:
    raw = data.get("user")
    if not isinstance(raw, dict):
        raise ConfigError("[user] section is required")

    _warn_unexpected_keys(raw, _USER_KEYS, "user")

    pub = _expand(str(_require(raw, "ssh_public_key", "user")))
    priv = _expand(str(_require(raw, "ssh_private_key", "user")))

    if not pub.exists():
        raise ConfigError(f"user.ssh_public_key does not exist: {pub}")
    if not priv.exists():
        raise ConfigError(f"user.ssh_private_key does not exist: {priv}")

    ssh_config = Path.home() / ".ssh" / "config"
    if "ssh_config" in raw:
        ssh_config = _expand(str(raw["ssh_config"]))

    extra_keys: list[Path] = []
    for entry in raw.get("extra_ssh_public_keys", []):
        p = _expand(str(entry))
        if not p.exists():
            raise ConfigError(f"user.extra_ssh_public_keys: file does not exist: {p}")
        extra_keys.append(p)

    host_prefix = str(raw.get("ssh_host_prefix", "awvm--"))
    if not SSH_HOST_PREFIX_RE.match(host_prefix):
        raise ConfigError(
            f"user.ssh_host_prefix must be alphanumeric with hyphens, underscores, "
            f"or dots (no whitespace or special characters), got: {host_prefix!r}"
        )

    return UserConfig(
        ssh_public_key=pub,
        ssh_private_key=priv,
        ssh_config=ssh_config,
        ssh_config_dir=bool(raw.get("ssh_config_dir", True)),
        ssh_host_prefix=host_prefix,
        extra_ssh_public_keys=extra_keys,
    )


def _load_paths(data: dict[str, object]) -> PathsConfig:
    raw = data.get("paths", {})
    if not isinstance(raw, dict):
        raise ConfigError("[paths] must be a table")
    defaults = PathsConfig()
    local_ws = _expand(str(raw["local_workspaces"])) if "local_workspaces" in raw else defaults.local_workspaces
    vm_ws = str(raw["vm_workspaces"]) if "vm_workspaces" in raw else defaults.vm_workspaces
    if "vscode_workspaces" in raw:
        vscode_ws = _expand(str(raw["vscode_workspaces"]))
    elif "code_workspaces" in raw:
        vscode_ws = _expand(str(raw["code_workspaces"]))
    else:
        vscode_ws = defaults.vscode_workspaces
    return PathsConfig(local_workspaces=local_ws, vm_workspaces=vm_ws, vscode_workspaces=vscode_ws)


_DEFAULTS_KEYS = {"platform", "vm_host"}


def _load_defaults(data: dict[str, object]) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    if "git_credentials" in raw:
        raise ConfigError(
            "defaults.git_credentials has been removed. Move git_credentials into "
            "[admin.config] and/or [agent.config]. See docs/guides/config-migration.md."
        )

    _warn_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults")

    platform = raw.get("platform")
    if platform is not None and platform not in VALID_PLATFORMS:
        raise ConfigError(f"defaults.platform must be one of {VALID_PLATFORMS}, got: {platform}")

    return DefaultsConfig(
        platform=str(platform) if platform is not None else None,
        vm_host=str(raw["vm_host"]) if "vm_host" in raw else None,
    )


_VM_TEMPLATE_KEYS = {
    "inherits",
    "cpus",
    "memory",
    "disk",
    "azure_vm_size",
    "swap",
    "apt",
    "apt_packages",
    "snap",
    "system_install_commands",
    "install_mise",
    "install_nerf_tools",
    "skip_nerf_defaults",
    "nerf_addl_manifests",
    "nerf_keep_existing",
    "nerf_bin_dir",
    "nerf_skills_dir",
}


def _load_vm_templates(data: dict[str, object]) -> dict[str, VMTemplate]:
    raw = data.get("vm_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm_templates] must be a table")

    if "vm" in data and isinstance(data["vm"], dict) and "config" in data["vm"]:
        raise ConfigError(
            "[vm.config] has been replaced by [vm_templates.default]. See docs/guides/config-migration.md for details."
        )

    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}")

        nerf_addl = [_expand(str(m)) for m in tdata["nerf_addl_manifests"]] if "nerf_addl_manifests" in tdata else None

        templates[name] = VMTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            cpus=int(tdata["cpus"]) if "cpus" in tdata else None,
            memory=int(tdata["memory"]) if "memory" in tdata else None,
            disk=int(tdata["disk"]) if "disk" in tdata else None,
            azure_vm_size=str(tdata["azure_vm_size"]) if "azure_vm_size" in tdata else None,
            swap=int(tdata["swap"]) if "swap" in tdata else None,
            apt=list(tdata["apt"]) if "apt" in tdata else None,
            apt_packages=list(tdata["apt_packages"]) if "apt_packages" in tdata else None,
            snap=list(tdata["snap"]) if "snap" in tdata else None,
            system_install_commands=(
                list(tdata["system_install_commands"]) if "system_install_commands" in tdata else None
            ),
            install_mise=bool(tdata["install_mise"]) if "install_mise" in tdata else None,
            install_nerf_tools=bool(tdata["install_nerf_tools"]) if "install_nerf_tools" in tdata else None,
            skip_nerf_defaults=bool(tdata["skip_nerf_defaults"]) if "skip_nerf_defaults" in tdata else None,
            nerf_addl_manifests=nerf_addl,
            nerf_keep_existing=bool(tdata["nerf_keep_existing"]) if "nerf_keep_existing" in tdata else None,
            nerf_bin_dir=str(tdata["nerf_bin_dir"]) if "nerf_bin_dir" in tdata else None,
            nerf_skills_dir=str(tdata["nerf_skills_dir"]) if "nerf_skills_dir" in tdata else None,
        )

    # Validate inherits references and cycles
    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"vm_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "vm_templates")

    return templates


_USER_CONFIG_KEYS = {
    "username",
    "shell",
    "git_credentials",
    "user_install_commands",
    "dotfiles_source",
    "dotfiles_destination",
    "dotfiles_install_cmd",
    "mise_activate",
    "mise_packages",
    "mise_lockfile",
    "mise_allow_unlocked",
    "mise_install_before",
    "mise_prune_on_reinit",
    "add_nerftools_to_path",
}


def _load_admin_config(data: dict[str, object]) -> AdminConfig:
    """Load admin per-user config from [admin.config]."""
    top = data.get("admin", {})
    if not isinstance(top, dict):
        raise ConfigError("[admin] must be a table")
    raw = top.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[admin.config] must be a table")

    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config")

    return AdminConfig(
        username=str(raw.get("username", "agentworks")),
        shell=str(raw.get("shell", "zsh")),
        git_credentials=list(raw.get("git_credentials", [])),
        user_install_commands=list(raw.get("user_install_commands", [])),
        dotfiles_source=str(raw["dotfiles_source"]) if "dotfiles_source" in raw else None,
        dotfiles_destination=str(raw.get("dotfiles_destination", "~/.dotfiles")),
        dotfiles_install_cmd=str(raw.get("dotfiles_install_cmd", "./install.sh")),
        mise_activate=bool(raw.get("mise_activate", True)),
        mise_packages=list(raw.get("mise_packages", [])),
        mise_lockfile=str(raw["mise_lockfile"]) if "mise_lockfile" in raw else None,
        mise_allow_unlocked=bool(raw.get("mise_allow_unlocked", False)),
        mise_install_before=str(raw.get("mise_install_before", "7d")),
        mise_prune_on_reinit=bool(raw.get("mise_prune_on_reinit", True)),
        add_nerftools_to_path=bool(raw.get("add_nerftools_to_path", False)),
    )


_AGENT_TEMPLATE_KEYS = _USER_CONFIG_KEYS | {"inherits"}


def _load_agent_templates(data: dict[str, object]) -> dict[str, AgentTemplate]:
    raw = data.get("agent_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[agent_templates] must be a table")

    if "agent" in data and isinstance(data["agent"], dict) and "config" in data["agent"]:
        raise ConfigError(
            "[agent.config] has been replaced by [agent_templates.default]. "
            "See docs/guides/config-migration.md for details."
        )

    templates: dict[str, AgentTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"agent_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}")

        templates[name] = AgentTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            shell=str(tdata["shell"]) if "shell" in tdata else None,
            git_credentials=list(tdata["git_credentials"]) if "git_credentials" in tdata else None,
            user_install_commands=(list(tdata["user_install_commands"]) if "user_install_commands" in tdata else None),
            dotfiles_source=str(tdata["dotfiles_source"]) if "dotfiles_source" in tdata else None,
            dotfiles_destination=(str(tdata["dotfiles_destination"]) if "dotfiles_destination" in tdata else None),
            dotfiles_install_cmd=(str(tdata["dotfiles_install_cmd"]) if "dotfiles_install_cmd" in tdata else None),
            mise_activate=bool(tdata["mise_activate"]) if "mise_activate" in tdata else None,
            mise_packages=list(tdata["mise_packages"]) if "mise_packages" in tdata else None,
            mise_lockfile=str(tdata["mise_lockfile"]) if "mise_lockfile" in tdata else None,
            mise_allow_unlocked=(bool(tdata["mise_allow_unlocked"]) if "mise_allow_unlocked" in tdata else None),
            mise_install_before=(str(tdata["mise_install_before"]) if "mise_install_before" in tdata else None),
            mise_prune_on_reinit=(bool(tdata["mise_prune_on_reinit"]) if "mise_prune_on_reinit" in tdata else None),
            add_nerftools_to_path=(bool(tdata["add_nerftools_to_path"]) if "add_nerftools_to_path" in tdata else None),
        )

    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"agent_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "agent_templates")

    return templates


def _load_catalog_sections(
    data: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """Load the four user-defined catalog sections as raw dicts.

    Actual parsing into typed entries happens in catalog.py during merge.
    Here we just validate that each section is a table of tables.
    """
    sections = {}
    for section_name in ("apt_sources", "apt_packages", "system_install_commands", "user_install_commands"):
        raw = data.get(section_name, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"[{section_name}] must be a table")
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"{section_name}.{name} must be a table")
        sections[section_name] = raw
    return (
        sections["apt_sources"],
        sections["apt_packages"],
        sections["system_install_commands"],
        sections["user_install_commands"],
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
            if parent not in templates and parent != "default":
                raise ConfigError(f"workspace_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "workspace_templates")

    return templates


class _HasInherits(Protocol):
    @property
    def inherits(self) -> list[str]: ...


def _detect_template_cycles(templates: Mapping[str, _HasInherits], label: str) -> None:
    visited: set[str] = set()
    in_stack: set[str] = set()

    def visit(name: str) -> None:
        if name not in templates:
            return  # implicit default or already validated
        if name in in_stack:
            raise ConfigError(f"{label} inheritance cycle detected involving: {name}")
        if name in visited:
            return
        in_stack.add(name)
        for parent in templates[name].inherits:
            visit(parent)
        in_stack.remove(name)
        visited.add(name)

    for name in templates:
        visit(name)


def _load_git_credentials(data: dict[str, object]) -> dict[str, GitCredentialConfig]:
    raw = data.get("git_credentials", {})
    if not isinstance(raw, dict):
        raise ConfigError("[git_credentials] must be a table")

    creds: dict[str, GitCredentialConfig] = {}
    for name, cdata in raw.items():
        if not isinstance(cdata, dict):
            raise ConfigError(f"git_credentials.{name} must be a table")
        cred_type = str(_require(cdata, "type", f"git_credentials.{name}"))
        if cred_type not in VALID_GIT_CREDENTIAL_TYPES:
            raise ConfigError(
                f"git_credentials.{name}.type must be one of {VALID_GIT_CREDENTIAL_TYPES}, got: {cred_type}"
            )
        if cred_type == "azdo" and "org" not in cdata:
            raise ConfigError(f"git_credentials.{name}.org is required for azdo type")
        creds[name] = GitCredentialConfig(
            name=name,
            type=cred_type,
            org=str(cdata["org"]) if "org" in cdata else None,
        )
    return creds


_TASK_CONFIG_KEYS = {"history_limit"}


def _load_task_config(data: dict[str, object]) -> TaskConfig:
    task_section = data.get("task", {})
    if not isinstance(task_section, dict):
        raise ConfigError("[task] must be a table")
    raw = task_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[task.config] must be a table")

    _warn_unexpected_keys(raw, _TASK_CONFIG_KEYS, "task.config")

    history_limit = int(raw.get("history_limit", 50_000))
    if history_limit < 1:
        raise ConfigError("task.config.history_limit must be a positive integer")

    return TaskConfig(
        history_limit=history_limit,
    )


_TASK_TEMPLATE_KEYS = {"inherits", "command", "description", "restart_command", "env"}


def _load_task_templates(data: dict[str, object]) -> dict[str, TaskTemplate]:
    raw = data.get("task_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[task_templates] must be a table")

    templates: dict[str, TaskTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"task_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _TASK_TEMPLATE_KEYS, f"task_templates.{name}")
        env_raw = tdata.get("env")
        env: dict[str, str] | None = None
        if env_raw is not None:
            if not isinstance(env_raw, dict):
                raise ConfigError(f"task_templates.{name}.env must be a table")
            env = {str(k): str(v) for k, v in env_raw.items()}
        templates[name] = TaskTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            command=str(tdata["command"]) if "command" in tdata else None,
            description=str(tdata["description"]) if "description" in tdata else None,
            restart_command=str(tdata["restart_command"]) if "restart_command" in tdata else None,
            env=env,
        )

    for name, tmpl in templates.items():
        for parent in tmpl.inherits:
            if parent not in templates and parent != "default":
                raise ConfigError(f"task_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "task_templates")

    return templates


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


EXPECTED_TOP_LEVEL_KEYS = {
    "user",
    "paths",
    "defaults",
    "vm_templates",
    "admin",
    "agent_templates",
    "task",
    "task_templates",
    "apt_sources",
    "apt_packages",
    "system_install_commands",
    "user_install_commands",
    "workspace_templates",
    "git_credentials",
    "azure",
}


def _warn_unexpected_top_level_keys(data: dict[str, object]) -> None:
    """Warn about unexpected top-level keys.

    This catches a common TOML pitfall: uncommenting a key without its section
    header causes the key to land in the wrong (or top-level) section.
    """
    unexpected = set(data.keys()) - EXPECTED_TOP_LEVEL_KEYS
    if unexpected:
        import warnings

        keys = ", ".join(sorted(unexpected))
        warnings.warn(
            f"Unexpected top-level keys in config: {keys}. "
            "This usually means a [section] header is commented out "
            "but keys beneath it are not.",
            stacklevel=2,
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
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            print(f"Error: invalid config file {config_path}: {e}", file=sys.stderr)
            raise SystemExit(1) from None

    _warn_unexpected_top_level_keys(data)

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd). "
            "See docs/guides/config-migration.md for details."
        )

    git_credentials = _load_git_credentials(data)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(data)

    task_config = _load_task_config(data)
    task_templates = _load_task_templates(data)

    loaded_vm_templates = _load_vm_templates(data)
    loaded_agent_templates = _load_agent_templates(data)

    # Resolve default templates eagerly so config.vm / config.agent work everywhere
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm

    resolved_vm = _resolve_vm(loaded_vm_templates)

    from agentworks.agents.templates import resolve_from_dict as _resolve_agent

    resolved_agent = _resolve_agent(loaded_agent_templates)

    return Config(
        user=_load_user(data),
        paths=_load_paths(data),
        defaults=_load_defaults(data),
        vm_templates=loaded_vm_templates,
        vm=resolved_vm,
        admin=_load_admin_config(data),
        agent_templates=loaded_agent_templates,
        agent=resolved_agent,
        task=task_config,
        task_templates=task_templates,
        workspace_templates=_load_workspace_templates(data),
        git_credentials=git_credentials,
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
        azure=_load_azure(data),
    )
