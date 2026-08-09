"""Typed, source-first secret resolution with operation-bounded clients."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
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
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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
        failed = False
        try:
            result = self.backend_class.would_attempt(secret.name, mapping_present=mapping_present)
        except Exception:
            failed = True
            result = False
        if failed or type(result) is not bool:
            raise _BackendProtocolError from None
        return result

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        request, identifier = _lookup_projection(secret, self)
        return None if request is None else identifier


class CompletionPolicy(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    interaction: InteractionPolicy
    completion: CompletionPolicy

    def __post_init__(self) -> None:
        validate_interaction_policy(self.interaction)
        if type(self.completion) is not CompletionPolicy:
            raise StateError("completion must be an exact CompletionPolicy") from None


class ResolutionBatch:
    """A private value-bearing batch whose representation is always redacted."""

    __slots__ = ("_outcomes", "_values")

    def __init__(
        self,
        outcomes: Sequence[ResolutionOutcome],
        values: Mapping[str, str],
        *,
        _token: object,
    ) -> None:
        try:
            # Initialize both slots before doing any fallible copying so even
            # an asynchronously interrupted instance remains safely redacted.
            self._outcomes: tuple[ResolutionOutcome, ...] = ()
            self._values: dict[str, str] = {}
            copied_values: dict[str, str] = {}
            name: object | None = None
            value: object | None = None
            if _token is not _BATCH_TOKEN:
                raise TypeError("ResolutionBatch construction is internal")
            copied_outcomes = tuple(outcomes)
            names = tuple(outcome.name for outcome in copied_outcomes)
            if len(names) != len(set(names)):
                raise ValueError("resolution outcome names must be unique")
            copied_values = dict(values)
            for name, value in copied_values.items():
                if type(name) is not str or type(value) is not str:
                    raise TypeError("resolution values must be strings")
                name = None
                value = None
            resolved_names = {o.name for o in copied_outcomes if o.category is ResolutionCategory.RESOLVED}
            if set(copied_values) != resolved_names:
                raise ValueError("resolution values must match resolved outcomes exactly")
            self._outcomes = copied_outcomes
            self._values = copied_values
            copied_values = {}
            values = {}
            name = None
            value = None
            return None
        except BaseException:
            name = None
            value = None
            temporary = locals().get("copied_values")
            if isinstance(temporary, dict):
                temporary.clear()
            assigned = getattr(self, "_values", None)
            if isinstance(assigned, dict):
                assigned.clear()
            self._outcomes = ()
            values = {}
            temporary = None
            assigned = None
            raise

    @property
    def outcomes(self) -> tuple[ResolutionOutcome, ...]:
        return self._outcomes

    def complete_or_raise(self) -> dict[str, str]:
        if all(outcome.category is ResolutionCategory.RESOLVED for outcome in self._outcomes):
            return dict(self._values)
        outcomes = self._outcomes
        self._values.clear()
        raise complete_resolution_error(outcomes) from None

    def discard_values(self) -> tuple[ResolutionOutcome, ...]:
        try:
            outcomes = self._outcomes
            self._values.clear()
            return outcomes
        except BaseException:
            self._values.clear()
            raise

    def scrub_values(self) -> None:
        self._values.clear()

    def __repr__(self) -> str:
        outcomes = getattr(self, "_outcomes", ())
        values = getattr(self, "_values", {})
        return f"ResolutionBatch(outcomes={len(outcomes)}, resolved={len(values)}, values=<redacted>)"

    __str__ = __repr__


_BATCH_TOKEN = object()


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
    for name in config.secret_config_data.backends:
        selected = source_backend_class(lookup, name)
        if selected is None:
            error = direct_backend_source_error(
                name=name,
                registry=registry,
                referrer=SettingReference(
                    setting="[secret_config].backends",
                    kind="secret-source",
                    name=name,
                ),
            )
            if error is not None:
                raise error
            registered = sorted(row.name for row in registry.iter_kind("secret-source"))
            raise ConfigError(
                f"[secret_config].backends names unknown secret-source {name!r}",
                hint=f"declared secret sources: {registered}",
            ) from None
        source, backend_class = selected
        if not isinstance(source, SecretSourceDecl):
            raise StateError(f"secret-source/{name} is not a SecretSourceDecl")
        if registry.graph.enablement_of("secret-source", name) is Enablement.disabled:
            continue
        disabled_backend_plugin: str | None = None
        if registry.graph.enablement_of("secret-backend", source.backend.name) is Enablement.disabled:
            backend_row = registry.lookup("secret-backend", source.backend.name)
            origin = getattr(backend_row, "origin", None)
            if getattr(origin, "variant", None) == "system-plugin":
                plugin = getattr(origin, "plugin", None)
                if type(plugin) is not str:
                    raise StateError(f"secret-backend/{source.backend.name} has invalid plugin attribution")
                disabled_backend_plugin = plugin
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
    identifier_failed = False
    try:
        identifier = source.backend_class.describe_lookup(secret.name, validated)
    except Exception:
        identifier_failed = True
        identifier = None
    if identifier_failed:
        raise _BackendProtocolError from None
    if identifier is not None and (type(identifier) is not str or not _safe_diagnostic_text(identifier)):
        raise _BackendProtocolError
    return SecretLookupRequest(name=secret.name, mapping=validated), identifier


class _BackendProtocolError(Exception):
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
) -> tuple[bool, str | None]:
    for source in sources:
        if not source.readiness.is_ready:
            continue
        if source.interactive and policy.interaction is InteractionPolicy.REFUSE:
            continue
        try:
            would_attempt = source.would_attempt(secret)
        except _BackendProtocolError:
            return False, source.name
        if would_attempt:
            return True, None
    return False, None


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
) -> object:
    budget = _MonotonicBudget.start(
        timeout,
        clock=time.monotonic,
    )
    remaining = budget.remaining
    context: AbstractContextManager[SecretSourceClient] | None = None
    driver: _SourceContextDriver | None = None
    client: SecretSourceClient | None = None
    entered_client: SecretSourceClient | None = None
    resolved: object | None = None
    try:
        _check_boundary(remaining)
        context = source.backend_class.create_client(
            source_name=source.name,
            config=source.config,
            interaction_broker=broker,
            remaining_time=remaining,
        )
        _check_boundary(remaining)
        driver = _SourceContextDriver(context, source_name=source.name, remaining_time=remaining)
        with driver as entered_client:
            client = entered_client
            _check_boundary(remaining)
            client.prepare(requests, remaining_time=remaining)
            _check_boundary(remaining)
            resolved = client.resolve(requests, remaining_time=remaining)
            _check_boundary(remaining)

        # Keep the post-context and return boundaries inside the same fence:
        # an asynchronous interruption here must first release every provider
        # object as well as a successfully returned value mapping.
        client = None
        entered_client = None
        driver = None
        context = None
        requests = ()
        broker = None
        return resolved
    except BaseException:
        resolved = None
        client = None
        entered_client = None
        driver = None
        context = None
        requests = ()
        broker = None
        raise


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
    returned: object | None = None
    returned_values: dict[str, str] | None = None
    returned_detail: ResolutionDetail | None = None
    raw_items: tuple[object, ...] | None = None
    raw_item: object | None = None
    raw_name: object | None = None
    raw_value: object | None = None
    value: str | None = None
    batch: ResolutionBatch | None = None
    evidence = {secret.name: _Evidence.empty() for secret in deduped}
    try:
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
                terminal_exists = any(
                    outcome.category is not ResolutionCategory.RESOLVED for outcome in outcomes.values()
                )
                if not terminal_exists:
                    for secret in still_missing:
                        attemptable, protocol_source = _remaining_attemptable(
                            secret,
                            sources[index:],
                            policy=policy,
                        )
                        if protocol_source is not None:
                            outcomes[secret.name] = _outcome(
                                secret.name,
                                ResolutionDetail.BACKEND_PROTOCOL,
                                source=protocol_source,
                            )
                        elif not attemptable:
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
            returned = None
            failure_kind: SecretClientFailureKind | None = None
            timed_out = False
            unexpected = False
            timeout_failed = False
            try:
                declared_timeout = source.backend_class.external_operation_timeout(source.config)
            except Exception:
                timeout_failed = True
                declared_timeout = None
            if timeout_failed:
                for request in requests:
                    outcomes[request.name] = _outcome(
                        request.name,
                        ResolutionDetail.UNEXPECTED,
                        source=source.name,
                        identifier=identifiers[request.name],
                    )
                continue
            if declared_timeout is not None and (
                type(declared_timeout) not in {int, float}
                or not math.isfinite(declared_timeout)
                or declared_timeout <= 0
            ):
                raise StateError(f"secret-source/{source.name} declared an invalid external-operation timeout")
            timeout = None if declared_timeout is None else float(declared_timeout)
            try:
                returned = _drive_source(source, requests, broker=broker, timeout=timeout)
            except UserAbort:
                raise
            except SecretClientTimeout:
                timed_out = True
            except SecretClientFailure as failure:
                failure_kind = failure.kind
            except Exception:
                unexpected = True
            if timed_out or failure_kind is not None or unexpected:
                returned = None
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
            # Snapshot the provider mapping in this already-protected frame.
            # Passing it into a helper would expose the plaintext-bearing
            # object in a callee frame before that helper could establish its
            # own cleanup fence.
            returned_values = {}
            returned_detail = None
            requested_names = frozenset(request.name for request in requests)
            try:
                if not isinstance(returned, Mapping):
                    returned_detail = ResolutionDetail.BACKEND_PROTOCOL
                else:
                    raw_items = tuple(returned.items())
                    for raw_item in raw_items:
                        if type(raw_item) is not tuple or len(raw_item) != 2:
                            returned_detail = ResolutionDetail.BACKEND_PROTOCOL
                            break
                        raw_name, raw_value = raw_item
                        if type(raw_name) is not str or type(raw_value) is not str or raw_name not in requested_names:
                            returned_detail = ResolutionDetail.BACKEND_PROTOCOL
                            break
                        returned_values[raw_name] = raw_value
            except Exception:
                returned_detail = ResolutionDetail.UNEXPECTED
            returned = None
            raw_items = None
            raw_item = None
            raw_name = None
            raw_value = None
            if returned_detail is not None:
                returned_values.clear()
                returned_values = None
                for request in requests:
                    outcomes[request.name] = _outcome(
                        request.name,
                        returned_detail,
                        source=source.name,
                    )
                continue
            assert returned_values is not None
            for request in requests:
                if request.name not in returned_values:
                    evidence[request.name].soft_missed.append((source.name, identifiers[request.name]))
                    continue
                value = returned_values[request.name]
                if "\n" in value or "\r" in value or "\0" in value:
                    outcomes[request.name] = _outcome(
                        request.name,
                        ResolutionDetail.MALFORMED_VALUE,
                        source=source.name,
                        identifier=identifiers[request.name],
                    )
                    value = None
                    continue
                values[request.name] = value
                outcomes[request.name] = _outcome(
                    request.name,
                    ResolutionDetail.RESOLVED,
                    source=source.name,
                    identifier=identifiers[request.name],
                )
                value = None
            returned_values.clear()
            returned_values = None

        for secret in deduped:
            if secret.name not in outcomes:
                outcomes[secret.name] = _collapse(
                    secret.name,
                    evidence[secret.name],
                    sources_empty=not sources,
                )
        ordered = tuple(outcomes[secret.name] for secret in deduped)
        batch = ResolutionBatch(ordered, values, _token=_BATCH_TOKEN)
        values.clear()
        return batch
    except BaseException:
        returned = None
        if returned_values is not None:
            returned_values.clear()
        returned_detail = None
        raw_items = None
        raw_item = None
        raw_name = None
        raw_value = None
        value = None
        values.clear()
        if batch is not None:
            batch._values.clear()
        batch = None
        raise


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
    """Resolve independent values for the explicit env reveal surface."""
    interaction = validate_interaction_policy(interaction)
    broker = OutputInteractionBroker(secrets) if interaction is InteractionPolicy.ALLOW else None
    projected: dict[str, str] = {}
    result = PartialResolution(values={}, outcomes=())
    batch = resolve_batch(
        secrets,
        sources,
        policy=ResolutionPolicy(interaction=interaction, completion=CompletionPolicy.PARTIAL),
        interaction_broker=broker,
    )
    try:
        result.outcomes = batch.outcomes
        projected = _copy_partial_values(batch)
        batch.scrub_values()
        result.values = projected
        projected = {}
        projected.clear()
        return result
    except BaseException:
        projected.clear()
        result.values.clear()
        batch.scrub_values()
        raise


def validate_chain(config: Config, registry: Registry) -> None:
    """Validate source-chain reachability for operator-declared secrets."""
    from agentworks.resources.access import secret_decls

    opted_in = set(config.secret_config_data.backends)
    operator_decls = [
        decl
        for decl in secret_decls(registry).values()
        if getattr(getattr(decl, "origin", None), "variant", None) == "operator-declared"
    ]
    unreachable = [
        decl
        for decl in operator_decls
        if not ({ref.name for ref in registry.graph.edges_of("secret", decl.name)} & opted_in)
    ]
    if unreachable:
        names = ", ".join(sorted(decl.name for decl in unreachable))
        chain = ", ".join(config.secret_config_data.backends) or "(empty)"
        raise ConfigError(
            f"unreachable secret(s): {names}",
            hint=(
                f"active source chain: [{chain}]. Add an attemptable secret-source, "
                "remove a false opt-out, or remove the unused declaration."
            ),
        )
