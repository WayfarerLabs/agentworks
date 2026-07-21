"""The orchestrator's secret path: union, central prediction, scoped
delivery.

Replaces the per-instance bound resolver's orchestration-shaped jobs:
the union of a command's secrets comes from the plan's
declared ``secret_refs`` (not construct-time registration), and
resolvability prediction is computed centrally over declarations (not
by each instance). Prediction is
:func:`~agentworks.secrets.resolve.preview_resolution` applied per
declaration (a prompt backend is reported without probing; probing
would BE the prompt), with the preflight caller gating the interactive
answer on ``output.is_interactive()`` so a prompt-only secret fails
fast at preflight under ``--non-interactive`` rather than at resolve
end (issue #202). Doctor's all-resources sweep and a command's union
are two callers of the same computation, which is why the prediction
helper takes declarations, not a walk.

Resolution itself is untouched here: the single resolve pass at the
preflight boundary stays :class:`~agentworks.secrets.resolver
.Resolver` / :func:`~agentworks.secrets.resolve.resolve_secrets`
machinery. What this module adds downstream of it is SCOPED DELIVERY:
:class:`ScopedSecrets`, the ``ctx.secret(name)`` view that hands a
node only the secret names it declared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentworks.config import Config
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.resolve import ActiveBackend

    from .node import Node


def secret_union(nodes: Iterable[Node]) -> tuple[str, ...]:
    """The union of secret names the plan's nodes declare: what the
    single resolve pass must cover.

    Central by design (no instance registers itself anywhere), deduped,
    in first-encounter order over ``nodes`` (normally a walk's output),
    so prompting order is deterministic.
    """
    seen: set[str] = set()
    out: list[str] = []
    for node in nodes:
        for name in node.secret_refs():
            if name not in seen:
                seen.add(name)
                out.append(name)
    return tuple(out)


def secret_declarations(
    names: Iterable[str], registry: Registry
) -> tuple[SecretDecl, ...]:
    """Declarations for ``names``, from the registry's ``secret`` rows.

    A name with no registry row falls back to a synthesized bare
    declaration: an operator who omits every ``[secrets.*]`` section
    leaves the registry empty under the ``secret`` kind, and the
    backend chain must stay callable for the well-known names (the
    same fallback ``Resolver.register_name`` applies).
    """
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.kinds import SECRET_KIND_NAME

    out: list[SecretDecl] = []
    for name in names:
        try:
            found: SecretDecl = registry.lookup(SECRET_KIND_NAME, name)
        except KeyError:
            found = SecretDecl(name=name, description="")
        out.append(found)
    return tuple(out)


def predict_resolution(
    decls: Iterable[SecretDecl], backends: list[ActiveBackend]
) -> dict[str, str | None]:
    """Central resolvability prediction over declared references: for
    each declaration, the name of the first active backend that would
    resolve it, or ``None`` when nothing would.

    :func:`~agentworks.secrets.resolve.preview_resolution` per
    declaration; the semantics (non-prompting, a non-interactive backend
    must actually produce a value) are that function's. This is the
    PREFLIGHT prediction, so it must match resolve-time reality: an
    interactive backend counts as resolving only when interactive input
    is actually available this run (``output.is_interactive()``), so a
    prompt-only secret fails fast at preflight under ``--non-interactive``
    rather than reaching a resolve-end failure (issue #202).
    """
    from agentworks import output
    from agentworks.secrets.resolve import preview_resolution

    interactive_available = output.is_interactive()
    return {
        decl.name: preview_resolution(
            decl, backends, interactive_available=interactive_available
        )
        for decl in decls
    }


def require_predicted_refs(
    owner: str,
    refs: Iterable[ResourceReference],
    config: Config | None,
    registry: Registry,
) -> None:
    """The node-preflight half of central prediction: every declared
    secret reference must be predicted resolvable by some active
    backend, without prompting (an unresolvable secret is fatal and
    knowable pre-resolve; a prompt-only secret's value check defers
    past preflight). The holding node runs this for its held
    instance's declared config secrets, with the same owner/usage
    error framing the per-instance prediction produced; ``owner`` is
    the node's ``<kind>/<name>`` key, which IS the instance's owner
    display.
    """
    from agentworks.errors import ConfigError
    from agentworks.secrets.resolve import active_backends

    refs = tuple(refs)
    if not refs:
        return
    if config is None:
        raise ConfigError(
            f"{owner}: cannot predict declared secret resolvability "
            f"without config on the context (assembled for inspection?)"
        )
    predictions = predict_resolution(
        secret_declarations((ref.name for ref in refs), registry),
        active_backends(config, registry),
    )
    for ref in refs:
        if predictions[ref.name] is None:
            raise ConfigError(
                f"{owner}: secret '{ref.name}' ({ref.usage}) is not "
                f"resolvable by any active backend",
                hint=(
                    f"`agw secret describe {ref.name}` shows how each "
                    "backend looks the secret up; add a backend mapping "
                    "or extend [secret_config].backends."
                ),
            )


class ScopedSecrets:
    """Scoped secret delivery: a read-only view over the
    operation's resolved values, restricted to one node's declared
    names.

    Satisfies the ``SecretReader`` protocol, so it drops into
    ``RunContext`` where the whole-cache reader goes today. The
    orchestrator assembles one per node invocation from the boundary
    pass's resolved mapping and the node's ``secret_refs()``; the node
    then cannot read a secret it did not declare, which is what keeps
    the declare/receive contract honest end to end.
    """

    def __init__(self, values: Mapping[str, str], names: Iterable[str]) -> None:
        self._values = values
        self._names = frozenset(names)

    def get(self, name: str) -> str:
        if name not in self._names:
            raise StateError(
                f"secret {name!r} was not declared by this node, so it "
                f"is not delivered to it. Nodes receive only the "
                f"secrets their declared references name (the "
                f"declare/receive contract); declare it, or read it "
                f"from the node that does."
            )
        try:
            return self._values[name]
        except KeyError:
            raise StateError(
                f"secret {name!r} is declared but was not resolved by "
                f"the operation's boundary pass. The orchestrator "
                f"resolves the plan's whole union before delivery; "
                f"reaching here means the union missed this name."
            ) from None
