"""Typed accessors for the handful of Registry read shapes consumers use.

The Registry's generic surface (``lookup`` / ``iter_kind`` /
``iter_kind_items``) is deliberately untyped (kinds are diverse types).
Consumers overwhelmingly want a few concrete shapes; centralizing them
here keeps kind-string literals in one place and call sites readable.

These accessors centralize resource reads: every read that used to
be a ``Config`` resource attribute goes through here (or through a
template resolver that does). The ``Config`` resource fields and the
operator publishers that once read them are gone (ADR 0022); resources
live only in the Registry now.
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


def ensure_reference_enabled(registry: Registry, kind: str, name: str) -> None:
    """The single-row use-gate for a declarable reference (Phase 7, LLD b's
    named-row rule), mirroring ``ensure_harness_integration_enabled``.

    A present-but-disabled declarable row (a not-enabled plugin's bundled
    manifest resource) resolves cleanly as a reference (it is present, so not an
    unknown-name miss), so a consumer that fetches it by name and acts on it
    must consult enablement FIRST. Returns unless
    ``enablement_of(kind, name)`` is ``disabled``; on disabled, raises a typed
    ``StateError`` whose tail derives the plugin from the row's ``system-plugin``
    origin (``registry.lookup(...).origin.plugin``), falling back to
    ``enable its unit`` for a non-plugin disabled row (a future
    operator-explicit-disable source, R13). Tolerates a missing node
    (``enablement_of`` returns ``enabled``), so it is a safe no-op for an
    implicit ``default`` reference before the lookup can raise ``KeyError``.
    """
    from agentworks.errors import StateError
    from agentworks.resources.graph import Enablement

    if registry.graph.enablement_of(kind, name) is not Enablement.disabled:
        return
    origin = getattr(registry.lookup(kind, name), "origin", None)
    plugin = getattr(origin, "plugin", None)
    tail = f"enable plugin `{plugin}`" if plugin else "enable its unit"
    raise StateError(
        f"{kind} '{name}' is disabled; {tail}",
        entity_kind=kind,
        entity_name=name,
        hint="`agw doctor` lists each plugin's state; enable the plugin providing this resource",
    )


def ensure_recipe_enabled(registry: Registry, kind: str, name: str) -> None:
    """The recipe use-gate for a template consumption (Phase 7, LLD b).

    A template resolver merges a whole lineage into the resolved recipe, so
    gating only the named row would let a disabled plugin's
    transitively-referenced declarable leak into the acted-on recipe. This
    applies ``ensure_reference_enabled`` to the named node and to every
    DECLARABLE node the recipe is made of, refusing on the first disabled one.
    Capability nodes are deliberately EXCLUDED: each capability kind keeps its
    own R14 use-model (a platform propagates via its site, a harness
    integration is gated by ``ensure_harness_integration_enabled``, etc.), so
    the recipe gate neither duplicates nor contradicts them.

    The recipe is TWO closures, and taking their union is the whole point
    (FR17):

    - ``runtime_reachable_from``, everything the named row needs, which since
      an inheriting row publishes the needs of its MERGED declaration already
      includes everything it inherited and still uses; and
    - ``composed_from``, the ancestor template rows themselves.

    **ENABLEMENT PROPAGATES ACROSS AN INHERITANCE EDGE, and this is the one
    traversal that says so** (FR17's policy call, settled here rather than
    left to fall out): a disabled parent template is not a runtime need this
    row happens to have, it is SOURCE the resolver is about to compile in, so
    using the child means using it. What the union deliberately does NOT
    include is an ancestor's own standalone needs, because those are exactly
    the ones a child may have overridden: a child that renames the auth key
    it inherits does not use the parent's, and gating on it would refuse an
    operator over a secret nothing in the recipe reads.

    Readiness is the other half of the call and propagates nowhere: no
    template kind implements ``not_ready``, so the fold gives every template
    row a ready verdict and an inheritance edge changes nothing, which is the
    answer to keep (a base template a plugin has turned off is an enablement
    fact, and enablement is the axis that answers for it). If a future
    inheriting kind grows a ``not_ready`` hook, the fold hands it every
    out-edge's state including the inherited one and the hook decides, which
    is R4's rule and not this gate's to make in advance.

    Safe no-op for an implicit ``default`` template (a missing start node: both
    closures return empty, ``enablement_of`` reads ``enabled``) and for an
    all-enabled registry.
    """
    from agentworks.resources.kind import KIND_REGISTRY

    graph = registry.graph
    ensure_reference_enabled(registry, kind, name)
    recipe = [*graph.composed_from(kind, name), *graph.runtime_reachable_from(kind, name)]
    for dep_kind, dep_name in recipe:
        handler = KIND_REGISTRY.get(dep_kind)
        if handler is not None and handler.category == "declarable":
            ensure_reference_enabled(registry, dep_kind, dep_name)
