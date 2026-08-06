"""Core types for the agentworks secret system.

Secrets are declarations (``SecretDecl``); values come from the
registered backend capabilities (``agentworks.secrets.backends``)
through the resolution loop (ADR 0016, YAML resource manifests and the
config/resource/capability split). See
``docs/adrs/0013-cli-side-secret-injection.md`` for why values never
persist on the VM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal

from pydantic import BeforeValidator, Field
from pydantic.json_schema import SkipJsonSchema

from agentworks.declared_resource import DeclaredResource
from agentworks.naming import MAX_SECRET_NAME_LENGTH
from agentworks.schema import RefOwner
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


def _refuse_true(value: Any) -> Any:
    """``true`` keeps its own message: the alternatives list the union
    would otherwise render says what IS accepted without teaching that
    ``false`` is the opt-out an operator was reaching for."""
    if value is True:
        raise ValueError("boolean must be `false` (opt-out); `true` is not a valid value")
    return value


MappingValue = Annotated[str | dict[str, object] | Literal[False], BeforeValidator(_refuse_true)]
"""One entry in ``SecretDecl.backend_mappings``: an identifier override
(string or structured), or ``False`` for an explicit opt-out."""


class SecretDecl(DeclaredResource):
    """A declared secret. Values are never stored here; only the existence,
    description, and per-backend identifier overrides.

    ``backend_mappings`` is keyed by backend (capability) name
    (``"env-var"``, ``"prompt"``; later ``"onepassword"``, ...). Value
    forms per the env-and-secrets SDD:

    - ``str``: backend's identifier for this secret (env var name, op:// URI, etc.).
    - ``dict[str, object]``: structured identifier (for backends whose ID
      carries more than the bare reference, e.g. 1Password's
      ``{account, reference}`` for pinning a specific account).
    - ``False``: opt out; skip this backend for this secret regardless of any
      default convention the backend would otherwise apply.
    - key absent: use the backend's default convention if it has one, else
      soft-skip (backend reports as "no mapping" via ``would_attempt``).
    """

    # Secrets are never derived into Linux usernames, so they take the
    # larger cap rather than the freeform one.
    NAME_MAX_LENGTH: ClassVar[int | None] = MAX_SECRET_NAME_LENGTH

    # Override the base's optional ``description``: a secret must carry one
    # (it is the operator-facing prompt text), so it is required here.
    #
    # Required, but deliberately NOT ``NonEmptyStr``, and the distinction
    # is the one ``NAME_MAX_LENGTH`` above draws: the framework CONSTRUCTS
    # secret rows with an empty description on purpose (``synthesize`` for
    # every auto-declared secret, plus four placeholder sites for a secret
    # nothing declared), and the registry's polish pass fills the
    # auto-declared ones in afterwards. What has to be non-empty is what an
    # OPERATOR wrote, which decode checks against this field's
    # requiredness (``_check_declared_description``).
    description: SkipJsonSchema[str]

    hint: str | None = None
    """Operator-facing text shown when the secret has to be entered by
    hand: where to generate it, which account it belongs to."""

    backend_mappings: dict[str, MappingValue] = Field(default_factory=dict)
    """Per-backend identifier overrides, keyed by backend name."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The secret's ``secret -> secret-backend`` edges: the candidate
        backends that could resolve it, frozen into the graph at finalize.

        The edge set is the union of:

        - (a) every PRESENT backend that would attempt this secret
          (``would_attempt(secret, mapping)`` true: it has a mapping or is
          mapping-optional), read off the build ``context``'s available-backend
          list, MINUS an explicit ``False`` opt-out; and
        - (b) every explicit non-``False`` ``backend_mappings`` key, even one
          naming no present backend (the DANGLING edge that turns a typo'd key
          into a hard finalize miss under the ``secret-backend`` kind's
          ``"error"`` policy, R9.11).

        ``would_attempt`` is a pure function of ``(secret, mapping)`` (the
        ``SecretBackend`` contract), so freezing candidates into edges at
        finalize is safe: ``edges_of(secret)`` is the full candidate set that
        resolution (LLD d) walks. Total and non-throwing. Deduped by target
        backend name in first-encountered order (present backends in registry
        order, then any extra explicit keys).
        """
        from agentworks.resources.reference import ResourceReference, sourced_references

        source = ("secret", self.name)
        seen: set[str] = set()
        refs: list[ResourceReference] = []

        def emit(backend_name: str) -> None:
            if backend_name in seen:
                return
            seen.add(backend_name)
            refs.append(
                ResourceReference(
                    name=backend_name,
                    kind="secret-backend",
                    usage=f"a resolution backend for secret {self.name!r}",
                    source=source,
                )
            )

        # (a) present backends that would attempt this secret (minus a false
        # opt-out). ``would_attempt`` is pure over (secret, mapping).
        for backend_name, backend in context.available_backends:
            mapping = self.backend_mappings.get(backend_name)
            if mapping is False:
                continue
            if backend.would_attempt(self, mapping):
                emit(backend_name)
        # (a2) whatever each declared mapping itself NAMES. Every shipped
        # backend's mapping is an external identifier (an env var name, an
        # ``op://`` reference) that implies no agentworks resource, so this
        # contributes nothing today. It is wired anyway so secret-backend is
        # not the one kind whose config references are structurally
        # underivable: the core reads them off the backend's declared model,
        # exactly as it does for the other three kinds.
        from agentworks.capabilities.config import capability_config_references

        for backend_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            refs.extend(
                sourced_references(
                    capability_config_references(
                        kind="secret-backend",
                        name=backend_name,
                        config=mapping,
                        owner=self._mapping_owner(backend_name),
                    ),
                    source,
                )
            )
        # (b) explicit non-false mapping keys, including any naming no present
        # backend (a dangling edge that the "error" miss policy hard-errors).
        for backend_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            emit(backend_name)
        return refs

    def validate_config(self, enabled_backends: frozenset[str], context: FinalizeContext) -> None:
        """Throwing per-mapping spec check, run by the finalize ``validate``
        pass: every declared ``backend_mappings`` entry addressed to a PRESENT
        AND ENABLED backend is validated by the CORE against that backend's
        declared model (R9.9: every declared mapping, not just the opted-in
        ones, so a stale mapping for a configured-but-not-opted-in backend
        fails at build). No backend code runs.

        ``enabled_backends`` is the set of enabled ``secret-backend`` names the
        finalize pass threads from the graph's enablement axis. A mapping to a
        present-but-DISABLED backend is INERT (not validated until enabled),
        the same enablement seam materialization-gating and resolution already
        consult; inert today (no disabled producer ships, R7). The generic
        ``False`` opt-out is loop-owned and never validated; a mapping to an
        ABSENT backend is the dangling edge the resolve pass already
        hard-errored (R9.11), so it never reaches here.
        """
        from agentworks.capabilities.config import validate_capability_config

        for backend_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            if backend_name not in enabled_backends:
                # Absent (dangling, already hard-errored) or present-but-disabled
                # (inert until enabled): neither is validated here.
                continue
            validate_capability_config(
                kind="secret-backend",
                name=backend_name,
                config=mapping,
                owner=self._mapping_owner(backend_name),
                location=self.error_location,
            )

    def _mapping_owner(self, backend_name: str) -> RefOwner:
        """Who owns one ``backend_mappings`` value, for error framing.

        The secret alone would be ambiguous: a secret may map several
        backends, and a root model's errors carry no field path of their
        own, so without the key an operator reading "must not be empty"
        would not know WHICH mapping to fix.
        """
        return RefOwner(
            kind="secret",
            name=self.name,
            label=f"secret/{self.name}.backend_mappings.{backend_name}",
        )


DEFAULT_BACKEND_CHAIN: tuple[str, ...] = ("env-var", "prompt")
"""Default backend chain when ``[secret_config].backends`` is absent.

Resolves declared secrets from operator-side env (``AW_SECRET_<NAME>``) first,
then prompts interactively. The chain is operator-overridable via an explicit
``[secret_config]`` block; an explicit empty list ``backends = []`` disables
resolution entirely (operators who don't use secrets pay nothing either way).
"""


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table. Pure config, never published to
    the resource Registry: the chain is a SETTING that names resources
    (like a future active-plugins list would), consumed by the secrets
    subsystem when it validates (``validate_chain``, at
    ``build_registry``) and when it resolves (``resolve_secrets``).

    ``backends`` is dual-role: presence activates the backend, list
    order is the resolution precedence. A declared backend absent from
    this list is dormant (never consulted).

    Default value is ``DEFAULT_BACKEND_CHAIN`` (``env-var``, then ``prompt``).
    The default applies when the operator's TOML has no ``[secret_config]``
    table OR has the table without a ``backends`` key. An explicit
    ``backends = []`` disables resolution entirely.
    """

    backends: tuple[str, ...] = DEFAULT_BACKEND_CHAIN
    declared_at: SourceLocation = field(default_factory=synthesized)
