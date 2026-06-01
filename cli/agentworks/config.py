"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.errors import ConfigError as ConfigError

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
    """Validate a resource name, raising ValidationError on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end with
    alphanumeric. No consecutive hyphens (reserved for agent username separator).
    Max 30 characters (leaves room for agent username derivation within the
    32-character Linux username limit).
    """
    from agentworks.output import ValidationError

    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"name '{name}' is too long ({len(name)} chars, max {MAX_NAME_LENGTH})"
        )
    if not NAME_RE.match(name) or "--" in name:
        raise ValidationError(
            f"invalid name '{name}'. Names must be lowercase alphanumeric "
            "with hyphens or underscores, must start and end with a letter or digit, "
            "and cannot contain consecutive hyphens (--)"
        )


def validate_admin_username(admin_username: str) -> None:
    """Validate an admin username for shell and OS safety."""
    from agentworks.output import ValidationError

    if not VM_USER_RE.match(admin_username):
        raise ValidationError(
            f"invalid admin_username '{admin_username}'. Must be a valid Linux username "
            "(lowercase, alphanumeric/hyphens/underscores, max 32 chars)"
        )


# Valid values for enum-like fields
VALID_PLATFORMS = ("lima", "azure", "wsl2", "proxmox")
VALID_GIT_CREDENTIAL_TYPES = ("azdo", "github")


# -- Data classes ----------------------------------------------------------


@dataclass(frozen=True)
class OperatorConfig:
    ssh_public_key: Path
    ssh_private_key: Path
    ssh_config: Path = field(default_factory=lambda: Path.home() / ".ssh" / "config")
    ssh_config_dir: bool = True
    ssh_host_prefix: str = "awvm--"
    extra_ssh_public_keys: list[Path] = field(default_factory=list)


#: Backward-compatible alias; prefer ``OperatorConfig``.
UserConfig = OperatorConfig


@dataclass(frozen=True)
class PathsConfig:
    vm_workspaces: str = "/opt/agentworks/workspaces"
    vscode_workspaces: Path = field(default_factory=lambda: Path.home() / "aw-vscode-workspaces")
    backups: Path = field(default_factory=lambda: CONFIG_DIR / "backups")


@dataclass(frozen=True)
class DefaultsConfig:
    platform: str | None = None
    vm_host: str | None = None


# Valid tmux preset layouts for named-console session windows. These map
# 1:1 to tmux's built-in select-layout names; we deliberately don't invent
# our own names so operators can apply the same value to a window via
# `tmux select-layout` on the fly.
VALID_TMUX_LAYOUTS = (
    "tiled",
    "even-vertical",
    "even-horizontal",
    "main-vertical",
    "main-horizontal",
)


@dataclass(frozen=True)
class NamedConsoleConfig:
    """Settings for the `console` subcommand group (named multi-session
    consoles). Section is `[named_console]` in the TOML to disambiguate from
    the legacy `vm console` and the workspace console template. Only named
    consoles read these values today.
    """

    tmux_layout: str = "tiled"


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
    # Nerf tools
    nerf_build_claude_plugin: bool | None = None
    skip_nerf_defaults: bool | None = None
    nerf_addl_manifests: list[Path] | None = None
    nerf_home_dir: str | None = None


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
    nerf_install_claude_plugin: bool = False
    git_force_safe_directory: bool = True
    # Claude Code
    claude_marketplaces: list[str] = field(default_factory=list)
    claude_plugins: list[str] = field(default_factory=list)


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
    nerf_install_claude_plugin: bool | None = None
    claude_marketplaces: list[str] | None = None
    claude_plugins: list[str] | None = None


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
    description: str | None = None


@dataclass(frozen=True)
class SessionTemplate:
    """Session template definition. All fields optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    command: str | None = None
    description: str | None = None
    restart_command: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class SessionConfig:
    history_limit: int = 50_000


@dataclass(frozen=True)
class AzureConfig:
    subscription_id: str
    resource_group: str
    region: str
    idle_timeout_hours: int = 2


@dataclass(frozen=True)
class ProxmoxConfig:
    api_url: str
    node: str
    token_id: str
    template_vmid: int
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    pool: str = "agentworks"
    verify_ssl: bool = True


@dataclass(frozen=True)
class Config:
    operator: OperatorConfig
    paths: PathsConfig
    defaults: DefaultsConfig
    named_console: NamedConsoleConfig
    vm_templates: dict[str, VMTemplate]
    vm: ResolvedVMTemplate
    admin: AdminConfig
    agent_templates: dict[str, AgentTemplate]
    agent: ResolvedAgentTemplate
    session: SessionConfig
    session_templates: dict[str, SessionTemplate]
    workspace_templates: dict[str, WorkspaceTemplate]
    git_credentials: dict[str, GitCredentialConfig]
    apt_sources: dict[str, object] = field(default_factory=dict)
    apt_packages: dict[str, object] = field(default_factory=dict)
    system_install_commands: dict[str, object] = field(default_factory=dict)
    user_install_commands: dict[str, object] = field(default_factory=dict)
    azure: AzureConfig | None = None
    proxmox: ProxmoxConfig | None = None
    config_issues: tuple[str, ...] = ()


# -- Loading ---------------------------------------------------------------


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def _require(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise ConfigError(f"{context}.{key} is required")
    return data[key]


def _require_string_list(data: dict[str, object], key: str, context: str) -> list[str]:
    """Load a key as a list of strings, raising ConfigError on type mismatch."""
    val = data.get(key, [])
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ConfigError(f"{context}.{key} must be a list of strings")
    return val


def _warn_unexpected_keys(
    raw: dict[str, object],
    known: set[str],
    section: str,
    issues: list[str],
) -> None:
    """Record unexpected keys in a config section.

    This catches the common TOML pitfall where a [section] header is
    commented out and its keys land in the previous section, as well as
    typos and version mismatches. Issues are collected on the Config object
    so that doctor can report all of them without short-circuiting.
    """
    unexpected = set(raw.keys()) - known
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        issues.append(f"unexpected keys in [{section}]: {keys}")


_OPERATOR_KEYS = {
    "ssh_public_key",
    "ssh_private_key",
    "ssh_config",
    "ssh_config_dir",
    "ssh_host_prefix",
    "extra_ssh_public_keys",
}


def _load_operator(data: dict[str, object], issues: list[str]) -> OperatorConfig:
    raw = data.get("operator")
    section_name = "operator"
    if not isinstance(raw, dict):
        # Accept [user] as a deprecated alias for [operator]
        raw = data.get("user")
        if isinstance(raw, dict):
            print(
                "WARNING: config [user] section is deprecated; rename it to [operator].",
                file=sys.stderr,
            )
            section_name = "user"
        else:
            raise ConfigError("[operator] section is required")

    _warn_unexpected_keys(raw, _OPERATOR_KEYS, section_name, issues)

    pub = _expand(str(_require(raw, "ssh_public_key", section_name)))
    priv = _expand(str(_require(raw, "ssh_private_key", section_name)))

    if not pub.exists():
        raise ConfigError(f"{section_name}.ssh_public_key does not exist: {pub}")
    if not priv.exists():
        raise ConfigError(f"{section_name}.ssh_private_key does not exist: {priv}")

    ssh_config = Path.home() / ".ssh" / "config"
    if "ssh_config" in raw:
        ssh_config = _expand(str(raw["ssh_config"]))

    extra_keys: list[Path] = []
    for entry in raw.get("extra_ssh_public_keys", []):
        p = _expand(str(entry))
        if not p.exists():
            raise ConfigError(f"{section_name}.extra_ssh_public_keys: file does not exist: {p}")
        extra_keys.append(p)

    host_prefix = str(raw.get("ssh_host_prefix", "awvm--"))
    if not SSH_HOST_PREFIX_RE.match(host_prefix):
        raise ConfigError(
            f"{section_name}.ssh_host_prefix must be alphanumeric with hyphens, underscores, "
            f"or dots (no whitespace or special characters), got: {host_prefix!r}"
        )

    return OperatorConfig(
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
    vm_ws = str(raw["vm_workspaces"]) if "vm_workspaces" in raw else defaults.vm_workspaces
    if "vscode_workspaces" in raw:
        vscode_ws = _expand(str(raw["vscode_workspaces"]))
    elif "code_workspaces" in raw:
        vscode_ws = _expand(str(raw["code_workspaces"]))
    else:
        vscode_ws = defaults.vscode_workspaces
    backups = _expand(str(raw["backups"])) if "backups" in raw else defaults.backups
    return PathsConfig(vm_workspaces=vm_ws, vscode_workspaces=vscode_ws, backups=backups)


_DEFAULTS_KEYS = {"platform", "vm_host"}


def _load_defaults(data: dict[str, object], issues: list[str]) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    if "git_credentials" in raw:
        raise ConfigError(
            "defaults.git_credentials has been removed. Move git_credentials into "
            "[admin.config] and/or [agent.config]."
        )

    _warn_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults", issues)

    platform = raw.get("platform")
    if platform is not None and platform not in VALID_PLATFORMS:
        raise ConfigError(f"defaults.platform must be one of {VALID_PLATFORMS}, got: {platform}")

    return DefaultsConfig(
        platform=str(platform) if platform is not None else None,
        vm_host=str(raw["vm_host"]) if "vm_host" in raw else None,
    )


_NAMED_CONSOLE_KEYS = {"tmux_layout"}


def _load_named_console(
    data: dict[str, object], issues: list[str]
) -> NamedConsoleConfig:
    raw = data.get("named_console", {})
    if not isinstance(raw, dict):
        raise ConfigError("[named_console] must be a table")

    _warn_unexpected_keys(raw, _NAMED_CONSOLE_KEYS, "named_console", issues)

    layout = raw.get("tmux_layout", "tiled")
    if layout not in VALID_TMUX_LAYOUTS:
        raise ConfigError(
            f"named_console.tmux_layout must be one of {VALID_TMUX_LAYOUTS}, "
            f"got: {layout}"
        )

    return NamedConsoleConfig(tmux_layout=str(layout))


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
    "nerf_build_claude_plugin",
    "skip_nerf_defaults",
    "nerf_addl_manifests",
    "nerf_home_dir",
}


def _load_vm_templates(data: dict[str, object], issues: list[str]) -> dict[str, VMTemplate]:
    raw = data.get("vm_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm_templates] must be a table")

    if "vm" in data and isinstance(data["vm"], dict) and "config" in data["vm"]:
        raise ConfigError(
            "[vm.config] has been replaced by [vm_templates.default]."
        )

    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

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
            nerf_build_claude_plugin=(
                bool(tdata["nerf_build_claude_plugin"]) if "nerf_build_claude_plugin" in tdata else None
            ),
            skip_nerf_defaults=bool(tdata["skip_nerf_defaults"]) if "skip_nerf_defaults" in tdata else None,
            nerf_addl_manifests=nerf_addl,
            nerf_home_dir=str(tdata["nerf_home_dir"]) if "nerf_home_dir" in tdata else None,
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
    "nerf_install_claude_plugin",
    "git_force_safe_directory",
    "claude_marketplaces",
    "claude_plugins",
}


def _load_admin_config(data: dict[str, object], issues: list[str]) -> AdminConfig:
    """Load admin per-user config from [admin.config]."""
    top = data.get("admin", {})
    if not isinstance(top, dict):
        raise ConfigError("[admin] must be a table")
    raw = top.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[admin.config] must be a table")

    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config", issues)

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
        nerf_install_claude_plugin=bool(raw.get("nerf_install_claude_plugin", False)),
        git_force_safe_directory=bool(raw.get("git_force_safe_directory", True)),
        claude_marketplaces=_require_string_list(raw, "claude_marketplaces", "admin.config"),
        claude_plugins=_require_string_list(raw, "claude_plugins", "admin.config"),
    )


_AGENT_TEMPLATE_KEYS = _USER_CONFIG_KEYS | {"inherits"}


def _load_agent_templates(data: dict[str, object], issues: list[str]) -> dict[str, AgentTemplate]:
    raw = data.get("agent_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[agent_templates] must be a table")

    if "agent" in data and isinstance(data["agent"], dict) and "config" in data["agent"]:
        raise ConfigError(
            "[agent.config] has been replaced by [agent_templates.default]."
        )

    templates: dict[str, AgentTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"agent_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}", issues)

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
            nerf_install_claude_plugin=(
                bool(tdata["nerf_install_claude_plugin"]) if "nerf_install_claude_plugin" in tdata else None
            ),
            claude_marketplaces=(
                _require_string_list(tdata, "claude_marketplaces", f"agent_templates.{name}")
                if "claude_marketplaces" in tdata else None
            ),
            claude_plugins=(
                _require_string_list(tdata, "claude_plugins", f"agent_templates.{name}")
                if "claude_plugins" in tdata else None
            ),
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
            description=str(cdata["description"]) if "description" in cdata else None,
        )
    return creds


_SESSION_CONFIG_KEYS = {"history_limit"}


def _load_session_config(data: dict[str, object], issues: list[str]) -> SessionConfig:
    session_section = data.get("session", {})
    if not isinstance(session_section, dict):
        raise ConfigError("[session] must be a table")
    raw = session_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[session.config] must be a table")

    _warn_unexpected_keys(raw, _SESSION_CONFIG_KEYS, "session.config", issues)

    history_limit = int(raw.get("history_limit", 50_000))
    if history_limit < 1:
        raise ConfigError("session.config.history_limit must be a positive integer")

    return SessionConfig(
        history_limit=history_limit,
    )


_SESSION_TEMPLATE_KEYS = {"inherits", "command", "description", "restart_command", "env"}


def _load_session_templates(data: dict[str, object], issues: list[str]) -> dict[str, SessionTemplate]:
    raw = data.get("session_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[session_templates] must be a table")

    templates: dict[str, SessionTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"session_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}", issues)
        env_raw = tdata.get("env")
        env: dict[str, str] | None = None
        if env_raw is not None:
            if not isinstance(env_raw, dict):
                raise ConfigError(f"session_templates.{name}.env must be a table")
            env = {str(k): str(v) for k, v in env_raw.items()}
        templates[name] = SessionTemplate(
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
                raise ConfigError(f"session_templates.{name}.inherits references unknown template: {parent}")
    _detect_template_cycles(templates, "session_templates")

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


def _load_proxmox(data: dict[str, object]) -> ProxmoxConfig | None:
    raw = data.get("proxmox")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[proxmox] must be a table")
    return ProxmoxConfig(
        api_url=str(_require(raw, "api_url", "proxmox")),
        node=str(_require(raw, "node", "proxmox")),
        token_id=str(_require(raw, "token_id", "proxmox")),
        template_vmid=int(str(_require(raw, "template_vmid", "proxmox"))),
        storage=str(raw.get("storage", "local-lvm")),
        bridge=str(raw.get("bridge", "vmbr0")),
        pool=str(raw.get("pool", "agentworks")),
        verify_ssl=bool(raw.get("verify_ssl", True)),
    )


EXPECTED_TOP_LEVEL_KEYS = {
    "operator",
    "paths",
    "defaults",
    "named_console",
    "vm_templates",
    "admin",
    "agent_templates",
    "session",
    "session_templates",
    "apt_sources",
    "apt_packages",
    "system_install_commands",
    "user_install_commands",
    "workspace_templates",
    "git_credentials",
    "azure",
    "proxmox",
}


def _warn_unexpected_top_level_keys(data: dict[str, object], issues: list[str]) -> None:
    """Record unexpected top-level keys.

    This catches a common TOML pitfall: uncommenting a key without its section
    header causes the key to land in the wrong (or top-level) section.
    """
    unexpected = set(data.keys()) - EXPECTED_TOP_LEVEL_KEYS
    if unexpected:
        keys = ", ".join(sorted(unexpected))
        issues.append(f"unexpected top-level keys in config: {keys}")


def load_config(path: Path | None = None, *, warn_issues: bool = True) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).

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

    issues: list[str] = []

    _warn_unexpected_top_level_keys(data, issues)

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd)."
        )

    git_credentials = _load_git_credentials(data)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(data)

    session_config = _load_session_config(data, issues)
    session_templates = _load_session_templates(data, issues)

    loaded_vm_templates = _load_vm_templates(data, issues)
    loaded_agent_templates = _load_agent_templates(data, issues)

    # Resolve default templates eagerly so config.vm / config.agent work everywhere
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm

    resolved_vm = _resolve_vm(loaded_vm_templates)

    from agentworks.agents.templates import resolve_from_dict as _resolve_agent

    resolved_agent = _resolve_agent(loaded_agent_templates)

    admin = _load_admin_config(data, issues)

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data, issues),
        named_console=_load_named_console(data, issues),
        vm_templates=loaded_vm_templates,
        vm=resolved_vm,
        admin=admin,
        agent_templates=loaded_agent_templates,
        agent=resolved_agent,
        session=session_config,
        session_templates=session_templates,
        workspace_templates=_load_workspace_templates(data),
        git_credentials=git_credentials,
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
        azure=_load_azure(data),
        proxmox=_load_proxmox(data),
        config_issues=tuple(issues),
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")

    return config
