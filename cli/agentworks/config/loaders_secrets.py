"""Secrets-related settings loaders: the deprecated ``[secret_backends.*]``
no-op sections, ``[secret_config]``, and ``[plugins]``.

The ``[secrets.*]`` resource loader (``_load_secrets``) and the aggregated
deprecated-TOML-resource-section warning relocated when config.toml stopped
declaring resources (ADR 0022): ``_load_secrets`` moved to
``agentworks.migrate.toml_resources``, and the warning became a raising
check (``_raise_for_resource_sections``) in ``agentworks.config.load``.

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


def _load_secret_backends(
    data: dict[str, object],
    deprecations: list[str],
) -> tuple[str, ...]:
    """Warn ``[secret_backends.*]`` sections as deprecated no-ops.

    The backend-keyed TOML sections never carried configuration (only
    the backend name itself), and the backends are registered code
    capabilities -- so a section here is semantically empty. Known
    backends warn as deprecated; unknown ones (typo ``envvar`` for
    ``env-var``) stay a hard ``ConfigError`` for typo protection.
    Nothing is stored and nothing publishes.

    Returns the display shapes of the sections found (facts for
    surfaces that render their own tidy rows, mirroring
    ``_warn_deprecated_resource_sections``).
    """
    raw = data.get("secret_backends", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secret_backends] must be a table")

    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    known_backends = set(SECRET_BACKEND_REGISTRY)
    found: list[str] = []
    for key, bdata in raw.items():
        backend_str = str(key)
        if not isinstance(bdata, dict):
            raise ConfigError(f"secret_backends.{backend_str} must be a table")
        if backend_str not in known_backends:
            raise ConfigError(
                f"[secret_backends.{backend_str}] names an unknown secret backend; supported: {sorted(known_backends)}"
            )
        found.append(f"[secret_backends.{backend_str}]")
        deprecations.append(
            f"[secret_backends.{backend_str}] is deprecated and has no effect: "
            f"the built-in backends ship with agentworks, and activation is "
            f"[secret_config].backends. Remove the section, or run "
            f"`agw resource migrate --all` to drop it."
        )
    return tuple(found)


def _load_secret_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> SecretConfig:
    """Load [secret_config] with the enabled-backends precedence list.

    Absence of the [secret_config] table OR absence of the ``backends`` key
    within it falls back to ``SecretConfig()``'s default chain
    (``DEFAULT_BACKEND_CHAIN``). An explicit ``backends = []`` is respected
    as "no backends" (operator opts out of resolution entirely).
    """
    declared_at = decls.lookup("secret_config")
    if "secret_config" not in data:
        return SecretConfig(declared_at=declared_at)
    raw = data["secret_config"]
    if not isinstance(raw, dict):
        raise ConfigError("[secret_config] must be a table")
    _warn_unexpected_keys(raw, {"backends"}, "secret_config", issues)
    if "backends" not in raw:
        return SecretConfig(declared_at=declared_at)
    backends_raw = raw["backends"]
    if not isinstance(backends_raw, list) or not all(isinstance(b, str) for b in backends_raw):
        raise ConfigError("[secret_config].backends must be a list of strings")
    return SecretConfig(backends=tuple(backends_raw), declared_at=declared_at)


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


# Secret resolution lives in ``agentworks.secrets.resolve`` (ADR 0016):
# the chain can name manifest-declared backends, which are unknowable at
# config-load time, so the chain-name and unreachable-secret checks run
# at the composition boundary instead of here.
