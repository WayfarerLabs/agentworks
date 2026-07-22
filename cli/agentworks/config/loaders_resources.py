"""TOML resource-section loaders: named consoles, VM templates, the admin
config singleton, agent templates, the apt/install-command sections,
workspace templates, and the legacy ``[azure]`` / ``[proxmox]`` vm-site
sections.

Session-related loaders (``[session.config]``, ``[session_templates.*]``)
live in ``agentworks.config.loaders_sessions`` instead: the harness-hoisting
logic there is a large, self-contained unit that would otherwise dominate
this module.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.agents.template import AgentTemplate
from agentworks.config.loaders_core import _parse_env_table, _require_string_list, _warn_unexpected_keys
from agentworks.errors import ConfigError
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS
from agentworks.sessions.template import NamedConsoleConfig
from agentworks.vms.admin import AdminConfig
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate

if TYPE_CHECKING:
    from agentworks.config.models import _SectionLineMap
    from agentworks.vms.sites import VMSiteDecl

_NAMED_CONSOLE_KEYS = {"description", "tmux_layout"}


def _load_named_console(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> NamedConsoleConfig | None:
    if "named_console" not in data:
        # Nothing declared: the framework auto-declares the default row
        # (always-materialize), same as every other reserved-default
        # kind. The manifest decoder always passes the key, so this
        # None path is TOML-only.
        return None
    raw = data.get("named_console", {})
    if not isinstance(raw, dict):
        raise ConfigError("[named_console] must be a table")

    _warn_unexpected_keys(raw, _NAMED_CONSOLE_KEYS, "named_console", issues)

    layout = raw.get("tmux_layout", AW_SESSION_VERTICAL_LAYOUT)
    if layout not in VALID_TMUX_LAYOUTS:
        raise ConfigError(f"named_console.tmux_layout must be one of {VALID_TMUX_LAYOUTS}, got: {layout}")

    return NamedConsoleConfig(
        name="default",
        tmux_layout=str(layout),
        description=str(raw["description"]) if "description" in raw else None,
        declared_at=decls.lookup("named_console"),
    )


_VM_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "cpus",
    "memory",
    "disk",
    "swap",
    "apt",
    "apt_packages",
    "snap",
    "system_install_commands",
    "env",
    "tailscale_auth_key",
}


def _load_vm_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, VMTemplate]:
    raw = data.get("vm_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[vm_templates] must be a table")

    if "vm" in data and isinstance(data["vm"], dict) and "config" in data["vm"]:
        raise ConfigError("[vm.config] has been replaced by [vm_templates.default].")

    # TOML's implicit-parent semantics already populate this dict: writing
    # `[vm_templates.x.env]` alone produces `raw == {"x": {"env": {...}}}` even
    # without a separate `[vm_templates.x]` header, and the loop iterates `x`
    # like any other template. That minimal form is a
    # valid Resource; declared_at picks the earliest contributing header (the
    # env line, in that case) via `_SectionLineMap.lookup`.
    templates: dict[str, VMTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"vm_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

        # tailscale_auth_key must be a non-empty bare string secret
        # name; no `{ secret = "..." }` polymorphism per the SDD (the
        # field IS the secret reference, not a value-or-ref
        # discriminator). Absence means "inherit" via the raw-template's
        # None sentinel; the resolver applies the default
        # ``"tailscale-auth-key"`` when no ancestor set it explicitly.
        # Empty-string is rejected: it would derive an env-var name
        # ``AW_SECRET_`` (empty suffix) and prompt the operator for a
        # secret called ``""`` -- a usability footgun on a security-
        # relevant field.
        ts_key_raw: str | None = None
        if "tailscale_auth_key" in tdata:
            if not isinstance(tdata["tailscale_auth_key"], str):
                raise ConfigError(
                    f"vm_templates.{name}.tailscale_auth_key must be a bare secret "
                    f"name (string), got {type(tdata['tailscale_auth_key']).__name__}"
                )
            if not tdata["tailscale_auth_key"]:
                raise ConfigError(
                    f"vm_templates.{name}.tailscale_auth_key must not be empty; "
                    f"omit the key to inherit the default secret name "
                    f'"tailscale-auth-key"'
                )
            ts_key_raw = tdata["tailscale_auth_key"]

        templates[name] = VMTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            cpus=int(tdata["cpus"]) if "cpus" in tdata else None,
            memory=int(tdata["memory"]) if "memory" in tdata else None,
            disk=int(tdata["disk"]) if "disk" in tdata else None,
            swap=int(tdata["swap"]) if "swap" in tdata else None,
            apt=list(tdata["apt"]) if "apt" in tdata else None,
            apt_packages=list(tdata["apt_packages"]) if "apt_packages" in tdata else None,
            snap=list(tdata["snap"]) if "snap" in tdata else None,
            system_install_commands=(
                list(tdata["system_install_commands"]) if "system_install_commands" in tdata else None
            ),
            tailscale_auth_key=ts_key_raw,
            env=_parse_env_table(tdata.get("env"), context=f"vm_templates.{name}", issues=issues),
            declared_at=decls.lookup("vm_templates", name),
        )

    # Validation lives in the framework / resolver, not here. The
    # framework's VMTemplateKind miss policy + Registry.finalize cycle
    # pass own the canonical inherits-reference validation (called via
    # build_registry). The per-template field-merging resolver in
    # agentworks.vms.templates also has its own visited-set cycle guard
    # for the load-time eager-resolve path. Either pass catches malformed
    # configs; this loader no longer does a pre-pass.

    return templates


_USER_CONFIG_KEYS = {
    "description",
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
    "git_force_safe_directory",
    "claude_marketplaces",
    "claude_plugins",
}


def _load_admin_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
    name: str = "default",
) -> AdminConfig | None:
    """Load admin per-user config from [admin.config].

    Returns ``None`` when the TOML has no ``[admin.*]`` sections at all:
    the framework auto-declares ``admin-template:default`` instead
    (always-materialize). The manifest decoder always passes the key.

    ``name`` is the resource name for the loaded row. The TOML path is a
    singleton and never passes it (stays ``default``); the manifest
    decoder passes the document's ``metadata.name`` so a declared
    admin-template carries its own name.
    """
    if "admin" not in data:
        return None
    top = data.get("admin", {})
    if not isinstance(top, dict):
        raise ConfigError("[admin] must be a table")
    raw = top.get("config", {})
    if not isinstance(raw, dict):
        raise ConfigError("[admin.config] must be a table")

    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config", issues)

    return AdminConfig(
        name=name,
        description=str(raw["description"]) if "description" in raw else None,
        username=str(raw.get("username", "agentworks")),
        shell=str(raw.get("shell", "bash")),
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
        git_force_safe_directory=bool(raw.get("git_force_safe_directory", True)),
        claude_marketplaces=_require_string_list(raw, "claude_marketplaces", "admin.config"),
        claude_plugins=_require_string_list(raw, "claude_plugins", "admin.config"),
        env=_parse_env_table(top.get("env"), context="admin", issues=issues),
        declared_at=decls.lookup("admin"),
    )


_AGENT_TEMPLATE_KEYS = _USER_CONFIG_KEYS | {"inherits", "env"}


def _load_agent_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, AgentTemplate]:
    raw = data.get("agent_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[agent_templates] must be a table")

    if "agent" in data and isinstance(data["agent"], dict) and "config" in data["agent"]:
        raise ConfigError("[agent.config] has been replaced by [agent_templates.default].")

    templates: dict[str, AgentTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"agent_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}", issues)

        templates[name] = AgentTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
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
            claude_marketplaces=(
                _require_string_list(tdata, "claude_marketplaces", f"agent_templates.{name}")
                if "claude_marketplaces" in tdata
                else None
            ),
            claude_plugins=(
                _require_string_list(tdata, "claude_plugins", f"agent_templates.{name}")
                if "claude_plugins" in tdata
                else None
            ),
            env=_parse_env_table(tdata.get("env"), context=f"agent_templates.{name}", issues=issues),
            declared_at=decls.lookup("agent_templates", name),
        )

    # Inherits-reference validation and cycle detection live in the
    # framework (AgentTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The agents/templates.py resolver
    # also has its own visited-set guard as a safety net for callers
    # that resolve without going through build_registry.
    return templates


def _load_apt_and_install_sections(
    data: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """Load the four user-defined apt / install-command sections as raw dicts.

    Actual parsing into typed entries happens in the ``apt`` and
    ``install_commands`` operator publishers. Here we just validate that
    each section is a table of tables.
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


_WORKSPACE_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "repo",
    "tmuxinator",
    "git_user_name",
    "git_user_email",
    "env",
}


def _load_workspace_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, WorkspaceTemplate]:
    raw = data.get("workspace_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[workspace_templates] must be a table")

    templates: dict[str, WorkspaceTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"workspace_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _WORKSPACE_TEMPLATE_KEYS, f"workspace_templates.{name}", issues)
        repo = str(tdata["repo"]) if "repo" in tdata else None
        # Embedded-username advice moved to a provider-owned preflight
        # (git_credentials.remote_advisories, run when a template is
        # actually used): only the credential instance knows its host and
        # scope, so the judgment lives there rather than in this loader.
        templates[name] = WorkspaceTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            repo=repo,
            tmuxinator=bool(tdata["tmuxinator"]) if "tmuxinator" in tdata else None,
            git_user_name=(str(tdata["git_user_name"]) if "git_user_name" in tdata else None),
            git_user_email=(str(tdata["git_user_email"]) if "git_user_email" in tdata else None),
            env=_parse_env_table(
                tdata.get("env"),
                context=f"workspace_templates.{name}",
                issues=issues,
            ),
            declared_at=decls.lookup("workspace_templates", name),
        )

    # Inherits-reference validation and cycle detection live in the
    # framework (WorkspaceTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The workspaces/templates.py
    # resolver also has its own visited-set guard.
    return templates


# The flat legacy [azure] / [proxmox] keys that hoist into the nested
# platform_config blob. The flat domain stays silently loose on stray
# keys (the git-credential `org` precedent: promoting historically-
# ignored keys into validation errors would break released configs);
# manifests validate the true blob strictly.
_LEGACY_SITE_SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    # The section (and thus the declared SITE) keeps its historical
    # name "azure" (released configs and VM rows point at it), while
    # the platform underneath is azure-vm (the Azure Virtual Machines
    # service specifically).
    "azure": ("azure-vm", ("subscription_id", "resource_group", "region")),
    "proxmox": (
        "proxmox",
        (
            "api_url",
            "node",
            "token_id",
            "template_vmid",
            "storage",
            "bridge",
            "pool",
            "verify_ssl",
            "token_secret",
        ),
    ),
}


def _load_vm_sites_legacy(
    data: dict[str, object],
    decls: _SectionLineMap,
) -> dict[str, VMSiteDecl]:
    """Load the legacy ``[azure]`` / ``[proxmox]`` sections as ``vm-site``
    resources (dual-path; the sections warn as deprecated via
    ``_warn_deprecated_resource_sections``).

    Flat TOML is the one place platform-owned fields sit outside the
    ``platform_config`` blob; this loader nests at the boundary
    (section name becomes the site name, ``platform`` is synthesized),
    exactly as the git-credential loader nests ``org``. The platform
    capability validates the assembled blob so errors carry config
    vocabulary and the implied secret references derive at finalize.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.vms.sites import VMSiteDecl

    sites: dict[str, VMSiteDecl] = {}
    for section, (platform_name, known_keys) in _LEGACY_SITE_SECTIONS.items():
        raw = data.get(section)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ConfigError(f"[{section}] must be a table")
        platform_config: dict[str, object] = {key: raw[key] for key in known_keys if key in raw}
        capability = VM_PLATFORM_REGISTRY[platform_name]
        capability.validate_config(f"[{section}]", platform_config)
        sites[section] = VMSiteDecl(
            name=section,
            platform=platform_name,
            platform_config=platform_config,
            declared_at=decls.lookup(section),
        )
    return sites
