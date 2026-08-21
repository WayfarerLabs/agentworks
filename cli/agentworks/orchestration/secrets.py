"""The orchestrator's secret path: union, central preview, scoped delivery.

Replaces the per-instance bound resolver's orchestration-shaped jobs:
the union of a command's secrets comes from the plan's
declared ``secret_refs`` (not construct-time registration), and
resolution preview is computed centrally over declarations (not
by each instance, and not by the nodes that declare them either).
Preflight uses provider-aware preview at zero operator impact. A backend
therefore goes as far as it can without operator action and can return an
indeterminate result when that limit prevents a definitive answer.

WHO previews is load-bearing, not incidental. Resolvability is a
property of the operation's runtime world, so the OPERATION asks: the
preflight sweep (:func:`~agentworks.orchestration.readiness
.preflight_all`) runs :func:`require_predicted_refs` per node. A node
asks only :func:`require_declared_refs`, whether its declarations point
at rows that exist, which is registry consistency and genuinely its
own. Doctor, which invokes node preflight per row without a sweep,
lands on the right side of that split for free: it reports resolvability
once, on the secret's own row, instead of smearing it across every
resource that names the secret.

Resolution itself remains one typed batch at the preflight boundary,
owned by :class:`~agentworks.secrets.resolver.Resolver`. What this module
adds downstream of it is SCOPED DELIVERY:
:class:`ScopedSecrets`, the ``ctx.secret(name)`` view that hands a
node only the secret names it declared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agentworks.capabilities.secret_backend import TtyInteractionAccess
    from agentworks.config import Config
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.preview import ResolutionPreview
    from agentworks.secrets.resolve import ActiveSource

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


def secret_declarations(names: Iterable[str], registry: Registry) -> tuple[SecretDecl, ...]:
    """Declarations for ``names``, from the registry's ``secret`` rows.

    A name with no registry row falls back to a synthesized bare
    declaration. This keeps sources callable for well-known names that no
    manifest references, matching the fallback ``Resolver.register_name``
    applies.
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
    decls: Iterable[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    tty_access: TtyInteractionAccess,
) -> dict[str, ResolutionPreview]:
    """Preview declared references once at the fixed preflight impact."""
    from agentworks.capabilities.secret_backend import OperatorImpact, TtyInteractionAccess
    from agentworks.secrets.preview import preview_batch

    if type(tty_access) is not TtyInteractionAccess:
        raise StateError("tty_access must be an exact TtyInteractionAccess")
    declarations = tuple(decls)
    return preview_batch(
        declarations,
        sources,
        impact=OperatorImpact.NONE,
        tty_access=tty_access,
        interaction_broker=None,
    )


def require_declared_refs(owner: str, refs: Iterable[ResourceReference], registry: Registry) -> None:
    """Reference INTACTNESS: every declared secret reference must name a
    row that actually exists in the registry.

    This is registry consistency, and it IS the holding node's concern:
    the node's own config named these secrets, so a name that reaches no
    row means the node's declarations and the registry disagree. Nodes
    call it from ``preflight``.

    It deliberately says nothing about whether a declared secret would
    RESOLVE, which is the operation's concern and lives in the preflight
    sweep (:func:`~agentworks.orchestration.readiness.preflight_all`).
    Doctor, which invokes node preflight per row and never runs a sweep,
    therefore gets the intactness check and not the prediction, which is
    exactly right: an operator's secret being prompt-only makes a site
    row no less healthy.

    A dangling reference is normally unreachable (a referenced secret is
    auto-declared at finalize), so this catches the one case that is
    not: the readiness-gated materialization pass (R12) leaves a
    not-ready or disabled node's secrets unmaterialized on purpose.
    Reaching preflight in that state means something upstream let a
    not-ready resource through, and a clear typed error beats the
    ``KeyError`` or the silently-synthesized bare declaration that would
    otherwise follow.
    """
    from agentworks.errors import ConfigError
    from agentworks.secrets.kinds import SECRET_KIND_NAME

    for ref in refs:
        try:
            registry.lookup(SECRET_KIND_NAME, ref.name)
        except KeyError:
            raise ConfigError(
                f"{owner}: declared secret '{ref.name}' ({ref.usage}) has no declaration in the registry",
                hint=(
                    "A referenced secret is normally auto-declared, so this "
                    "usually means the declaring resource is not ready and its "
                    "secrets were never materialized. `agw doctor` shows the "
                    "resource's readiness."
                ),
            ) from None


def require_predicted_refs(
    owner: str,
    refs: Iterable[ResourceReference],
    config: Config | None,
    registry: Registry,
    *,
    tty_access: TtyInteractionAccess,
    preview_memo: dict[str, ResolutionPreview] | None = None,
    sources: Sequence[ActiveSource] | None = None,
) -> None:
    """Value-free, no-impact screen over one node's declared config secrets.

    Invoked by the preflight sweep
    (:func:`~agentworks.orchestration.readiness.preflight_all`), once
    per node, NOT by the nodes themselves. Whether a declared secret can
    be attempted is a property of the operation's runtime world (the
    active source chain and exact interaction policy), not of the resource
    that named it, so the resource must not assume the concern. The
    practical consequence is doctor: it invokes node preflight per row
    and never sweeps, so it never predicts, and a prompt-only secret
    leaves a site row healthy while the Secrets group reports on the
    secret itself.

    ``owner`` is the node's ``<kind>/<name>`` key, which IS the owner
    display of the instance whose config named the secret, so the error
    keeps the owner/usage framing the per-instance prediction produced.
    """
    from agentworks.capabilities.secret_backend import PreviewIndeterminate
    from agentworks.errors import ConfigError
    from agentworks.secrets.preview import PreviewStatus
    from agentworks.secrets.resolve import active_sources

    refs = tuple(refs)
    if not refs:
        return
    if config is None:
        raise ConfigError(
            f"{owner}: cannot predict declared secret resolvability "
            f"without config on the context (assembled for inspection?)"
        )
    memo = {} if preview_memo is None else preview_memo
    missing_names = tuple(dict.fromkeys(ref.name for ref in refs if ref.name not in memo))
    if missing_names:
        predictions = predict_resolution(
            secret_declarations(missing_names, registry),
            active_sources(config, registry) if sources is None else sources,
            tty_access=tty_access,
        )
        memo.update(predictions)
    for ref in refs:
        prediction = memo[ref.name]
        acceptable_failure = prediction.status is PreviewStatus.FAILED and any(
            isinstance(attempt.result, PreviewIndeterminate) for attempt in prediction.attempts[:-1]
        )
        if prediction.status not in {PreviewStatus.AVAILABLE, PreviewStatus.INDETERMINATE} and not acceptable_failure:
            raise ConfigError(
                f"{owner}: secret '{ref.name}' ({ref.usage}) cannot pass preflight",
                hint=(
                    f"preview status: {prediction.status.value}"
                    f"{f'/{prediction.reason}' if prediction.reason is not None else ''}. "
                    f"Run `agw secret describe {ref.name}` for details."
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
