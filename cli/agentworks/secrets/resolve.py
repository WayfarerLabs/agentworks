"""Typed, source-first secret resolution with operation-bounded clients."""

from __future__ import annotations

import concurrent.futures
from contextlib import AbstractContextManager
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
    IndeterminateReason,
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
from agentworks.secrets.lookup import LookupDescriptionProtocolError, describe_lookup_exact
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
    from collections.abc import Mapping, Sequence
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


_CLEANUP_WARNING = "secret source {source_name!r}: cleanup failed; primary result unchanged"


def _is_protected_exit(error: BaseException) -> bool:
    return isinstance(error, (UserAbort, concurrent.futures.CancelledError)) or not isinstance(error, Exception)


def _warn_cleanup_failure(source_name: str, *, primary: BaseException | None) -> None:
    try:
        output.warn(_CLEANUP_WARNING.format(source_name=source_name))
    except BaseException as warning_error:
        if primary is None and _is_protected_exit(warning_error):
            raise


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
            if suppressed:
                _warn_cleanup_failure(self._source_name, primary=exc)
        except BaseException as cleanup_error:
            if exc is None and _is_protected_exit(cleanup_error):
                raise
            _warn_cleanup_failure(self._source_name, primary=exc)
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


class _SourceTurnInteractionBroker:
    """Restrict one backend client to its exact source-turn requests."""

    __slots__ = ("_active", "_allowed_names", "_closed", "_inner")

    def __init__(
        self,
        inner: InteractionBroker,
        requests: Sequence[SecretLookupRequest],
    ) -> None:
        self._active = False
        self._inner = inner
        self._allowed_names = frozenset(request.name for request in requests)
        self._closed = False

    def request_secret(self, name: str, /) -> str:
        if not self._active or type(name) is not str or name not in self._allowed_names:
            raise _BrokerScopeViolationError("secret backend requested terminal input outside its source turn")
        return self._inner.request_secret(name)

    def _activate(self) -> None:
        """Open the view for its selected client method."""
        if self._closed:
            raise StateError("secret source-turn interaction broker is closed")
        self._active = True

    def _revoke(self) -> None:
        """End this broker view's single source-turn lifetime."""
        self._active = False
        self._closed = True

    def __repr__(self) -> str:
        return f"_SourceTurnInteractionBroker(requests={len(self._allowed_names)})"


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


class _BrokerScopeViolationError(StateError):
    """A backend requested broker access outside its exact source turn."""

    __slots__ = ()


def _broker_allowed(
    source: ActiveSource,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
) -> bool:
    return (
        source.backend_class.supports_tty_interaction
        and tty_access is TtyInteractionAccess.AVAILABLE
        and (
            isinstance(intent, ResolutionIntent)
            or isinstance(intent, PreviewIntent)
            and intent.impact is OperatorImpact.ALLOW
        )
    )


def _require_interaction_broker(
    source: ActiveSource,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> None:
    """Validate the core caller's broker invariant before backend work."""
    if _broker_allowed(source, intent, tty_access) and interaction_broker is None:
        raise StateError(f"secret source {source.name!r} requires a terminal interaction broker")


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
        checked = describe_lookup_exact(source.backend_class, secret.name, validated)
    except LookupDescriptionProtocolError:
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
    tty_access: TtyInteractionAccess,
) -> dict[str, BackendPreview] | dict[str, BackendResolution]:
    """Validate a complete plugin result map before any value is copied."""
    from collections.abc import Mapping

    try:
        if not isinstance(returned, Mapping):
            raise _BackendProtocolError
        snapshot = dict(returned)
        snapshot_keys = tuple(snapshot)
        if any(type(key) is not str for key in snapshot_keys):
            raise _BackendProtocolError
        if len(snapshot_keys) != len(requests):
            raise _BackendProtocolError
        if any(request.name not in snapshot for request in requests):
            raise _BackendProtocolError
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except _BackendProtocolError:
        raise
    except Exception:
        raise _BackendProtocolError from None
    preview = preview_impact is not None
    normalized_results: dict[str, BackendPreview | BackendResolution] = {}
    try:
        for request in requests:
            result = snapshot[request.name]
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
                    if result.reason is IndeterminateReason.OPERATOR_INPUT_REQUIRED and (
                        preview_impact is not OperatorImpact.NONE
                        or not supports_tty_interaction
                        or tty_access is not TtyInteractionAccess.AVAILABLE
                    ):
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
            normalized_results[request.name] = normalized
    except (UserAbort, concurrent.futures.CancelledError):
        raise
    except _BackendProtocolError:
        raise
    except Exception:
        raise _BackendProtocolError from None
    return cast("dict[str, BackendPreview] | dict[str, BackendResolution]", normalized_results)


def _drive_source(
    source: ActiveSource,
    requests: tuple[SecretLookupRequest, ...],
    *,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> Mapping[str, BackendPreview] | Mapping[str, BackendResolution]:
    """Drive exactly one source method under a fresh operation client."""
    supports_tty = source.backend_class.supports_tty_interaction
    broker = (
        _SourceTurnInteractionBroker(cast("InteractionBroker", interaction_broker), requests)
        if _broker_allowed(source, intent, tty_access)
        else None
    )
    try:
        context = source.backend_class.create_client(
            config=source.config,
            intent=intent,
            tty_access=tty_access,
            interaction_broker=broker,
        )
        driver = _SourceContextDriver(context, source_name=source.name)
        with driver as client:
            returned: object
            if isinstance(intent, PreviewIntent):
                preview_method = client.preview
                if broker is not None:
                    broker._activate()
                try:
                    returned = preview_method(requests)
                finally:
                    if broker is not None:
                        broker._revoke()
                return _validate_result_map(
                    requests,
                    returned,
                    preview_impact=intent.impact,
                    supports_tty_interaction=supports_tty,
                    tty_access=tty_access,
                )
            resolve_method = client.resolve
            if broker is not None:
                broker._activate()
            try:
                returned = resolve_method(requests)
            finally:
                if broker is not None:
                    broker._revoke()
            return _validate_result_map(
                requests,
                returned,
                preview_impact=None,
                supports_tty_interaction=supports_tty,
                tty_access=tty_access,
            )
    finally:
        if broker is not None:
            broker._revoke()


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


def _mark_batch_doomed(
    names: Sequence[str],
    outcomes: dict[str, ResolutionOutcome],
) -> None:
    """Complete a terminal batch without attributing skipped work to a source."""
    for name in names:
        if name not in outcomes:
            outcomes[name] = _resolution_outcome(
                name,
                ResolutionBlocked(BlockReason.BATCH_DOOMED),
            )


def _resolve_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
    complete: bool,
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
    projection_cache: dict[
        tuple[int, str],
        tuple[SecretLookupRequest | None, LookupDescription] | None,
    ] = {}

    def projection_for(
        source_index: int,
        secret: SecretDecl,
    ) -> tuple[SecretLookupRequest | None, LookupDescription] | None:
        key = (source_index, secret.name)
        if key not in projection_cache:
            try:
                projection_cache[key] = _lookup_projection(secret, sources[source_index])
            except _BackendProtocolError:
                projection_cache[key] = None
        return projection_cache[key]

    def static_terminal_outcome(
        secret: SecretDecl,
        *,
        start: int,
    ) -> ResolutionOutcome | None:
        prospective = _Evidence(
            tty_blocks=list(evidence[secret.name].tty_blocks),
            missing=list(evidence[secret.name].missing),
            source_blocks=list(evidence[secret.name].source_blocks),
        )
        for source_index in range(start, len(sources)):
            source = sources[source_index]
            projection = projection_for(source_index, secret)
            if projection is None:
                return _resolution_outcome(
                    secret.name,
                    ResolutionFailed(FailureReason.BACKEND_PROTOCOL),
                    source=source.name,
                    backend=source.backend_class.name,
                )
            request, description = projection
            if request is None:
                continue
            reason: BlockReason | None = None
            if source.disabled_backend_plugin is not None:
                reason = BlockReason.BACKEND_PLUGIN_DISABLED
            elif not source.readiness.is_ready:
                reason = BlockReason.SOURCE_NOT_READY
            if reason is None:
                return None
            prospective.source_blocks.append((source.name, description.identifier, source.backend_class.name, reason))
        return _collapse_actual(secret.name, prospective, sources_empty=not sources)

    for source_index, source in enumerate(sources):
        unresolved = [declarations[name] for name in deduped if name not in outcomes]
        if not unresolved:
            break
        projected: list[tuple[SecretLookupRequest, str | None]] = []
        for secret in unresolved:
            projection = projection_for(source_index, secret)
            if projection is None:
                outcomes[secret.name] = _resolution_outcome(
                    secret.name,
                    ResolutionFailed(FailureReason.BACKEND_PROTOCOL),
                    source=source.name,
                    backend=source.backend_class.name,
                )
                continue
            request, description = projection
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

        if complete:
            terminal = any(outcome.status is ResolutionStatus.FAILED for outcome in outcomes.values())
            if not terminal:
                for secret in (declarations[name] for name in deduped if name not in outcomes):
                    outcome = static_terminal_outcome(secret, start=source_index)
                    if outcome is not None:
                        outcomes[secret.name] = outcome
                        terminal = True
            if terminal:
                _mark_batch_doomed(deduped, outcomes)
                break

        requests = tuple(request for request, _identifier in projected)
        identifiers = {request.name: identifier for request, identifier in projected}
        intent = ResolutionIntent()
        _require_interaction_broker(source, intent, tty_access, interaction_broker)
        try:
            returned = _drive_source(
                source,
                requests,
                intent=intent,
                tty_access=tty_access,
                interaction_broker=interaction_broker,
            )
        except (UserAbort, concurrent.futures.CancelledError):
            raise
        except (_BackendProtocolError, _BrokerScopeViolationError):
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


def resolve_batch(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> ResolutionBatch:
    """Resolve one complete batch in one bounded source-first pass."""
    return _resolve_batch(
        secrets,
        sources,
        tty_access=tty_access,
        interaction_broker=interaction_broker,
        complete=True,
    )


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
    batch = _resolve_batch(
        secrets,
        sources,
        tty_access=tty_access,
        interaction_broker=broker,
        complete=False,
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
