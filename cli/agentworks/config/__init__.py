"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.

This package holds the settings dataclasses and the settings-section
loaders; nothing else. config.toml is settings only now (ADR 0022): the
TOML resource loaders are gone and a resource-declaring section is a hard
error at load. The declarable-resource dataclasses (VMTemplate,
AgentTemplate, AdminConfig, WorkspaceTemplate, SessionTemplate,
NamedConsoleConfig, GitCredentialConfig) live in their domain packages;
kind definitions live there too (see ``agentworks.resources.kinds`` for the
registration index).

It was split out of a single ~1600-line ``config.py`` module into this
package, one submodule per cohesive concern, while keeping the public
import path ``agentworks.config`` unchanged:

- ``validation``: ``CONFIG_DIR`` / ``CONFIG_PATH`` and the name/username
  validators. Has no dependency on any sibling submodule.
- ``models``: the settings dataclasses, the ``Config`` object (and its
  now-empty ``publish_to``), and ``_SectionLineMap``.
- ``loaders_core``: generic TOML-loading helpers (the unknown-key pair) and
  the ``[operator]`` / ``[paths]`` / ``[defaults]`` settings loaders.
- ``loaders_database``: the strict ``[database]`` loader and focused
  migration-safety projection.
- ``loaders_sessions``: the ``[session.config]`` settings loader.
- ``loaders_secrets``: the ``[secret_config]`` and ``[plugins]`` settings
  loaders.
- ``references``: the settings values that NAME resource rows, and the
  post-finalize check that they resolve. Not a loader: the registry does not
  exist yet at load time, so the loaders validate shape and this validates
  existence, from ``bootstrap.build_registry``.
- ``load``: the ``load_config`` entry point (drives the settings loaders and
  the resource-section hard error).

This ``__init__.py`` re-exports the public surface (and the handful of
private helpers that the manifest decoder and tests reach into directly) so
every existing ``from agentworks.config import ...`` call site keeps working
unchanged.

CRITICAL cycle note: the ``agentworks.db`` package imports ``agentworks.config.CONFIG_DIR``
at module load time, while the domain packages this package imports
(agents.template, sessions.template, vms.admin, etc.) only import
``agentworks.db`` under ``TYPE_CHECKING``. Nothing in this package may import
``agentworks.db`` at module load time either, or that cycle breaks.
"""

from __future__ import annotations

from agentworks.config.load import (
    EXPECTED_TOP_LEVEL_KEYS,
    _raise_unexpected_top_level_keys,
    load_config,
)
from agentworks.config.loaders_core import (
    _load_defaults,
    _load_operator,
    _load_paths,
    _require,
    _require_string_list,
    _warn_unexpected_keys,
)
from agentworks.config.loaders_database import (
    _load_database_config,
    load_database_config,
)
from agentworks.config.loaders_secrets import (
    _load_plugins,
    _load_secret_config,
)
from agentworks.config.loaders_sessions import (
    _load_session_config,
)
from agentworks.config.models import (
    Config,
    DatabaseConfig,
    DefaultsConfig,
    OperatorConfig,
    PathsConfig,
    SessionConfig,
    _SectionLineMap,
)
from agentworks.config.references import (
    SettingReference,
    setting_references,
    validate_setting_references,
)
from agentworks.config.validation import (
    CONFIG_DIR,
    CONFIG_PATH,
    validate_admin_username,
    validate_vm_workspaces,
)

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.errors import ConfigError as ConfigError

__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "Config",
    "ConfigError",
    "DatabaseConfig",
    "DefaultsConfig",
    "EXPECTED_TOP_LEVEL_KEYS",
    "OperatorConfig",
    "PathsConfig",
    "SessionConfig",
    "SettingReference",
    "_SectionLineMap",
    "_load_defaults",
    "_load_database_config",
    "_load_operator",
    "_load_paths",
    "_load_plugins",
    "_load_secret_config",
    "_load_session_config",
    "_require",
    "_require_string_list",
    "_warn_unexpected_keys",
    "_raise_unexpected_top_level_keys",
    "load_config",
    "load_database_config",
    "setting_references",
    "validate_admin_username",
    "validate_setting_references",
    "validate_vm_workspaces",
]
