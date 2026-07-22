"""Session-related settings/resource loaders: ``[session.config]`` and
``[session_templates.*]``, including the legacy flat-field-to-harness
hoisting logic.

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


# The legacy flat fields (``shell``'s config vocabulary) plus the
# harness pair. The flat fields keep loading verbatim; the loader hoists
# them into ``harness = "shell"`` + the equivalent ``harness_config``
# blob (FRD R6), so the internal representation follows the YAML shape.
_SESSION_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "harness",
    "harness_config",
    "command",
    "restart_command",
    "required_commands",
    "env",
}
_SHELL_FLAT_FIELDS = ("command", "restart_command", "required_commands")


def _load_session_templates(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
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
        harness, harness_config = _session_harness_pair(name, tdata)
        templates[name] = SessionTemplate(
            name=name,
            inherits=list(tdata.get("inherits", [])),
            description=str(tdata["description"]) if "description" in tdata else None,
            harness=harness,
            harness_config=harness_config,
            env=env,
            declared_at=decls.lookup("session_templates", name),
        )

    # Inherits-reference validation and cycle detection live in the
    # framework (SessionTemplateKind's miss policy +
    # Registry.finalize's cycle pass). The sessions/templates.py
    # resolver also has its own visited-set guard.
    return templates


def _session_harness_pair(name: str, tdata: dict[str, object]) -> tuple[str | None, dict[str, object] | None]:
    """Resolve a TOML session-template's ``(harness, harness_config)``
    pair, hoisting the legacy flat fields onto the ``shell`` harness
    (FRD R6). ``None`` on either means "not declared here".

    The flat form is the lone TOML divergence from the YAML shape; it
    nests into the blob at this boundary, mirroring how the
    git-credential loader nests ``org`` into ``provider_config``. The
    two conflict cases (flat + a non-``shell`` harness, flat + an
    explicit ``harness_config``) are load errors: the flat fields ARE
    ``shell``'s config, and mixing spellings in one declaration has no
    operator payoff.
    """
    flat_present = [key for key in _SHELL_FLAT_FIELDS if key in tdata]
    harness_val = tdata.get("harness")
    if harness_val is not None and not isinstance(harness_val, str):
        raise ConfigError(f"session_templates.{name}.harness must be a string")

    if flat_present:
        if harness_val is not None and harness_val != "shell":
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} configure the 'shell' harness "
                f"and cannot combine with harness = {harness_val!r}; put "
                f"the workload under [session_templates.{name}.harness_config]"
            )
        if "harness_config" in tdata:
            raise ConfigError(
                f"session_templates.{name}: the legacy field(s) "
                f"{', '.join(flat_present)} cannot combine with an explicit "
                f"harness_config table (one spelling per declaration); put "
                f"the commands under harness_config instead"
            )
        blob: dict[str, object] = {}
        if "command" in tdata:
            blob["command"] = str(tdata["command"])
        if "restart_command" in tdata:
            blob["restart_command"] = str(tdata["restart_command"])
        if "required_commands" in tdata:
            blob["required_commands"] = _require_string_list(tdata, "required_commands", f"session_templates.{name}")
        harness: str | None = "shell"
        harness_config: dict[str, object] | None = blob
    else:
        harness = harness_val
        harness_config = None
        if "harness_config" in tdata:
            raw_config = tdata["harness_config"]
            if not isinstance(raw_config, dict):
                raise ConfigError(f"session_templates.{name}.harness_config must be a table")
            harness_config = dict(raw_config)
        if harness is None and harness_config is not None:
            raise ConfigError(
                f'session_templates.{name}: harness_config needs a harness (a blob with no owner); add harness = "..."'
            )

    # Shape-and-vocabulary validation on the declared/hoisted blob, in
    # the operator's TOML vocabulary (harness-api-lld section 2). Unknown
    # harness names skip: the kind's miss policy reports them at finalize.
    if isinstance(harness, str) and harness_config is not None:
        from agentworks.capabilities.harness import HARNESS_REGISTRY

        capability = HARNESS_REGISTRY.get(harness)
        if capability is not None:
            capability.validate_config(f"session-template/{name}", harness_config)
    return harness, harness_config
