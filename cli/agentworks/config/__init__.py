"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.

This package holds the settings dataclasses and the settings-section
loaders; nothing else. config.toml is settings only now (ADR 0022): the
TOML resource loaders relocated to ``agentworks.migrate.toml_resources``
(the migrator's private oracle), so a resource-declaring section is a hard
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
- ``loaders_core``: generic TOML-loading helpers, the ``[operator]`` /
  ``[paths]`` / ``[defaults]`` settings loaders, and the two shared
  nonconforming-secret-name helpers (used by both the migrator oracle and
  the manifest decoders).
- ``loaders_sessions``: the ``[session.config]`` settings loader.
- ``loaders_secrets``: ``[secret_backends.*]`` (deprecated no-op warning),
  ``[secret_config]``, and ``[plugins]``.
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
    _parse_env_table,
    _require,
    _require_string_list,
    _warn_unexpected_keys,
)
from agentworks.config.loaders_secrets import (
    _load_plugins,
    _load_secret_backends,
    _load_secret_config,
)
from agentworks.config.loaders_sessions import (
    _load_session_config,
)
from agentworks.config.models import (
    Config,
    DefaultsConfig,
    OperatorConfig,
    PathsConfig,
    SessionConfig,
    _SectionLineMap,
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
    "DefaultsConfig",
    "EXPECTED_TOP_LEVEL_KEYS",
    "OperatorConfig",
    "PathsConfig",
    "SessionConfig",
    "_SectionLineMap",
    "_load_defaults",
    "_load_operator",
    "_load_paths",
    "_load_plugins",
    "_load_secret_backends",
    "_load_secret_config",
    "_load_session_config",
    "_parse_env_table",
    "_require",
    "_require_string_list",
    "_warn_unexpected_keys",
    "_raise_unexpected_top_level_keys",
    "load_config",
    "validate_admin_username",
    "validate_vm_workspaces",
]
