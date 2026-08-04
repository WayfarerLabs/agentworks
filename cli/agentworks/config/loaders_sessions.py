"""Session-related settings/resource loaders: ``[session.config]`` and
``[session_templates.*]``, including legacy flat fields hoisted into the
shell harness integration's config.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _parse_env_table, _require_string_list, _warn_unexpected_keys
from agentworks.config.models import SessionConfig
from agentworks.errors import ConfigError
from agentworks.sessions.template import SessionTemplate

if TYPE_CHECKING:
    from agentworks.config.models import _SectionLineMap
    from agentworks.env import EnvEntry

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


# The legacy flat fields (``shell``'s config vocabulary) plus the canonical
# harness-integration pair and its deprecated aliases. The flat fields keep
# loading verbatim; the loader hoists them into the canonical
# ``harness_integration = "shell"`` plus ``harness_integration_config`` shape.
_SESSION_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "harness",
    "harness_config",
    "harness_integration",
    "harness_integration_config",
    "command",
    "resume_command",
    "restart_command",
    "required_commands",
    "env",
}
_SHELL_FLAT_FIELDS = ("command", "resume_command", "restart_command", "required_commands")


def _load_session_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
    deprecated_harness_selectors: list[str] | None = None,
    deprecated_restart_commands: list[str] | None = None,
) -> dict[str, SessionTemplate]:
    raw = data.get("session_templates", {})
    if not isinstance(raw, dict):
        raise ConfigError("[session_templates] must be a table")

    templates: dict[str, SessionTemplate] = {}
    for name, tdata in raw.items():
        if not isinstance(tdata, dict):
            raise ConfigError(f"session_templates.{name} must be a table")
        _warn_unexpected_keys(tdata, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}", issues)
        env: dict[str, EnvEntry] | None = None
        if "env" in tdata:
            env = _parse_env_table(
                tdata["env"],
                context=f"session_templates.{name}",
                issues=issues,
            )
        harness_integration, harness_integration_config, used_old_selector = _session_harness_integration_pair(
            name, tdata
        )
        uses_restart_command = _uses_restart_command(harness_integration, harness_integration_config)
        if uses_restart_command:
            if deprecated_restart_commands is not None:
                deprecated_restart_commands.append(f"session-template/{name}")
            assert harness_integration_config is not None
            harness_integration_config["resume_command"] = harness_integration_config.pop("restart_command")
        if used_old_selector and deprecated_harness_selectors is not None:
            deprecated_harness_selectors.append(f"session-template/{name}")
        templates[name] = SessionTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            harness_integration=harness_integration,
            harness_integration_config=harness_integration_config,
            restart_command_compat=uses_restart_command,
            env=env,
            declared_at=decls.lookup("session_templates", name),
        )

    # Inherits-reference validation and cycle detection live in the
    # framework (SessionTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The sessions/templates.py
    # resolver also has its own visited-set guard.
    return templates


def _session_harness_integration_pair(
    name: str, tdata: dict[str, object]
) -> tuple[str | None, dict[str, object] | None, bool]:
    """Resolve a TOML session-template's harness-integration selector/config pair.

    The deprecated literals are ``harness`` and ``harness_config``. Legacy flat fields are hoisted
    onto the ``shell`` harness integration. ``None`` on either result means "not declared here".

    The flat form is the lone TOML divergence from the YAML shape; it
    nests into the blob at this boundary, mirroring how the
    git-credential loader nests ``org`` into ``provider_config``. The
    two conflict cases (flat + a non-``shell`` integration, flat + an
    explicit integration config) are load errors: the flat fields ARE
    ``shell``'s config, and mixing spellings in one declaration has no
    operator payoff.
    """
    old_fields = {"harness", "harness_config"} & set(tdata)
    new_fields = {"harness_integration", "harness_integration_config"} & set(tdata)
    if old_fields and new_fields:
        names = ", ".join(sorted(old_fields | new_fields))
        raise ConfigError(
            f"session_templates.{name}: old and new harness integration selector/config fields cannot be mixed: "
            f"{names}; "
            "use harness_integration and harness_integration_config only"
        )
    old = bool(old_fields)
    selector = "harness" if old else "harness_integration"
    config_selector = "harness_config" if old else "harness_integration_config"
    flat_present = [key for key in _SHELL_FLAT_FIELDS if key in tdata]
    harness_integration_val = tdata.get(selector)
    if harness_integration_val is not None and not isinstance(harness_integration_val, str):
        raise ConfigError(f"session_templates.{name}.{selector} must be a string")

    if flat_present:
        if "resume_command" in tdata and "restart_command" in tdata:
            raise ConfigError(
                f"session_templates.{name}: resume_command and restart_command cannot be combined; "
                "use resume_command only"
            )
        if harness_integration_val is not None and harness_integration_val != "shell":
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} configure the 'shell' harness integration "
                f"and cannot combine with {selector} = {harness_integration_val!r}; put "
                f"the workload under [session_templates.{name}.{config_selector}]"
            )
        if config_selector in tdata:
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} cannot combine with an explicit "
                f"{config_selector} table (one spelling per declaration); put "
                f"the commands under {config_selector} instead"
            )
        blob: dict[str, object] = {}
        if "command" in tdata:
            blob["command"] = str(tdata["command"])
        if "resume_command" in tdata:
            blob["resume_command"] = str(tdata["resume_command"])
        if "restart_command" in tdata:
            blob["restart_command"] = str(tdata["restart_command"])
        if "required_commands" in tdata:
            blob["required_commands"] = _require_string_list(tdata, "required_commands", f"session_templates.{name}")
        harness_integration: str | None = "shell"
        harness_integration_config: dict[str, object] | None = blob
    else:
        harness_integration = harness_integration_val
        harness_integration_config = None
        if config_selector in tdata:
            raw_config = tdata[config_selector]
            if not isinstance(raw_config, dict):
                raise ConfigError(f"session_templates.{name}.{config_selector} must be a table")
            harness_integration_config = dict(raw_config)
        if harness_integration is None and harness_integration_config is not None:
            raise ConfigError(
                f"session_templates.{name}: {config_selector} needs a selector "
                f'(a blob with no owner); add {selector} = "..."'
            )

    # The declared or hoisted harness-integration config blob's shape is validated by
    # the finalize ``validate`` pass (SessionTemplate.validate), not
    # here: capability validation is decoupled from load (R3). The
    # TOML-shape checks above (flat-vs-nested, blob-needs-integration) stay
    # at load, in the operator's TOML vocabulary.
    return harness_integration, harness_integration_config, old


def _uses_restart_command(integration: str | None, config: dict[str, object] | None) -> bool:
    """Return whether a shell declaration uses the compatibility spelling."""
    return integration == "shell" and config is not None and "restart_command" in config
