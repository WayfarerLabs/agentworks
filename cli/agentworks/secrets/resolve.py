"""Typed, source-first secret resolution with operation-bounded clients."""

from __future__ import annotations

import concurrent.futures
import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self, cast

from agentworks import output
from agentworks.capabilities.secret_backend.client import (
    BackendBlocked,
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    BlockReason,
    FailureReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    OperatorImpact,
    PreviewAvailable,
    PreviewBlocked,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    PreviewMissing,
    ResolutionIntent,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
    safe_identity,
)
from agentworks.errors import ConfigError, StateError, UserAbort
from agentworks.schema import AgwModel, RefOwner
from agentworks.secrets.outcomes import (
    ResolutionBlocked,
    ResolutionFailed,
    ResolutionMissing,
    ResolutionOutcome,
    ResolutionResolved,
    ResolutionStatus,
    complete_resolution_error,
)
from agentworks.secrets.policy import (
    TtyInteractionPolicy,
    require_exact_tty_interaction_policy,
    tty_interaction_access,
)

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
        try:
            safe_identity(self.backend_class.name)
        except ValueError:
            raise StateError(f"secret-source/{self.source.name} selected an unsafe backend identity") from None
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

    def mapping_for(self, secret: SecretDecl) -> tuple[bool, MappingValue | None]:
        """Return mapping presence separately from its JSON-native value."""
        if self.name not in secret.backend_mappings:
            return False, None
        return True, secret.backend_mappings[self.name]

    def describe_lookup(self, secret: SecretDecl) -> LookupDescription:
        """Return this source's validated static lookup projection."""
        _request, description = _lookup_projection(secret, self)
        return description


class ResolutionBatch:
    """A private value-bearing batch whose representation is redacted."""

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
        resolved_names = {outcome.name for outcome in copied_outcomes if outcome.status is ResolutionStatus.RESOLVED}
        if set(copied_values) != resolved_names:
            raise ValueError("resolution values must match resolved outcomes exactly")
        self._outcomes = copied_outcomes
        self._values = copied_values

    @property
    def outcomes(self) -> tuple[ResolutionOutcome, ...]:
        return self._outcomes

    def complete_or_raise(self) -> dict[str, str]:
        if all(outcome.status is ResolutionStatus.RESOLVED for outcome in self._outcomes):
            return dict(self._values)
        raise complete_resolution_error(self._outcomes) from None

    def __repr__(self) -> str:
        outcomes = getattr(self, "_outcomes", ())
        values = getattr(self, "_values", {})
        return f"ResolutionBatch(outcomes={len(outcomes)}, resolved={len(values)}, values=<redacted>)"

    __str__ = __repr__


@dataclass(slots=True)
class _Evidence:
    tty_blocks: list[tuple[str, str | None, str, BlockReason]]
    missing: list[tuple[str, str | None, str]]
    source_blocks: list[tuple[str, str | None, str, BlockReason]]

    @classmethod
    def empty(cls) -> Self:
        return cls(tty_blocks=[], missing=[], source_blocks=[])


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
    """Preserve the primary source result while always attempting cleanup."""

    def __init__(self, inner: AbstractContextManager[SecretSourceClient], *, source_name: str) -> None:
        self._inner = inner
        self._source_name = source_name

    def __enter__(self) -> SecretSourceClient:
        return self._inner.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            suppressed = self._inner.__exit__(exc_type, exc, traceback)
        except BaseException as cleanup_error:
            if exc is None and (
                isinstance(cleanup_error, (UserAbort, concurrent.futures.CancelledError))
                or not isinstance(cleanup_error, Exception)
            ):
                raise
            _warn_cleanup_failure(self._source_name)
        else:
            if suppressed:
                _warn_cleanup_failure(self._source_name)
        return False


class OutputInteractionBroker:
    """CLI-owned terminal prompt broker exposing only named requests."""

    __slots__ = ("_prompts",)

    def __init__(self, secrets: Sequence[SecretDecl]) -> None:
        self._prompts: dict[str, tuple[str, str | None]] = {}
        for secret in secrets:
            self._prompts.setdefault(secret.name, (secret.description, secret.hint))

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
        if registry.graph.enablement_of("secret-source", name) is Enablement.disabled:
            continue
        disabled_backend_plugin: str | None = None
        if registry.graph.enablement_of("secret-backend", source.backend.name) is Enablement.disabled:
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


class _BackendProtocolError(Exception):
    """A backend boundary returned a malformed shape."""

    __slots__ = ()


def _lookup_projection(
    secret: SecretDecl,
    source: ActiveSource,
) -> tuple[SecretLookupRequest | None, LookupDescription]:
    """Project one secret onto one source as a request and description."""
    from agentworks.capabilities.config import validate_capability_mapping

    try:
        safe_identity(secret.name)
        safe_identity(source.name)
    except ValueError:
        raise _BackendProtocolError from None
    mapping_present, mapping = source.mapping_for(secret)
    if mapping_present and mapping is False:
        return None, LookupDescription(LookupDisposition.NOT_APPLICABLE, None)
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
    try:
        description = source.backend_class.describe_lookup(secret.name, validated)
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except Exception:
        raise _BackendProtocolError from None
    if type(description) is not LookupDescription:
        raise _BackendProtocolError
    try:
        checked = LookupDescription(description.disposition, description.identifier)
    except ValueError:
        raise _BackendProtocolError from None
    if checked.disposition is LookupDisposition.NOT_APPLICABLE:
        return None, checked
    try:
        request = SecretLookupRequest(name=secret.name, mapping=validated)
    except ValueError:
        raise _BackendProtocolError from None
    return request, checked


_BACKEND_FAILURE_REASONS = frozenset(
    {
        FailureReason.INVALID_MAPPING,
        FailureReason.LOOKUP_REJECTED,
        FailureReason.AUTHENTICATION,
        FailureReason.CONNECTIVITY,
        FailureReason.DEADLINE_EXCEEDED,
        FailureReason.EXTERNAL,
        FailureReason.MALFORMED_VALUE,
    }
)
_BACKEND_BLOCK_REASONS = frozenset({BlockReason.TTY_UNAVAILABLE, BlockReason.TTY_INTERACTION_DISABLED})


def _validate_result_map(
    requests: tuple[SecretLookupRequest, ...],
    returned: object,
    *,
    preview_impact: OperatorImpact | None,
    supports_tty_interaction: bool,
) -> dict[str, BackendPreview] | dict[str, BackendResolution]:
    """Validate a complete plugin result map before any value is copied."""
    from collections.abc import Mapping

    try:
        if not isinstance(returned, Mapping):
            raise _BackendProtocolError
        snapshot = dict(returned)
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except _BackendProtocolError:
        raise
    except Exception:
        raise _BackendProtocolError from None
    names = {request.name for request in requests}
    if set(snapshot) != names:
        raise _BackendProtocolError
    preview = preview_impact is not None
    try:
        for name, result in tuple(snapshot.items()):
            normalized: BackendPreview | BackendResolution
            if preview:
                if type(result) is PreviewAvailable:
                    normalized = PreviewAvailable()
                elif type(result) is PreviewMissing:
                    normalized = PreviewMissing()
                elif type(result) is PreviewIndeterminate:
                    normalized = PreviewIndeterminate(result.reason)
                    if preview_impact is OperatorImpact.ALLOW:
                        raise _BackendProtocolError
                elif type(result) is PreviewBlocked:
                    normalized = PreviewBlocked(result.reason)
                    if result.reason not in _BACKEND_BLOCK_REASONS or not supports_tty_interaction:
                        raise _BackendProtocolError
                elif type(result) is PreviewFailed:
                    normalized = PreviewFailed(result.reason)
                    if result.reason not in _BACKEND_FAILURE_REASONS:
                        raise _BackendProtocolError
                else:
                    raise _BackendProtocolError
            elif type(result) is BackendResolved:
                normalized = BackendResolved(result.value)
            elif type(result) is BackendMissing:
                normalized = BackendMissing()
            elif type(result) is BackendBlocked:
                normalized = BackendBlocked(result.reason)
                if result.reason not in _BACKEND_BLOCK_REASONS or not supports_tty_interaction:
                    raise _BackendProtocolError
            elif type(result) is BackendFailed:
                normalized = BackendFailed(result.reason)
                if result.reason not in _BACKEND_FAILURE_REASONS:
                    raise _BackendProtocolError
            else:
                raise _BackendProtocolError
            snapshot[name] = normalized
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except _BackendProtocolError:
        raise
    except Exception:
        raise _BackendProtocolError from None
    return cast("dict[str, BackendPreview] | dict[str, BackendResolution]", snapshot)


def _drive_source(
    source: ActiveSource,
    requests: tuple[SecretLookupRequest, ...],
    *,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> Mapping[str, BackendPreview] | Mapping[str, BackendResolution]:
    """Drive exactly one source method under a fresh operation client."""
    budget = _MonotonicBudget.start(None)
    remaining = budget.remaining
    supports_tty = source.backend_class.supports_tty_interaction
    broker_allowed = (
        supports_tty
        and tty_access is TtyInteractionAccess.AVAILABLE
        and (
            isinstance(intent, ResolutionIntent)
            or isinstance(intent, PreviewIntent)
            and intent.impact is OperatorImpact.ALLOW
        )
    )
    broker = interaction_broker if broker_allowed else None
    if broker_allowed and broker is None:
        raise StateError(f"secret source {source.name!r} requires a terminal interaction broker")
    context = source.backend_class.create_client(
        source_name=source.name,
        config=source.config,
        intent=intent,
        tty_access=tty_access,
        interaction_broker=broker,
        remaining_time=remaining,
    )
    driver = _SourceContextDriver(context, source_name=source.name)
    returned: Mapping[str, BackendPreview] | Mapping[str, BackendResolution]
    impact: OperatorImpact | None
    with driver as client:
        if isinstance(intent, PreviewIntent):
            returned = client.preview(requests)
            impact = intent.impact
        else:
            returned = client.resolve(requests)
            impact = None
    return _validate_result_map(
        requests,
        returned,
        preview_impact=impact,
        supports_tty_interaction=supports_tty,
    )


def _resolution_outcome(
    name: str,
    result: ResolutionResolved | ResolutionMissing | ResolutionBlocked | ResolutionFailed,
    *,
    source: str | None = None,
    identifier: str | None = None,
    backend: str | None = None,
) -> ResolutionOutcome:
    return ResolutionOutcome(
        name=name,
        result=result,
        source=source,
        identifier=identifier,
        backend=backend,
    )


def _collapse_actual(name: str, evidence: _Evidence, *, sources_empty: bool) -> ResolutionOutcome:
    if evidence.tty_blocks:
        source, identifier, backend, reason = evidence.tty_blocks[0]
        return _resolution_outcome(
            name,
            ResolutionBlocked(reason),
            source=source,
            identifier=identifier,
            backend=backend,
        )
    if evidence.missing:
        source, identifier, backend = evidence.missing[0]
        return _resolution_outcome(
            name,
            ResolutionMissing(),
            source=source,
            identifier=identifier,
            backend=backend,
        )
    if evidence.source_blocks:
        source, identifier, backend, reason = evidence.source_blocks[0]
        return _resolution_outcome(
            name,
            ResolutionBlocked(reason),
            source=source,
            identifier=identifier,
            backend=backend,
        )
    reason = BlockReason.NO_ACTIVE_SOURCE if sources_empty else BlockReason.NO_ATTEMPTABLE_SOURCE
    return _resolution_outcome(name, ResolutionBlocked(reason))


def resolve_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> ResolutionBatch:
    """Resolve one deduplicated batch in one bounded source-first pass."""
    if type(tty_access) is not TtyInteractionAccess:
        raise StateError("tty_access must be an exact TtyInteractionAccess")
    deduped = list(dict.fromkeys(secret.name for secret in secrets))
    declarations: dict[str, SecretDecl] = {}
    for secret in secrets:
        declarations.setdefault(secret.name, secret)
    outcomes: dict[str, ResolutionOutcome] = {}
    values: dict[str, str] = {}
    evidence = {name: _Evidence.empty() for name in deduped}

    for source in sources:
        unresolved = [declarations[name] for name in deduped if name not in outcomes]
        if not unresolved:
            break
        projected: list[tuple[SecretLookupRequest, str | None]] = []
        for secret in unresolved:
            try:
                request, description = _lookup_projection(secret, source)
            except _BackendProtocolError:
                outcomes[secret.name] = _resolution_outcome(
                    secret.name,
                    ResolutionFailed(FailureReason.BACKEND_PROTOCOL),
                    source=source.name,
                    backend=source.backend_class.name,
                )
                continue
            if request is not None:
                projected.append((request, description.identifier))
        if not projected:
            continue
        block_reason: BlockReason | None = None
        if source.disabled_backend_plugin is not None:
            block_reason = BlockReason.BACKEND_PLUGIN_DISABLED
        elif not source.readiness.is_ready:
            block_reason = BlockReason.SOURCE_NOT_READY
        if block_reason is not None:
            for request, identifier in projected:
                evidence[request.name].source_blocks.append(
                    (source.name, identifier, source.backend_class.name, block_reason)
                )
            continue

        requests = tuple(request for request, _identifier in projected)
        identifiers = {request.name: identifier for request, identifier in projected}
        try:
            returned = _drive_source(
                source,
                requests,
                intent=ResolutionIntent(),
                tty_access=tty_access,
                interaction_broker=interaction_broker,
            )
        except (UserAbort, concurrent.futures.CancelledError):
            raise
        except _BackendProtocolError:
            returned = {request.name: BackendFailed(FailureReason.BACKEND_PROTOCOL) for request in requests}
        except Exception:
            returned = {request.name: BackendFailed(FailureReason.UNEXPECTED) for request in requests}

        for request in requests:
            result = cast("BackendResolution", returned[request.name])
            identity = {
                "source": source.name,
                "identifier": identifiers[request.name],
                "backend": source.backend_class.name,
            }
            if isinstance(result, BackendResolved):
                values[request.name] = result.value
                outcomes[request.name] = _resolution_outcome(request.name, ResolutionResolved(), **identity)
            elif isinstance(result, BackendMissing):
                evidence[request.name].missing.append(
                    (source.name, identifiers[request.name], source.backend_class.name)
                )
            elif isinstance(result, BackendBlocked):
                evidence[request.name].tty_blocks.append(
                    (source.name, identifiers[request.name], source.backend_class.name, result.reason)
                )
            else:
                assert isinstance(result, BackendFailed)
                outcomes[request.name] = _resolution_outcome(
                    request.name,
                    ResolutionFailed(result.reason),
                    **identity,
                )

    for name in deduped:
        if name not in outcomes:
            outcomes[name] = _collapse_actual(name, evidence[name], sources_empty=not sources)
    return ResolutionBatch(tuple(outcomes[name] for name in deduped), values)


@dataclass(slots=True)
class PartialResolution:
    """Explicit partial reveal result with values separate from diagnostics."""

    values: dict[str, str]
    outcomes: tuple[ResolutionOutcome, ...]

    def __repr__(self) -> str:
        return f"PartialResolution(outcomes={len(self.outcomes)}, values=<redacted>)"

    __str__ = __repr__


def resolve_partial_for_reveal(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    interaction: TtyInteractionPolicy,
) -> PartialResolution:
    """Resolve independent values for the explicit environment reveal surface."""
    import sys

    require_exact_tty_interaction_policy(interaction)
    tty_access = tty_interaction_access(interaction, terminal_input_usable=sys.stdin.isatty())
    broker = OutputInteractionBroker(secrets) if tty_access is TtyInteractionAccess.AVAILABLE else None
    batch = resolve_batch(
        secrets,
        sources,
        tty_access=tty_access,
        interaction_broker=broker,
    )
    return PartialResolution(values=dict(batch._values), outcomes=batch.outcomes)


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
                f"active source chain: [{source_chain}]. Add a candidate secret-source, "
                "remove a false opt-out, or remove the unused declaration."
            ),
        )
