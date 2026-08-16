"""Core types for the agentworks secret system.

Secrets are declarations (``SecretDecl``); values come from the
registered backend capabilities (``agentworks.capabilities.secret_backend``)
through the resolution loop (ADR 0016, YAML resource manifests and the
config/resource/capability split). See
``docs/adrs/0013-cli-side-secret-injection.md`` for why values never
persist on the VM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, Annotated, ClassVar, cast

from pydantic import BeforeValidator, Field
from pydantic.json_schema import SkipJsonSchema

from agentworks.declared_resource import DeclaredResource
from agentworks.naming import MAX_SECRET_NAME_LENGTH
from agentworks.schema import RefOwner
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


def require_exact_json_value(value: object) -> object:
    """Reject non-JSON-native values before pydantic can normalize them.

    Boundary: operator-authored manifest input. ``backend_mappings`` is
    carried losslessly to whichever backend model the selected source
    declares, so what YAML produced has to be what the backend sees. The
    manifest loader is pyyaml's ``SafeLoader``, whose tag set is wider than
    JSON: ``!!timestamp`` builds a ``datetime.date``, ``!!binary`` builds
    ``bytes``, ``!!set`` builds a ``set``, and ``!!pairs`` builds a list of
    tuples. Any of those would reach a backend that was promised JSON.

    The validator is attached recursively, so a failure keeps the list index
    or mapping key that led to it. Checking ``type`` rather than
    ``isinstance`` is intentional: enum members and custom primitive
    subclasses are not JSON-native runtime values even when Python makes
    them behave like one.
    """
    value_type = type(value)
    if value_type not in (type(None), bool, int, float, str, list, dict):
        raise ValueError(f"must use exact JSON-native runtime types (got {value_type.__name__})")
    if value_type is float and not isfinite(cast("float", value)):
        raise ValueError("JSON numbers must be finite")
    return value


def require_exact_json_string(value: object) -> object:
    """Reject non-string mapping keys before pydantic string normalization.

    Boundary: the same manifest input :func:`require_exact_json_value`
    names. YAML mapping keys are not string-only, so ``{1: "x"}`` and
    ``{2020-01-02: "x"}`` are both loadable documents.
    """
    if type(value) is not str:
        raise ValueError(f"must use exact JSON string keys (got {type(value).__name__})")
    return value


type _JsonString = Annotated[str, BeforeValidator(require_exact_json_string)]
type MappingValue = Annotated[
    None | bool | int | float | str | list[MappingValue] | dict[_JsonString, MappingValue],
    BeforeValidator(require_exact_json_value),
]
"""One lossless JSON-native mapping value. Exact ``False`` remains the
framework opt-out; every other value belongs to the selected model."""


class SecretDecl(DeclaredResource):
    """A declared secret. Values are never stored here; only the existence,
    description, and per-source identifier overrides.

    ``backend_mappings`` is a source-name-to-lookup-address map. Its raw carrier
    preserves every JSON-native value for the selected backend's validation:

    - strings, numbers, ``True``, null, arrays, and string-keyed objects are
      delivered to the selected model without framework coercion;
    - ``False``: opt out; skip this source for this secret regardless of any
      default convention the selected backend would otherwise apply.
    - key absent: use the selected backend's default convention if it has one, else
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
    description: SkipJsonSchema[str] = Field(examples=["npm registry token"])
    """What this secret is, in one line. Required on a secret where it is
    optional on every other kind, because this is the text an operator
    reads when they are being asked to type the value in."""

    hint: str | None = Field(default=None, examples=["Generate at https://www.npmjs.com/settings/<user>/tokens"])
    """Operator-facing text shown when the secret has to be entered by
    hand: where to generate it, which account it belongs to."""

    backend_mappings: dict[_JsonString, MappingValue] = Field(
        default_factory=dict,
        examples=[{"env-var": "NPM_TOKEN"}],
    )
    """Lookup-address overrides. The editor schema offers every registered
    backend mapping shape, marks each key as a secret-source reference, and
    includes exact ``false`` as the framework opt-out."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """Emit candidate ``secret -> secret-source`` edges source-first.

        The edge set is the union of:

        - (a) every PRESENT source whose selected backend would attempt this secret
          (``would_attempt(secret, mapping)`` true: it has a mapping or is
          mapping-optional), read from the build context's source rows, MINUS
          an explicit ``False`` opt-out; and
        - (b) every explicit non-``False`` ``backend_mappings`` key, even one
          naming no present source (the DANGLING validation edge that turns a
          typo'd key into a hard finalize miss under `secret-source`).

        ``would_attempt`` is a pure function of ``(secret, mapping)`` (the
        ``SecretBackend`` contract), so freezing candidates into edges at
        finalize is safe: ``edges_of(secret)`` is the full candidate set that
        resolution (LLD d) walks. Total and non-throwing. Deduped by target
        source name in first-encountered order (present sources in registry
        order, then any extra explicit keys).
        """
        from agentworks.resources.reference import ResourceReference, sourced_references
        from agentworks.secrets.sources import (
            finalize_source_backend_lookup,
            source_backend_class,
            source_mapping_references,
        )

        source = ("secret", self.name)
        seen: set[str] = set()
        refs: list[ResourceReference] = []

        def emit(source_name: str) -> None:
            if source_name in seen:
                return
            seen.add(source_name)
            refs.append(
                ResourceReference(
                    name=source_name,
                    kind="secret-source",
                    usage=f"a resolution source for secret {self.name!r}",
                    source=source,
                )
            )

        lookup = finalize_source_backend_lookup(context)
        for source_name in context.rows_of("secret-source"):
            selected = source_backend_class(lookup, source_name)
            if selected is None:
                continue
            _source_decl, backend = selected
            mapping_present = source_name in self.backend_mappings
            mapping = self.backend_mappings.get(source_name)
            if mapping_present and mapping is False:
                continue
            if backend.would_attempt(self.name, mapping_present=mapping_present):
                emit(source_name)

        for source_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            refs.extend(
                sourced_references(
                    source_mapping_references(
                        lookup=lookup,
                        source_name=source_name,
                        mapping=mapping,
                        owner=self.mapping_owner(source_name),
                    ),
                    source,
                )
            )
        for source_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            emit(source_name)
        return refs

    def validate_config(self, context: FinalizeContext) -> None:
        """Throwing per-mapping spec check, run by the finalize ``validate``
        pass: EVERY declared ``backend_mappings`` entry is validated by the
        CORE against its selected backend's declared model (R9.9: every declared
        mapping, not just the opted-in ones, so a stale mapping for a
        configured-but-not-opted-in source fails at build). No backend code
        runs.

        UNCONDITIONAL over enablement, like the pass that calls it
        (:meth:`~agentworks.resources.registry.Registry._validate_resources`).
        A mapping addressed to a source selecting a DISABLED backend is
        validated exactly like one selecting an enabled backend: whether a
        key is accepted is the backend model's answer, and an operator must not be
        able to bank invalid config that detonates at the moment they enable
        the backend. That is the worst possible moment to learn the mapping
        was never well-formed.

        Being validated is separate from being live. A source selecting a
        disabled backend folds not-ready and is skipped without constructing a
        client; this method decides only when the shape is checked.

        Two entries are still not validated against a backend model, neither
        of them for an environmental reason:

        - the ``False`` opt-out, which is LOOP-owned vocabulary rather than
          backend config. It names no model to check it against, and it says
          the same thing on every host, so skipping it is a fact about the
          document, not about the environment.
        - a mapping naming an absent source, which selects no model and
          validates vacuously here. Its separately collected source-key
          reference reports the dangling source exactly once.
        """
        from agentworks.secrets.sources import finalize_source_backend_lookup, validate_source_mapping

        lookup = finalize_source_backend_lookup(context)
        for source_name, mapping in self.backend_mappings.items():
            if mapping is False:
                continue
            validate_source_mapping(
                lookup=lookup,
                source_name=source_name,
                mapping=mapping,
                owner=self.mapping_owner(source_name),
                location=self.error_location,
            )

    def mapping_owner(self, source_name: str) -> RefOwner:
        """Who owns one ``backend_mappings`` value, for error framing.

        The secret alone would be ambiguous: a secret may map several
        sources, and a root model's errors carry no field path of their
        own, so without the key an operator reading "must not be empty"
        would not know WHICH mapping to fix.

        Public, because the finalize pass above is not the only validator
        of a mapping: the runtime projection that re-validates a source entry defensively
        (``onepassword``) has to frame the result identically, and a second
        spelling of this label would be a second thing to keep in sync.
        """
        return RefOwner(
            kind="secret",
            name=self.name,
            label=f"secret/{self.name}.backend_mappings.{source_name}",
        )


DEFAULT_SOURCE_CHAIN: tuple[str, ...] = ("env-var", "prompt")
"""Default source chain when ``[secret_config].sources`` is absent.

Resolves declared secrets from operator-side env (``AW_SECRET_<NAME>``) first,
then prompts interactively. The chain is operator-overridable via an explicit
``[secret_config]`` block; an explicit empty list ``sources = []`` disables
resolution entirely (operators who don't use secrets pay nothing either way).
"""


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table. Pure config, never published to
    the resource Registry: the chain is a SETTING that names resources
    (like a future active-plugins list would), consumed by the secrets
    subsystem when it validates (``validate_chain``, at
    ``build_registry``) and when it resolves (the operation's typed resolution batch).

    ``sources`` is the settings spelling and contains source names:
    presence activates the source and list order is the resolution precedence. A declared source absent from
    this list is dormant (never consulted).

    Default value is ``DEFAULT_SOURCE_CHAIN`` (``env-var``, then ``prompt``).
    The default applies when the operator's TOML has no ``[secret_config]``
    table OR has the table without a ``sources`` key. An explicit
    ``sources = []`` disables resolution entirely.
    """

    sources: tuple[str, ...] = DEFAULT_SOURCE_CHAIN
    declared_at: SourceLocation = field(default_factory=synthesized)
