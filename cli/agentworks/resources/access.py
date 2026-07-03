"""Typed accessors for the handful of Registry read shapes consumers use.

The Registry's generic surface (``lookup`` / ``iter_kind`` /
``iter_kind_items``) is deliberately untyped (kinds are diverse types).
Consumers overwhelmingly want a few concrete shapes; centralizing them
here keeps kind-string literals in one place and call sites readable.

These accessors are the Phase 1 repoint target: every read that used to
be a ``Config`` resource attribute goes through here (or through a
template resolver that does). ``Config.publish_to`` and
``catalog.publish_to`` remain the only direct readers of Config resource
attributes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agentworks.config import (
        AdminConfig,
        GitCredentialConfig,
        NamedConsoleConfig,
    )
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


def kind_dict(registry: Registry, kind: str) -> dict[str, Any]:
    """All rows of one kind as an insertion-ordered name -> resource dict.

    The shape the template resolvers' ``resolve_from_dict`` consume.
    """
    return dict(registry.iter_kind_items(kind))


def admin_template(registry: Registry) -> AdminConfig:
    """The single ``admin-template`` row (reserved name ``default``).

    ``lookup`` raises ``KeyError`` on a miss; the always-materialize
    pre-step guarantees this row exists after ``finalize``, so a miss
    here means the registry didn't come from ``build_registry``.
    """
    return cast("AdminConfig", registry.lookup("admin-template", "default"))


def named_console_template(registry: Registry) -> NamedConsoleConfig:
    """The single ``named-console-template`` row (reserved name
    ``default``). Same always-materialize guarantee as
    ``admin_template``.
    """
    return cast(
        "NamedConsoleConfig", registry.lookup("named-console-template", "default")
    )


def git_credential(registry: Registry, name: str) -> GitCredentialConfig | None:
    """One git credential entry by name, or None when undeclared.

    ``Registry.lookup`` raises ``KeyError`` on a miss; this accessor is
    the None-returning form so callers can raise their own typed errors
    (``NotFoundError`` / ``ConfigError``) for operator-typed names.
    """
    try:
        return cast("GitCredentialConfig", registry.lookup("git-credential", name))
    except KeyError:
        return None


def secret_decls(registry: Registry) -> dict[str, SecretDecl]:
    """All declared secrets (operator- and auto-declared) by name."""
    return dict(registry.iter_kind_items("secret"))
