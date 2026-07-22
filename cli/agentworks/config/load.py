"""Top-level ``load_config`` entry point: reads the TOML file, pre-scans it
for section-header line numbers, and drives every section loader to compose
a validated ``Config``.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

import sys
import tomllib
from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _load_defaults, _load_git_credentials, _load_operator, _load_paths
from agentworks.config.loaders_resources import (
    _load_admin_config,
    _load_agent_templates,
    _load_apt_and_install_sections,
    _load_named_console,
    _load_vm_sites_legacy,
    _load_vm_templates,
    _load_workspace_templates,
)
from agentworks.config.loaders_secrets import (
    _load_secret_backends,
    _load_secret_config,
    _load_secrets,
    _warn_deprecated_resource_sections,
)
from agentworks.config.loaders_sessions import _load_session_config, _load_session_templates
from agentworks.config.models import Config, _SectionLineMap
from agentworks.errors import ConfigError
from agentworks.source_location import scan_section_lines

if TYPE_CHECKING:
    from pathlib import Path

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
    "secrets",
    "secret_backends",
    "secret_config",
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


def load_config(
    path: Path | None = None,
    *,
    warn_issues: bool = True,
    warn_deprecations: bool = True,
    resources: bool = True,
) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).
        warn_deprecations: Emit the TOML-resource deprecation nudge (default:
            True; also silenceable per-invocation via --no-deprecations). Set
            to False for commands that ARE the remediation the nudge points at
            (e.g. ``agw resource migrate``) -- nagging them is noise.
        resources: Load the TOML resource sections into Config (default:
            True). Settings-only callers (e.g. ``agw resource sample --write``,
            which only needs ``source_path``) pass False: resource sections are
            neither validated nor deprecation-warned, and the resulting Config
            carries ``resources_loaded=False`` -- ``build_registry`` refuses it,
            so a settings-only Config can never silently publish an empty TOML
            side into a registry.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing or invalid.
        SystemExit: If the config file does not exist.
    """
    # Re-imported here (rather than bound at module load) so that tests'
    # ``monkeypatch.setattr("agentworks.config.CONFIG_PATH", ...)``, which
    # patches the attribute on the public ``agentworks.config`` package, is
    # observed. A module-top `from ... import CONFIG_PATH` would instead
    # bind this module's own copy of the name at import time, permanently
    # deaf to a later monkeypatch.
    from agentworks.config import CONFIG_PATH

    config_path = path or CONFIG_PATH
    if not config_path.exists():
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        print("Create it to get started. See the documentation for the schema.", file=sys.stderr)
        raise SystemExit(1)

    raw_text = config_path.read_text()
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: invalid config file {config_path}: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    # Pre-scan the raw text for section-header line numbers so we can attach
    # ``declared_at: SourceLocation`` to every composed Resource. tomllib loses
    # this info on parse; the scanner is a small regex pre-pass.
    decls = _SectionLineMap(
        config_path=config_path,
        section_lines=scan_section_lines(raw_text),
    )

    issues: list[str] = []

    _warn_unexpected_top_level_keys(data, issues)

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd)."
        )

    # Settings-only mode: resource loaders see an empty document, so they
    # produce their framework defaults with zero issues or deprecations.
    # Settings loaders (operator, paths, defaults, session, secret_config)
    # always see the real data; they are config. The legacy [azure] /
    # [proxmox] sections are RESOURCE declarations (vm-site rows) and go
    # through resource_data like every other resource section.
    resource_data = data if resources else {}

    git_credentials = _load_git_credentials(resource_data, issues, decls)
    apt_sources, apt_packages, system_cmds, user_cmds = _load_apt_and_install_sections(resource_data)

    session_config = _load_session_config(data, issues)
    session_templates = _load_session_templates(resource_data, issues, decls)

    loaded_vm_templates = _load_vm_templates(resource_data, issues, decls)
    loaded_agent_templates = _load_agent_templates(resource_data, issues, decls)

    admin = _load_admin_config(resource_data, issues, decls)
    workspace_templates = _load_workspace_templates(resource_data, issues, decls)

    secrets = _load_secrets(resource_data, issues, decls)
    deprecations: list[str] = []
    noop_backend_sections = _load_secret_backends(resource_data, deprecations)
    deprecated_sections = _warn_deprecated_resource_sections(resource_data, deprecations)
    secret_config_data = _load_secret_config(data, issues, decls)
    # Env-block secret references no longer error at config load
    # when they don't match a [secrets.<name>] block; the framework
    # auto-declares them at finalize. Resolution runs through the active
    # backends (``agentworks.secrets.resolve``).

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data, issues, deprecations),
        named_console=_load_named_console(resource_data, issues, decls),
        vm_templates=loaded_vm_templates,
        source_path=config_path,
        admin=admin,
        agent_templates=loaded_agent_templates,
        session=session_config,
        session_templates=session_templates,
        workspace_templates=workspace_templates,
        git_credentials=git_credentials,
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
        vm_sites=_load_vm_sites_legacy(resource_data, decls),
        secrets=secrets,
        secret_config_data=secret_config_data,
        config_issues=tuple(issues),
        deprecation_issues=tuple(deprecations),
        deprecated_sections=deprecated_sections,
        noop_secret_backend_sections=noop_backend_sections,
        resources_loaded=resources,
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")
    if warn_issues and warn_deprecations and config.deprecation_issues:
        from agentworks.output import deprecations_suppressed, warn

        if not deprecations_suppressed():
            for issue in config.deprecation_issues:
                warn(f"Config: {issue}")

    return config
