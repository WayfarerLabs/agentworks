"""Session-related settings loader: ``[session.config]``.

The ``[session_templates.*]`` resource loader (``_load_session_templates``
and its ``_session_harness_integration_pair`` helper) is gone: config.toml
stopped declaring resources (ADR 0022), so only the settings loader
remains here. A session template is a YAML manifest whose
``harness_integration`` is one tagged table, which needs no hoist.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from agentworks.config.loaders_core import _warn_unexpected_keys
from agentworks.config.models import SessionConfig
from agentworks.errors import ConfigError

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
