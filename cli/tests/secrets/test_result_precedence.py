"""Closed secret-result precedence and backend TTY authority."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import ClassVar, Literal, cast

import pytest
from pydantic import BaseModel

from agentworks.capabilities.secret_backend import (
    BackendBlocked,
    BackendPreview,
    BackendResolution,
    BlockReason,
    FailureReason,
    IndeterminateReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    OperatorImpact,
    PreviewBlocked,
    PreviewIndeterminate,
    PreviewMissing,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionStatus
from agentworks.secrets.preview import PreviewStatus, SourcePreviewAttempt, _aggregate, preview_batch
from agentworks.secrets.resolve import ActiveSource, _collapse_actual, _Evidence, resolve_batch

_PREVIEW_RESULTS: dict[str, tuple[BackendPreview, BackendPreview]] = {
    "source": (
        PreviewBlocked(BlockReason.SOURCE_NOT_READY),
        PreviewBlocked(BlockReason.BACKEND_PLUGIN_DISABLED),
    ),
    "missing": (PreviewMissing(), PreviewMissing()),
    "tty": (
        PreviewBlocked(BlockReason.TTY_UNAVAILABLE),
        PreviewBlocked(BlockReason.TTY_INTERACTION_DISABLED),
    ),
    "indeterminate": (
        PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED),
        PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED),
    ),
}


@pytest.mark.parametrize(
    ("categories", "status", "reason", "source"),
    [
        (
            frozenset(_PREVIEW_RESULTS),
            PreviewStatus.INDETERMINATE,
            IndeterminateReason.OPERATOR_IMPACT_LIMITED.value,
            "indeterminate-1",
        ),
        (
            frozenset({"source", "missing", "tty"}),
            PreviewStatus.BLOCKED,
            BlockReason.TTY_UNAVAILABLE.value,
            "tty-1",
        ),
        (frozenset({"source", "missing"}), PreviewStatus.MISSING, None, "missing-1"),
        (
            frozenset({"source"}),
            PreviewStatus.BLOCKED,
            BlockReason.SOURCE_NOT_READY.value,
            "source-1",
        ),
        (frozenset(), PreviewStatus.BLOCKED, "no-candidate", None),
    ],
    ids=("indeterminate", "tty", "missing", "source", "no-candidate"),
)
def test_exhausted_preview_evidence_category_matrix(
    categories: frozenset[str],
    status: PreviewStatus,
    reason: str | None,
    source: str | None,
) -> None:
    attempts = [
        SourcePreviewAttempt(f"{category}-{index}", f"id-{category}-{index}", result)
        for category in _PREVIEW_RESULTS
        if category in categories
        for index, result in enumerate(_PREVIEW_RESULTS[category], start=1)
    ]

    preview = _aggregate("secret", attempts)

    assert preview.status is status
    assert preview.reason == reason
    assert preview.source == source


@pytest.mark.parametrize(
    ("categories", "sources_empty", "status", "reason", "source"),
    [
        (
            frozenset({"source", "missing", "tty"}),
            False,
            ResolutionStatus.BLOCKED,
            BlockReason.TTY_UNAVAILABLE,
            "tty-1",
        ),
        (
            frozenset({"source", "missing"}),
            False,
            ResolutionStatus.MISSING,
            None,
            "missing-1",
        ),
        (
            frozenset({"source"}),
            False,
            ResolutionStatus.BLOCKED,
            BlockReason.SOURCE_NOT_READY,
            "source-1",
        ),
        (frozenset(), True, ResolutionStatus.BLOCKED, BlockReason.NO_ACTIVE_SOURCE, None),
        (frozenset(), False, ResolutionStatus.BLOCKED, BlockReason.NO_ATTEMPTABLE_SOURCE, None),
    ],
    ids=("tty", "missing", "source", "no-active-source", "no-attemptable-source"),
)
def test_exhausted_actual_evidence_category_matrix(
    categories: frozenset[str],
    sources_empty: bool,
    status: ResolutionStatus,
    reason: BlockReason | None,
    source: str | None,
) -> None:
    evidence = _Evidence.empty()
    if "source" in categories:
        evidence.source_blocks.extend(
            (
                ("source-1", "id-source-1", "backend", BlockReason.SOURCE_NOT_READY),
                ("source-2", "id-source-2", "backend", BlockReason.BACKEND_PLUGIN_DISABLED),
            )
        )
    if "missing" in categories:
        evidence.missing.extend((("missing-1", "id-missing-1", "backend"), ("missing-2", "id-missing-2", "backend")))
    if "tty" in categories:
        evidence.tty_blocks.extend(
            (
                ("tty-1", "id-tty-1", "backend", BlockReason.TTY_UNAVAILABLE),
                ("tty-2", "id-tty-2", "backend", BlockReason.TTY_INTERACTION_DISABLED),
            )
        )

    outcome = _collapse_actual("secret", evidence, sources_empty=sources_empty)

    assert outcome.status is status
    assert outcome.reason is reason
    assert outcome.source == source


class _Config(AgwModel):
    name: Literal["fixture"]


class _Mapping(AgwRootModel[str]):
    pass


class _Client:
    preview_result: ClassVar[BackendPreview]
    resolution_result: ClassVar[BackendResolution]

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        return {request.name: self.preview_result for request in requests}

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        return {request.name: self.resolution_result for request in requests}


class _Backend(SecretBackend):
    name = "fixture"
    description = "fixture"
    contract_version = 1
    config_model = _Config
    mapping_model = _Mapping
    supports_tty_interaction = False
    client_type: ClassVar[type[_Client]] = _Client

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.CANDIDATE, secret_name)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        return nullcontext(cls.client_type())


def _source(client_type: type[_Client]) -> ActiveSource:
    backend = cast("type[_Backend]", type("RuntimeBackend", (_Backend,), {"client_type": client_type}))
    return ActiveSource(
        source=SecretSourceDecl(name="fixture", backend=CapabilityBlock.of("fixture")),
        backend_class=backend,
        config=_Config(name="fixture"),
        readiness=Readiness.ready(),
    )


@pytest.mark.parametrize("preview", [True, False], ids=("preview", "resolution"))
@pytest.mark.parametrize(
    "reason",
    [BlockReason.TTY_UNAVAILABLE, BlockReason.TTY_INTERACTION_DISABLED],
    ids=("tty-unavailable", "tty-disabled"),
)
def test_non_tty_backend_tty_block_is_backend_protocol_failure(
    preview: bool,
    reason: BlockReason,
) -> None:
    class Client(_Client):
        preview_result = PreviewBlocked(reason)
        resolution_result = BackendBlocked(reason)

    source = _source(Client)
    declaration = SecretDecl(name="secret", description="secret")
    if preview:
        preview_result = preview_batch(
            [declaration],
            [source],
            impact=OperatorImpact.NONE,
            tty_access=TtyInteractionAccess.UNAVAILABLE,
            interaction_broker=None,
        )["secret"]
        assert preview_result.status is PreviewStatus.FAILED
        assert preview_result.reason == FailureReason.BACKEND_PROTOCOL.value
    else:
        resolution_result = resolve_batch(
            [declaration],
            [source],
            tty_access=TtyInteractionAccess.UNAVAILABLE,
            interaction_broker=None,
        ).outcomes[0]
        assert resolution_result.status is ResolutionStatus.FAILED
        assert resolution_result.reason is FailureReason.BACKEND_PROTOCOL
