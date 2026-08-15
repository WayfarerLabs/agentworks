"""Typed, source-first secret resolution with operation-bounded clients."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Self

from agentworks import output
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientTimeout,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import ConfigError, StateError, UserAbort
from agentworks.schema import AgwModel, RefOwner
from agentworks.secrets.outcomes import (
    OUTCOME_RULES,
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    _safe_diagnostic_text,
    complete_resolution_error,
)
from agentworks.secrets.policy import InteractionPolicy, require_exact_interaction_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType

    from pydantic import BaseModel

    from agentworks.capabilities.secret_backend.base import SecretBackend
    from agentworks.config import Config
    from agentworks.resources.graph import Readiness
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import MappingValue, SecretDecl
    from agentworks.secrets.sources import SecretSourceDecl


@dataclass(frozen=True, slots=True)
class ActiveSource:
    """One configured source in the active precedence chain."""

    source: SecretSourceDecl
    backend_class: type[SecretBackend]
    config: AgwModel
    readiness: Readiness
    disabled_backend_plugin: str | None = None

    def __post_init__(self) -> None:
        if self.source.backend.name != self.backend_class.name:
            raise StateError(
                f"secret-source/{self.source.name} selects {self.source.backend.name!r}, "
                f"not {self.backend_class.name!r}"
            )
        if not isinstance(self.config, self.backend_class.config_model):
            raise StateError(f"secret-source/{self.source.name} carries the wrong backend config model")

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def interactive(self) -> bool:
        return self.backend_class.interactive

    def mapping_for(self, secret: SecretDecl) -> tuple[bool, MappingValue | None]:
        """Return mapping presence separately from its JSON-native value."""
        if self.name not in secret.backend_mappings:
            return False, None
        return True, secret.backend_mappings[self.name]

    def would_attempt(self, secret: SecretDecl) -> bool:
        mapping_present, mapping = self.mapping_for(secret)
        if mapping_present and mapping is False:
            return False
        return self.backend_class.would_attempt(secret.name, mapping_present=mapping_present)

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        request, identifier = _lookup_projection(secret, self)
        return None if request is None else identifier


class CompletionPolicy(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    """The two authorities one resolution pass runs under.

    Boundary: a caller-supplied argument crossing into the resolution loop.
    This constructor is how any caller reaches :func:`resolve_batch`, and
    ``resolve_batch`` branches on both fields by identity, so both are
    checked here and a policy that exists is a policy that was checked.
    ``interaction`` decides whether an interactive source may be attempted;
    ``completion`` decides whether a doomed batch fails before prompting.
    An equal-but-not-identical value (a plain ``"refuse"``, a plain
    ``"complete"``) takes the opposite branch silently in both cases.

    Checking on arrival at the service entry points buys position and reach
    on top of this; see the note above ``require_exact_interaction_policy``.
    """

    interaction: InteractionPolicy
    completion: CompletionPolicy

    def __post_init__(self) -> None:
        require_exact_interaction_policy(self.interaction)
        if type(self.completion) is not CompletionPolicy:
            raise StateError("completion must be an exact CompletionPolicy") from None


class ResolutionBatch:
    """A private value-bearing batch whose representation is always redacted.

    The two constructor checks are this object's own cross-field invariant,
    not input validation: a batch holds exactly one outcome per requested
    name, and holds a value for exactly the names whose outcome resolved.
    :meth:`complete_or_raise` hands out values on the strength of the
    outcomes, so a batch where the two disagree would hand out the wrong
    ones.
    """

    __slots__ = ("_outcomes", "_values")

    def __init__(
        self,
        outcomes: Sequence[ResolutionOutcome],
        values: Mapping[str, str],
    ) -> None:
        copied_outcomes = tuple(outcomes)
        names = tuple(outcome.name for outcome in copied_outcomes)
        if len(names) != len(set(names)):
            raise ValueError("resolution outcome names must be unique")
        copied_values = dict(values)
        resolved_names = {
            outcome.name for outcome in copied_outcomes if outcome.category is ResolutionCategory.RESOLVED
        }
        if set(copied_values) != resolved_names:
            raise ValueError("resolution values must match resolved outcomes exactly")
        self._outcomes = copied_outcomes
        self._values = copied_values

    @property
    def outcomes(self) -> tuple[ResolutionOutcome, ...]:
        return self._outcomes

    def complete_or_raise(self) -> dict[str, str]:
        if all(outcome.category is ResolutionCategory.RESOLVED for outcome in self._outcomes):
            return dict(self._values)
        raise complete_resolution_error(self._outcomes) from None

    def __repr__(self) -> str:
        outcomes = getattr(self, "_outcomes", ())
        values = getattr(self, "_values", {})
        return f"ResolutionBatch(outcomes={len(outcomes)}, resolved={len(values)}, values=<redacted>)"

    __str__ = __repr__


@dataclass(slots=True)
class _Evidence:
    refused: list[tuple[str, str | None]]
    soft_missed: list[tuple[str, str | None]]
    not_ready: list[tuple[str, str | None, str | None]]

    @classmethod
    def empty(cls) -> _Evidence:
        return cls(refused=[], soft_missed=[], not_ready=[])


class _MonotonicBudget:
    __slots__ = ("_clock", "_deadline")

    def __init__(self, deadline: float | None, clock: Callable[[], float]) -> None:
        self._deadline = deadline
        self._clock = clock

    @classmethod
    def start(cls, timeout: float | None, *, clock: Callable[[], float] = time.monotonic) -> Self:
        now = clock()
        return cls(None if timeout is None else now + timeout, clock)

    def remaining(self) -> float | None:
        return None if self._deadline is None else max(0.0, self._deadline - self._clock())


_CLEANUP_WARNING = "secret source {source_name!r}: cleanup failed; primary result unchanged"


def _warn_cleanup_failure(source_name: str) -> None:
    with suppress(BaseException):
        output.warn(_CLEANUP_WARNING.format(source_name=source_name))


class _SourceContextDriver(AbstractContextManager[SecretSourceClient]):
    def __init__(
        self,
        inner: AbstractContextManager[SecretSourceClient],
        *,
        source_name: str,
        remaining_time: RemainingTime,
    ) -> None:
        self._inner = inner
        self._source_name = source_name
        self._remaining_time = remaining_time

    def __enter__(self) -> SecretSourceClient:
        return self._inner.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        started = self._remaining_time()
        failed = False
        try:
            suppressed = self._inner.__exit__(exc_type, exc, traceback)
            failed = bool(suppressed)
        except BaseException:
            failed = True
        else:
            finished = self._remaining_time()
            if started is not None and started > 0.0 and finished == 0.0:
                failed = True
        if failed:
            _warn_cleanup_failure(self._source_name)
        return False


class OutputInteractionBroker:
    """CLI-owned prompt broker exposing only named secret requests."""

    __slots__ = ("_prompts",)

    def __init__(self, secrets: Sequence[SecretDecl]) -> None:
        self._prompts = {secret.name: (secret.description, secret.hint) for secret in secrets}

    def request_secret(self, name: str, /) -> str:
        prompt = self._prompts.get(name)
        if prompt is None:
            raise StateError(f"no prompt metadata is registered for secret {name!r}")
        description, hint = prompt
        return output.prompt_secret(f"Secret '{name}': {description}", hint=hint)

    def __repr__(self) -> str:
        return f"OutputInteractionBroker(prompts={len(self._prompts)})"


def active_sources(config: Config, registry: Registry) -> tuple[ActiveSource, ...]:
    """Build the configured source chain from finalized source rows."""
    from agentworks.capabilities.config import validate_capability_config
    from agentworks.config.references import SettingReference
    from agentworks.resources.graph import Enablement
    from agentworks.secrets.sources import (
        SecretSourceDecl,
        direct_backend_source_error,
        registry_source_backend_lookup,
        source_backend_class,
    )

    lookup = registry_source_backend_lookup(registry)
    active: list[ActiveSource] = []
    for name in config.secret_config_data.sources:
        selected = source_backend_class(lookup, name)
        if selected is None:
            error = direct_backend_source_error(
                name=name,
                registry=registry,
                referrer=SettingReference(
                    setting="[secret_config].sources",
                    kind="secret-source",
                    name=name,
                ),
            )
            if error is not None:
                raise error
            registered = sorted(row.name for row in registry.iter_kind("secret-source"))
            raise ConfigError(
                f"[secret_config].sources names unknown secret-source {name!r}",
                hint=f"declared secret sources: {registered}",
            ) from None
        source, backend_class = selected
        if not isinstance(source, SecretSourceDecl):
            raise StateError(f"secret-source/{name} is not a SecretSourceDecl")
        if registry.graph.enablement_of("secret-source", name) is Enablement.disabled:
            continue
        disabled_backend_plugin: str | None = None
        if registry.graph.enablement_of("secret-backend", source.backend.name) is Enablement.disabled:
            # ``origin.plugin`` is set only by the ``system-plugin`` variant, so it
            # doubles as the variant test, exactly as the other disabled-row tails
            # read it (``resources.access.ensure_reference_enabled``).
            origin = getattr(registry.lookup("secret-backend", source.backend.name), "origin", None)
            disabled_backend_plugin = getattr(origin, "plugin", None)
        validated = validate_capability_config(
            kind="secret-backend",
            config=source.backend.tagged,
            owner=RefOwner(kind="secret-source", name=name),
            location=source.error_location,
        )
        if validated is None or not isinstance(validated, AgwModel):
            raise StateError(f"secret-source/{name} has no validated backend config")
        active.append(
            ActiveSource(
                source=source,
                backend_class=backend_class,
                config=validated,
                readiness=registry.graph.readiness_of("secret-source", name),
                disabled_backend_plugin=disabled_backend_plugin,
            )
        )
    return tuple(active)


def _lookup_projection(
    secret: SecretDecl,
    source: ActiveSource,
) -> tuple[SecretLookupRequest | None, str | None]:
    """Project one secret onto one source as a request and a safe identifier.

    Boundary: operator-authored manifest input rendered as text. The
    identifier a backend derives is the operator's own mapping value (the
    ``env-var`` backend returns the variable name verbatim, and ``op://``
    references likewise), and it lands in a rendered diagnostic line, so it
    is screened once here for characters that could forge or alter that
    line. Nothing else the backend returns is re-checked.
    """
    from agentworks.capabilities.config import validate_capability_mapping

    mapping_present, mapping = source.mapping_for(secret)
    if mapping_present and mapping is False:
        return None, None
    if not source.would_attempt(secret):
        return None, None
    validated: BaseModel | None = None
    if mapping_present:
        try:
            validated = validate_capability_mapping(
                kind="secret-backend",
                name=source.backend_class.name,
                mapping=mapping,
                owner=secret.mapping_owner(source.name),
                location=secret.error_location,
            )
        except ConfigError:
            raise StateError(
                f"secret/{secret.name} mapping for source {source.name!r} disagrees with finalized validation"
            ) from None
        if validated is None:
            raise StateError(f"secret-source/{source.name} mapping validation selected no backend model")
    identifier = source.backend_class.describe_lookup(secret.name, validated)
    if identifier is not None and not _safe_diagnostic_text(identifier):
        raise _BackendProtocolError
    return SecretLookupRequest(name=secret.name, mapping=validated), identifier


class _BackendProtocolError(Exception):
    """A lookup identifier is unsafe to render, so this secret gets no row here.

    Raised only by :func:`_lookup_projection`, and caught per secret so one
    unrenderable identifier costs that secret its attempt at this source
    rather than failing the whole batch.
    """

    __slots__ = ()


def _outcome(
    name: str,
    detail: ResolutionDetail,
    *,
    source: str | None = None,
    identifier: str | None = None,
    remediation_target: str | None = None,
) -> ResolutionOutcome:
    rule = OUTCOME_RULES[detail]
    return ResolutionOutcome(
        name=name,
        category=rule.category,
        detail=detail,
        remediation=rule.remediation,
        source=source,
        identifier=identifier,
        remediation_target=remediation_target,
    )


def _collapse(name: str, evidence: _Evidence, *, sources_empty: bool) -> ResolutionOutcome:
    if evidence.refused:
        source, identifier = evidence.refused[0]
        return _outcome(name, ResolutionDetail.INTERACTION_REFUSED, source=source, identifier=identifier)
    if evidence.soft_missed:
        source, identifier = evidence.soft_missed[0]
        return _outcome(name, ResolutionDetail.SOFT_MISS, source=source, identifier=identifier)
    if evidence.not_ready:
        source, identifier, disabled_backend_plugin = evidence.not_ready[0]
        detail = (
            ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED
            if disabled_backend_plugin is not None
            else ResolutionDetail.SOURCE_NOT_READY
        )
        return _outcome(
            name,
            detail,
            source=source,
            identifier=identifier,
            remediation_target=disabled_backend_plugin,
        )
    return _outcome(
        name,
        ResolutionDetail.NO_ACTIVE_SOURCE if sources_empty else ResolutionDetail.NO_ATTEMPTABLE_SOURCE,
    )


def _remaining_attemptable(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
    *,
    policy: ResolutionPolicy,
) -> bool:
    for source in sources:
        if not source.readiness.is_ready:
            continue
        if source.interactive and policy.interaction is InteractionPolicy.REFUSE:
            continue
        if source.would_attempt(secret):
            return True
    return False


def _check_boundary(remaining_time: RemainingTime) -> None:
    remaining = remaining_time()
    if remaining is not None and remaining == 0.0:
        raise SecretClientTimeout from None


def _drive_source(
    source: ActiveSource,
    requests: tuple[SecretLookupRequest, ...],
    *,
    broker: InteractionBroker | None,
    timeout: float | None,
) -> Mapping[str, str]:
    budget = _MonotonicBudget.start(
        timeout,
        clock=time.monotonic,
    )
    remaining = budget.remaining
    _check_boundary(remaining)
    context = source.backend_class.create_client(
        source_name=source.name,
        config=source.config,
        interaction_broker=broker,
        remaining_time=remaining,
    )
    _check_boundary(remaining)
    driver = _SourceContextDriver(context, source_name=source.name, remaining_time=remaining)
    with driver as client:
        _check_boundary(remaining)
        client.prepare(requests, remaining_time=remaining)
        _check_boundary(remaining)
        resolved = client.resolve(requests, remaining_time=remaining)
        _check_boundary(remaining)
    return resolved


def resolve_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    policy: ResolutionPolicy,
    interaction_broker: InteractionBroker | None,
) -> ResolutionBatch:
    """Resolve one deduplicated batch through lazy, bounded source turns."""
    deduped: list[SecretDecl] = []
    seen: set[str] = set()
    for secret in secrets:
        if secret.name not in seen:
            seen.add(secret.name)
            deduped.append(secret)

    outcomes: dict[str, ResolutionOutcome] = {}
    values: dict[str, str] = {}
    evidence = {secret.name: _Evidence.empty() for secret in deduped}

    for index, source in enumerate(sources):
        missing = [secret for secret in deduped if secret.name not in outcomes]
        if not missing:
            break
        projected: list[tuple[SecretLookupRequest, str | None]] = []
        for secret in missing:
            try:
                request, identifier = _lookup_projection(secret, source)
            except _BackendProtocolError:
                outcomes[secret.name] = _outcome(
                    secret.name,
                    ResolutionDetail.BACKEND_PROTOCOL,
                    source=source.name,
                )
                continue
            if request is not None:
                projected.append((request, identifier))
        if not projected:
            continue
        if not source.readiness.is_ready:
            if not source.readiness.reason:
                raise StateError(f"secret-source/{source.name} has invalid readiness") from None
            for request, identifier in projected:
                evidence[request.name].not_ready.append((source.name, identifier, source.disabled_backend_plugin))
            continue
        if source.interactive and policy.interaction is InteractionPolicy.REFUSE:
            for request, identifier in projected:
                evidence[request.name].refused.append((source.name, identifier))
            continue
        if source.interactive and policy.completion is CompletionPolicy.COMPLETE:
            still_missing = [secret for secret in deduped if secret.name not in outcomes]
            terminal_exists = any(outcome.category is not ResolutionCategory.RESOLVED for outcome in outcomes.values())
            if not terminal_exists:
                for secret in still_missing:
                    if not _remaining_attemptable(secret, sources[index:], policy=policy):
                        outcomes[secret.name] = _collapse(
                            secret.name,
                            evidence[secret.name],
                            sources_empty=not sources,
                        )
                terminal_exists = any(secret.name in outcomes for secret in still_missing)
            if terminal_exists:
                for secret in still_missing:
                    if secret.name not in outcomes:
                        outcomes[secret.name] = _outcome(secret.name, ResolutionDetail.BATCH_DOOMED)
                break
        if source.backend_class.name == "prompt" and interaction_broker is None:
            raise StateError("the prompt source requires an interaction broker")

        requests = tuple(request for request, _identifier in projected)
        identifiers = {request.name: identifier for request, identifier in projected}
        broker = interaction_broker if source.backend_class.name == "prompt" else None
        failure_kind: SecretClientFailureKind | None = None
        timed_out = False
        unexpected = False
        timeout = source.backend_class.external_operation_timeout(source.config)
        # Empty rather than unbound, so a failure branch that ever stopped
        # short of its ``continue`` would soft-miss this source instead of
        # attributing the previous source's values to it.
        returned: Mapping[str, str] = {}
        try:
            returned = _drive_source(source, requests, broker=broker, timeout=timeout)
        except UserAbort:
            raise
        except SecretClientTimeout:
            timed_out = True
        except SecretClientFailure as failure:
            failure_kind = failure.kind
        except Exception:
            # Boundary: an external process or service. The source turn shells
            # out to a provider CLI or SDK, so anything it raises beyond the
            # declared failure vocabulary becomes this source's per-secret
            # ``unexpected`` outcome rather than ending the command.
            unexpected = True
        if timed_out or failure_kind is not None or unexpected:
            if timed_out:
                detail = ResolutionDetail.DEADLINE_EXCEEDED
            elif failure_kind is not None:
                detail = {
                    SecretClientFailureKind.HARD_MAPPING: ResolutionDetail.HARD_MAPPING,
                    SecretClientFailureKind.AUTHENTICATION: ResolutionDetail.AUTHENTICATION,
                    SecretClientFailureKind.CONNECTIVITY: ResolutionDetail.CONNECTIVITY,
                    SecretClientFailureKind.EXTERNAL: ResolutionDetail.EXTERNAL,
                }[failure_kind]
            else:
                detail = ResolutionDetail.UNEXPECTED
            for request in requests:
                outcomes[request.name] = _outcome(
                    request.name,
                    detail,
                    source=source.name,
                    identifier=identifiers[request.name],
                )
            continue

        for request in requests:
            if request.name not in returned:
                evidence[request.name].soft_missed.append((source.name, identifiers[request.name]))
                continue
            value = returned[request.name]
            if "\0" in value:
                outcomes[request.name] = _outcome(
                    request.name,
                    ResolutionDetail.MALFORMED_VALUE,
                    source=source.name,
                    identifier=identifiers[request.name],
                )
                continue
            values[request.name] = value
            outcomes[request.name] = _outcome(
                request.name,
                ResolutionDetail.RESOLVED,
                source=source.name,
                identifier=identifiers[request.name],
            )

    for secret in deduped:
        if secret.name not in outcomes:
            outcomes[secret.name] = _collapse(
                secret.name,
                evidence[secret.name],
                sources_empty=not sources,
            )
    ordered = tuple(outcomes[secret.name] for secret in deduped)
    return ResolutionBatch(ordered, values)


@dataclass(slots=True)
class PartialResolution:
    """Explicit partial reveal result with values separate from diagnostics."""

    values: dict[str, str]
    outcomes: tuple[ResolutionOutcome, ...]

    def __repr__(self) -> str:
        return f"PartialResolution(outcomes={len(self.outcomes)}, values=<redacted>)"

    __str__ = __repr__


def _copy_partial_values(batch: ResolutionBatch) -> dict[str, str]:
    return dict(batch._values)


def resolve_partial_for_reveal(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy,
) -> PartialResolution:
    """Resolve independent values for the explicit env reveal surface.

    Checks its own ``interaction`` because the coverage rule beside
    ``require_exact_interaction_policy`` says every constructing function does. The
    constructor would catch a bad value one line later here, which is exactly why the
    rule is applied rather than judged.
    """
    require_exact_interaction_policy(interaction)
    broker = OutputInteractionBroker(secrets) if interaction is InteractionPolicy.ALLOW else None
    batch = resolve_batch(
        secrets,
        sources,
        policy=ResolutionPolicy(interaction=interaction, completion=CompletionPolicy.PARTIAL),
        interaction_broker=broker,
    )
    return PartialResolution(
        values=_copy_partial_values(batch),
        outcomes=batch.outcomes,
    )


def validate_chain(config: Config, registry: Registry) -> None:
    """Validate source-chain reachability for operator-declared secrets."""
    from agentworks.resources.access import secret_decls

    active_source_names = set(config.secret_config_data.sources)
    operator_decls = [
        decl
        for decl in secret_decls(registry).values()
        if getattr(getattr(decl, "origin", None), "variant", None) == "operator-declared"
    ]
    unreachable = [
        decl
        for decl in operator_decls
        if not ({ref.name for ref in registry.graph.edges_of("secret", decl.name)} & active_source_names)
    ]
    if unreachable:
        names = ", ".join(sorted(decl.name for decl in unreachable))
        source_chain = ", ".join(config.secret_config_data.sources) or "(empty)"
        raise ConfigError(
            f"unreachable secret(s): {names}",
            hint=(
                f"active source chain: [{source_chain}]. Add an attemptable secret-source, "
                "remove a false opt-out, or remove the unused declaration."
            ),
        )
