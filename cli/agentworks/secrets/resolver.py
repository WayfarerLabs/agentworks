"""The per-operation secret resolver: the handle a capability instance
is constructed against.

One ``Resolver`` per service-layer operation. Participating resources
register the secrets they declare (a capability instance's config
secrets register at construct; a vm-template's Tailscale key registers
at its preflight), preflights *predict* resolvability without prompting,
and the operation runs ONE :meth:`resolve` pass at the preflight
boundary -- as soon as every participating resource's preflight passes,
covering the union of everything registered, one prompt session. Ops
then draw values from the cache via :meth:`get`.

This does not change the no-cross-invocation-cache stance (ADR 0016):
the cache lives and dies with the operation, exactly like the resolved
mapping the composition roots used to thread down as a dict; the
resolver just reifies "resolve once per command and pass the values
down" into an object a capability instance can hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


class Resolver:
    """Accumulate an operation's secret declarations; predict, resolve
    once, then serve cached values.

    The three verbs map onto the capability lifecycle:

    - :meth:`predict` (preflight): the name of the first active backend
      that would resolve the secret, or ``None`` when nothing would --
      never prompts (an interactive backend is reported without
      probing; probing would BE the prompt).
    - :meth:`resolve` (the preflight boundary): one batched pass over
      the active backends for every registered declaration -- one
      prompt session. Idempotent when nothing new was registered.
    - :meth:`get` (ops): a cached value. Raises a typed error if the
      boundary resolve has not run -- an op must never trigger
      resolution (a prompt mid-op is exactly what the boundary
      ordering exists to prevent).
    """

    def __init__(self, config: Config, registry: Registry) -> None:
        self._config = config
        self._registry = registry
        self._decls: dict[str, SecretDecl] = {}
        self._values: dict[str, str] | None = None

    # -- registration --------------------------------------------------

    def register(self, decls: Iterable[SecretDecl]) -> None:
        """Add declarations to the operation's resolve set (first
        registration of a name wins; re-registration is a no-op)."""
        for decl in decls:
            self._decls.setdefault(decl.name, decl)

    def register_name(self, name: str) -> SecretDecl:
        """Register a secret by name and return its declaration.

        Looks the name up against the registry's ``secret`` rows and
        falls back to synthesizing a bare declaration when absent: an
        operator who omits every ``[vm_templates.*]`` and ``[secrets.*]``
        section leaves the registry empty under the ``secret`` kind, and
        the backend chain must stay callable for the well-known names
        (the same fallback the pre-resolver ``_collect_secrets`` used).
        """
        existing = self._decls.get(name)
        if existing is not None:
            return existing
        decl = self._decl_for(name)
        self._decls[name] = decl
        return decl

    def _decl_for(self, name: str) -> SecretDecl:
        from agentworks.secrets.base import SecretDecl
        from agentworks.secrets.kinds import SECRET_KIND_NAME

        try:
            found: SecretDecl = self._registry.lookup(SECRET_KIND_NAME, name)
        except KeyError:
            return SecretDecl(name=name, description="")
        return found

    # -- the lifecycle verbs -------------------------------------------

    def predict(self, decl: SecretDecl) -> str | None:
        """Non-prompting resolvability prediction for preflights: the
        first active backend that would resolve ``decl``, or ``None``.
        A non-interactive backend must actually produce a value to be
        reported (an unset env var does not count as resolvable); the
        interactive prompt backend is reported without probing.
        """
        from agentworks.secrets.resolve import active_backends, preview_resolution

        return preview_resolution(
            decl, active_backends(self._config, self._registry)
        )

    def resolve(self) -> None:
        """THE operation's one resolve pass, run at the preflight
        boundary (after every participating resource's preflight
        passes, before any op). One prompt session for everything
        registered; values cached for :meth:`get`.

        Idempotent while the registered set is unchanged. Registering
        more declarations after the pass and resolving again is a
        contract violation (it would mean a second prompt session), so
        it raises instead of quietly re-prompting.
        """
        if self._values is not None:
            unresolved = [n for n in self._decls if n not in self._values]
            if unresolved:
                raise StateError(
                    "secret declarations were registered after the "
                    f"operation's resolve pass: {', '.join(sorted(unresolved))}. "
                    "Register every participating resource's secrets before "
                    "the preflight-boundary resolve."
                )
            return
        from agentworks.secrets.resolve import active_backends, resolve_secrets

        if not self._decls:
            self._values = {}
            return
        self._values = resolve_secrets(
            list(self._decls.values()),
            active_backends(self._config, self._registry),
        )

    def get(self, name: str) -> str:
        """A resolved value, from the boundary pass's cache."""
        if self._values is None:
            raise StateError(
                f"secret '{name}' requested before the operation's resolve "
                "pass ran. The composition root resolves once at the "
                "preflight boundary; reaching here means a caller skipped "
                "that step."
            )
        try:
            return self._values[name]
        except KeyError:
            raise StateError(
                f"secret '{name}' was not part of the operation's resolve "
                "pass. Register it (and re-run preflight) before the "
                "boundary resolve."
            ) from None

    @property
    def resolved(self) -> bool:
        """Whether the boundary pass has run."""
        return self._values is not None
