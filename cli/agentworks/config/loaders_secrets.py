"""Secrets-related settings loaders: ``[secret_config]`` and ``[plugins]``.

Both are genuine settings. ``[secret_config]`` decides WHICH declared sources
are active and in what order; ``[plugins]`` is the system-plugin opt-in.

Two neighbours are deliberately absent. The ``[secrets.*]`` resource loader
(``_load_secrets``) is gone: config.toml stopped declaring resources
(ADR 0022), and secrets are decoded from YAML manifests by
``agentworks.manifests.decode``. ``[secret_backends.*]`` is gone the same
way: it was a retired backend-selection row that carried no configuration.
Like every retired resource section, it is now an unexpected top-level key
instead of being half-warned and half-refused here against the built-in
backend registry.

``[secret_config].sources`` NAMES declared ``secret-source`` resources and
stays, because choosing and ordering them is configuration. Only its SHAPE is checked here:
the registry does not exist at settings-load time, so whether the names
resolve is settled after finalize, by
``agentworks.config.references.validate_setting_references``.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _warn_unexpected_keys
from agentworks.errors import ConfigError
from agentworks.secrets import SecretConfig

if TYPE_CHECKING:
    from agentworks.config.models import _SectionLineMap


def _load_secret_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> SecretConfig:
    """Load [secret_config] with the active-source precedence list.

    Absence of the [secret_config] table OR absence of the ``sources`` key
    within it falls back to ``SecretConfig()``'s default chain
    (``DEFAULT_SOURCE_CHAIN``). An explicit ``sources = []`` is respected
    as "no sources" (operator opts out of resolution entirely).
    """
    declared_at = decls.lookup("secret_config")
    if "secret_config" not in data:
        return SecretConfig(declared_at=declared_at)
    raw = data["secret_config"]
    if not isinstance(raw, dict):
        raise ConfigError("[secret_config] must be a table")
    if "backends" in raw:
        raise ConfigError(
            "[secret_config].backends was renamed to [secret_config].sources",
            hint=(
                'Rename backends to sources in config.toml. See "Secret source precedence key" '
                "in docs/guides/upgrading-to-0.14.md."
            ),
        )
    _warn_unexpected_keys(raw, {"sources"}, "secret_config", issues)
    if "sources" not in raw:
        return SecretConfig(declared_at=declared_at)
    sources_raw = raw["sources"]
    if not isinstance(sources_raw, list) or not all(isinstance(source, str) for source in sources_raw):
        raise ConfigError("[secret_config].sources must be a list of strings")
    return SecretConfig(sources=tuple(sources_raw), declared_at=declared_at)


def _load_plugins(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> tuple[str, ...]:
    """Load [plugins] with the opt-in list of enabled system plugin names (R4).

    Absence of the [plugins] table, or of the ``system`` key within it,
    means no plugins are enabled (``()``). ``issues``/``decls`` are unused
    here (there is no soft-warn path and ``enabled_system_plugins`` is a
    bare tuple with no wrapper to carry a ``declared_at``); the parameters
    are kept to match the sibling settings loader ``_load_secret_config``'s
    shape.

    ``system`` is the only recognized key. It is scoped deliberately: the
    [plugins] table is the plugin-subsystem namespace, and ``system`` names
    the enabled in-repo system plugins, leaving room for future
    external-plugin keys to slot in as siblings without ambiguity.

    Unknown keys in [plugins] are a hard ``ConfigError``, NOT a collected
    soft issue via ``_warn_unexpected_keys`` (the convention
    ``_load_secret_config`` above uses). This is a deliberate departure:
    [plugins] is an opt-in gate, so a typo'd key (``sytsem``, an old
    ``enabled`` from before the rename, or a future per-plugin key used a
    release too early) must fail loudly at load time rather than silently
    leave plugins un-enabled behind a warning the operator may miss. Do not
    "consistency-fix" this back to soft-warn.
    """
    if "plugins" not in data:
        return ()
    raw = data["plugins"]
    if not isinstance(raw, dict):
        raise ConfigError("[plugins] must be a table")
    unexpected = set(raw.keys()) - {"system"}
    if unexpected:
        keys = ", ".join(sorted(str(k) for k in unexpected))
        raise ConfigError(f"unexpected keys in [plugins]: {keys}")
    if "system" not in raw:
        return ()
    system_raw = raw["system"]
    if not isinstance(system_raw, list) or not all(isinstance(e, str) for e in system_raw):
        raise ConfigError("[plugins].system must be a list of strings")
    return tuple(system_raw)


# Neither of the two post-load secret checks lives here, because neither can:
# the chain names sources that manifests and built-ins supply, which are
# unavailable at config-load time. The chain-NAME check is the generic settings-reference
# pass (``agentworks.config.references``); the unreachable-secret check is
# ``agentworks.secrets.resolve.validate_chain`` (ADR 0016). Both run at the
# composition boundary, in that order.
