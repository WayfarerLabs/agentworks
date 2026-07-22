"""Agentworks configuration loading and validation.

Config lives at ~/.config/agentworks/config.toml. It is read-only at runtime.

This package holds the settings dataclasses plus the legacy TOML
resource loaders/publisher (``Config.publish_to`` and the ``_load_*``
helpers) slated for removal; nothing else. The declarable-resource
dataclasses (VMTemplate,
AgentTemplate, AdminConfig, WorkspaceTemplate, SessionTemplate,
NamedConsoleConfig, GitCredentialConfig) and the console/tmux layout
constants now live in their domain packages; the loaders import them from
there. Kind definitions live in the domain packages too (see
``agentworks.resources.kinds`` for the registration index).

It was split out of a single ~1600-line ``config.py`` module into this
package, one submodule per cohesive concern, while keeping the public
import path ``agentworks.config`` unchanged:

- ``validation``: ``CONFIG_DIR`` / ``CONFIG_PATH`` and the name/username
  validators. Has no dependency on any sibling submodule.
- ``models``: the settings dataclasses, the ``Config`` object (and its
  ``publish_to``), and ``_SectionLineMap``.
- ``loaders_core``: generic TOML-loading helpers plus the
  ``[operator]`` / ``[paths]`` / ``[defaults]`` settings loaders and the
  ``[git_credentials]`` resource loader.
- ``loaders_resources``: the remaining TOML resource-section loaders
  (named console, VM/agent/workspace templates, admin config, apt/install
  sections, legacy vm-site sections).
- ``loaders_sessions``: ``[session.config]`` and ``[session_templates.*]``,
  including the legacy flat-field-to-harness hoisting.
- ``loaders_secrets``: ``[secrets.*]``, ``[secret_backends.*]``, the
  aggregated deprecated-TOML-section warning, and ``[secret_config]``.
- ``load``: the ``load_config`` entry point that drives the above.

This ``__init__.py`` re-exports the public surface (and the handful of
private ``_load_*`` helpers that the manifest decoder and tests reach into
directly) so every existing ``from agentworks.config import ...`` call site
keeps working unchanged.

CRITICAL cycle note: ``agentworks/db.py`` imports ``agentworks.config.CONFIG_DIR``
at module load time, while the domain packages this package imports
(agents.template, sessions.template, vms.admin, etc.) only import
``agentworks.db`` under ``TYPE_CHECKING``. Nothing in this package may import
``agentworks.db`` at module load time either, or that cycle breaks.
"""

from __future__ import annotations

from agentworks.config.load import (
    EXPECTED_TOP_LEVEL_KEYS,
    _warn_unexpected_top_level_keys,
    load_config,
)
from agentworks.config.loaders_core import (
    _load_defaults,
    _load_git_credentials,
    _load_operator,
    _load_paths,
    _parse_env_table,
    _require,
    _require_string_list,
    _warn_unexpected_keys,
)
from agentworks.config.loaders_resources import (
    _LEGACY_SITE_SECTIONS,
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
from agentworks.config.loaders_sessions import (
    _load_session_config,
    _load_session_templates,
    _session_harness_pair,
)
from agentworks.config.models import (
    Config,
    DefaultsConfig,
    OperatorConfig,
    PathsConfig,
    SessionConfig,
    UserConfig,
    _SectionLineMap,
)
from agentworks.config.validation import (
    CONFIG_DIR,
    CONFIG_PATH,
    MAX_NAME_LENGTH,
    validate_admin_username,
    validate_name,
)

# ConfigError is defined in agentworks.errors and re-exported here for backward
# compatibility with existing `from agentworks.config import ConfigError` users.
# The `X as X` shape marks the name as an explicit re-export for mypy strict mode.
from agentworks.errors import ConfigError as ConfigError

__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "MAX_NAME_LENGTH",
    "Config",
    "ConfigError",
    "DefaultsConfig",
    "EXPECTED_TOP_LEVEL_KEYS",
    "OperatorConfig",
    "PathsConfig",
    "SessionConfig",
    "UserConfig",
    "_LEGACY_SITE_SECTIONS",
    "_SectionLineMap",
    "_load_admin_config",
    "_load_agent_templates",
    "_load_apt_and_install_sections",
    "_load_defaults",
    "_load_git_credentials",
    "_load_named_console",
    "_load_operator",
    "_load_paths",
    "_load_secret_backends",
    "_load_secret_config",
    "_load_secrets",
    "_load_session_config",
    "_load_session_templates",
    "_load_vm_sites_legacy",
    "_load_vm_templates",
    "_load_workspace_templates",
    "_parse_env_table",
    "_require",
    "_require_string_list",
    "_session_harness_pair",
    "_warn_deprecated_resource_sections",
    "_warn_unexpected_keys",
    "_warn_unexpected_top_level_keys",
    "load_config",
    "validate_admin_username",
    "validate_name",
]
