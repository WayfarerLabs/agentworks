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
    code_workspaces: Path = field(default_factory=lambda: Path.home() / "agentworks-workspaces")


@dataclass(frozen=True)
class DefaultsConfig:
    platform: str | None = None
    vm_host: str | None = None
    git_credentials: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DotfilesConfig:
    enabled: bool = True
    source: Path = field(default_factory=lambda: Path.home() / ".dotfiles")
    install_cmd: str = "./install.sh"


@dataclass(frozen=True)
class VMConfig:
    # Provisioning (immutable after vm create)
    cpus: int = 4
    memory: int = 8  # GiB
    disk: int = 50  # GiB
    azure_vm_size: str = "Standard_B2s"
    admin_username: str = "agentworks"
    # Initialization (applied on create and reinit)
    admin_shell: str = "zsh"
    apt: list[str] = field(default_factory=list)
    apt_packages: list[str] = field(default_factory=list)
    snap: list[str] = field(default_factory=list)
    system_install_commands: list[str] = field(default_factory=list)
    admin_install_commands: list[str] = field(default_factory=list)
    # Nerf tools
    install_nerf_tools: bool = False
    skip_nerf_defaults: bool = False
    nerf_addl_manifests: list[Path] = field(default_factory=list)
    nerf_keep_existing: bool = False
    nerf_bin_dir: str = "/opt/agentworks/nerf/bin"
    nerf_skills_dir: str = "/opt/agentworks/nerf/skills"


@dataclass(frozen=True)
class AgentConfig:
    user_install_commands: list[str] = field(default_factory=list)
    shell: str = "bash"


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
    agent: AgentConfig
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
    code_ws = _expand(str(raw["code_workspaces"])) if "code_workspaces" in raw else defaults.code_workspaces
    return PathsConfig(local_workspaces=local_ws, code_workspaces=code_ws)


_DEFAULTS_KEYS = {"platform", "vm_host", "git_credentials"}


def _load_defaults(data: dict[str, object], git_credential_names: set[str]) -> DefaultsConfig:
    raw = data.get("defaults", {})
    if not isinstance(raw, dict):
        raise ConfigError("[defaults] must be a table")

    _warn_unexpected_keys(raw, _DEFAULTS_KEYS, "defaults")

    platform = raw.get("platform")
    if platform is not None and platform not in VALID_PLATFORMS:
        raise ConfigError(f"defaults.platform must be one of {VALID_PLATFORMS}, got: {platform}")

    git_creds_val = raw.get("git_credentials", [])
    if not isinstance(git_creds_val, list):
        raise ConfigError("defaults.git_credentials must be a list")
    for gc in git_creds_val:
        if gc not in git_credential_names:
            raise ConfigError(f"defaults.git_credentials references unknown git credential: {gc}")

    return DefaultsConfig(
        platform=str(platform) if platform is not None else None,
        vm_host=str(raw["vm_host"]) if "vm_host" in raw else None,
        git_credentials=list(git_creds_val),
    )


_DOTFILES_KEYS = {"enabled", "source", "install_cmd"}


def _load_dotfiles(data: dict[str, object]) -> DotfilesConfig:
    raw = data.get("dotfiles", {})
    if not isinstance(raw, dict):
        raise ConfigError("[dotfiles] must be a table")

    _warn_unexpected_keys(raw, _DOTFILES_KEYS, "dotfiles")
    return DotfilesConfig(
        enabled=bool(raw.get("enabled", True)),
        source=_expand(str(raw["source"])) if "source" in raw else DotfilesConfig().source,
        install_cmd=str(raw.get("install_cmd", "./install.sh")),
    )


_VM_CONFIG_KEYS = {
    "cpus",
    "memory",
    "disk",
    "azure_vm_size",
    "admin_username",
    "admin_shell",
    "apt",
    "apt_packages",
    "snap",
    "system_install_commands",
    "admin_install_commands",
    "install_nerf_tools",
    "skip_nerf_defaults",
    "nerf_addl_manifests",
    "nerf_keep_existing",
    "nerf_bin_dir",
    "nerf_skills_dir",
}


def _load_vm_config(data: dict[str, object]) -> VMConfig:
    vm_section = data.get("vm", {})
    if not isinstance(vm_section, dict):
        raise ConfigError("[vm] must be a table")
    raw = vm_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm.config] must be a table")

    _warn_unexpected_keys(raw, _VM_CONFIG_KEYS, "vm.config")

    nerf_addl_manifests = [_expand(str(m)) for m in raw.get("nerf_addl_manifests", [])]

    return VMConfig(
        cpus=int(raw.get("cpus", 4)),
        memory=int(raw.get("memory", 8)),
        disk=int(raw.get("disk", 50)),
        azure_vm_size=str(raw.get("azure_vm_size", "Standard_B2s")),
        admin_username=str(raw.get("admin_username", "agentworks")),
        admin_shell=str(raw.get("admin_shell", "zsh")),
        apt=list(raw.get("apt", [])),
        apt_packages=list(raw.get("apt_packages", [])),
        snap=list(raw.get("snap", [])),
        system_install_commands=list(raw.get("system_install_commands", [])),
        admin_install_commands=list(raw.get("admin_install_commands", [])),
        install_nerf_tools=bool(raw.get("install_nerf_tools", False)),
        skip_nerf_defaults=bool(raw.get("skip_nerf_defaults", False)),
        nerf_addl_manifests=nerf_addl_manifests,
        nerf_keep_existing=bool(raw.get("nerf_keep_existing", False)),
        nerf_bin_dir=str(raw.get("nerf_bin_dir", "/opt/agentworks/nerf/bin")),
        nerf_skills_dir=str(raw.get("nerf_skills_dir", "/opt/agentworks/nerf/skills")),
    )


_AGENT_CONFIG_KEYS = {"user_install_commands", "shell"}


def _load_agent_config(data: dict[str, object]) -> AgentConfig:
    agent_section = data.get("agent", {})
    if not isinstance(agent_section, dict):
        raise ConfigError("[agent] must be a table")
    raw = agent_section.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[agent.config] must be a table")

    _warn_unexpected_keys(raw, _AGENT_CONFIG_KEYS, "agent.config")

    return AgentConfig(
        user_install_commands=list(raw.get("user_install_commands", [])),
        shell=str(raw.get("shell", "bash")),
    )


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
    "dotfiles",
    "vm",
    "agent",
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

    git_credentials = _load_git_credentials(data)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_catalog_sections(data)

    return Config(
        user=_load_user(data),
        paths=_load_paths(data),
        defaults=_load_defaults(data, set(git_credentials.keys())),
        dotfiles=_load_dotfiles(data),
        vm=_load_vm_config(data),
        agent=_load_agent_config(data),
        workspace_templates=_load_workspace_templates(data),
        git_credentials=git_credentials,
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
        azure=_load_azure(data),
    )
