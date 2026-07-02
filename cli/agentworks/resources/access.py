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

    Always present after ``finalize`` (always-materialize guarantees the
    reserved name), so this never returns None.
    """
    row = registry.lookup("admin-template", "default")
    assert row is not None, "admin-template:default missing after finalize"
    return cast("AdminConfig", row)


def named_console_template(registry: Registry) -> NamedConsoleConfig:
    """The single ``named-console-template`` row (reserved name ``default``)."""
    row = registry.lookup("named-console-template", "default")
    assert row is not None, "named-console-template:default missing after finalize"
    return cast("NamedConsoleConfig", row)


def git_credential(registry: Registry, name: str) -> GitCredentialConfig | None:
    """One git credential entry by name, or None when undeclared."""
    return cast("GitCredentialConfig | None", registry.lookup("git-credential", name))


def secret_decls(registry: Registry) -> dict[str, SecretDecl]:
    """All declared secrets (operator- and auto-declared) by name."""
    return dict(registry.iter_kind_items("secret"))
