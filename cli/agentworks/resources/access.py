"""Typed accessors for the handful of Registry read shapes consumers use.

The Registry's generic surface (``lookup`` / ``iter_kind`` /
``iter_kind_items``) is deliberately untyped (kinds are diverse types).
Consumers overwhelmingly want a few concrete shapes; centralizing them
here keeps kind-string literals in one place and call sites readable.

These accessors centralize resource reads: every read that used to
be a ``Config`` resource attribute goes through here (or through a
template resolver that does). ``Config.publish_to`` and the ``apt`` /
``install_commands`` operator publishers remain the only direct readers of
Config resource attributes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agentworks.errors import unknown_template_error

if TYPE_CHECKING:
    from agentworks.git_credentials.credential import GitCredentialConfig
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.sessions.template import NamedConsoleConfig
    from agentworks.vms.admin import AdminConfig


def kind_dict(registry: Registry, kind: str) -> dict[str, Any]:
    """All rows of one kind as an insertion-ordered name -> resource dict.

    The shape the template resolvers' ``resolve_from_dict`` consume.
    """
    return dict(registry.iter_kind_items(kind))


def require_declared_template(registry: Registry, kind: str, name: str) -> None:
    """Assert that ``name`` is a declared template of ``kind``, else raise.

    This exists for ORDERING, not because the resolvers are silent on a
    miss: ``resolve_from_dict`` (the vm / workspace / agent / session
    resolvers) already raises ``unknown_template_error`` on an unknown
    non-default name. The re-point flow (``--update-template`` on ``agent
    reinit``) persists the new binding to the DB and only then resolves it,
    so the persist must be gated by an explicit up-front check; without it a
    bad name would land the re-point and the downstream resolve would raise
    on an already-mutated row.

    Shares ``unknown_template_error`` with the resolvers so all five callers
    frame a missing template the same way.
    """
    available = kind_dict(registry, kind)
    if name not in available:
        raise unknown_template_error(
            kind=kind,
            label=kind.replace("-", " "),
            name=name,
            available=available,
        )


def admin_template(registry: Registry, name: str = "default") -> AdminConfig:
    """One ``admin-template`` row by name (default: reserved ``default``).

    ``lookup`` raises ``KeyError`` on a miss. The always-materialize
    pre-step guarantees the ``default`` row exists after ``finalize``, so
    a miss on ``default`` means the registry didn't come from
    ``build_registry``. A non-default name resolves only when the
    operator declared that admin-template (via manifest); a miss there is
    an operator-typed bad name, and callers wrap the ``KeyError`` in a
    typed error naming the selector.
    """
    return cast("AdminConfig", registry.lookup("admin-template", name))


def named_console_template(registry: Registry) -> NamedConsoleConfig:
    """The single ``named-console-template`` row (reserved name
    ``default``). Same always-materialize guarantee as
    ``admin_template``.
    """
    return cast("NamedConsoleConfig", registry.lookup("named-console-template", "default"))


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
