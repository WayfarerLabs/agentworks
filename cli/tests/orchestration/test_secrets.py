"""Operation-owned secret preview and scoped delivery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.capabilities.secret_backend import (
    FailureReason,
    IndeterminateReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewMissing,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError, StateError
from agentworks.orchestration.node import Node as OrchestrationNode
from agentworks.orchestration.secrets import (
    ScopedSecrets,
    predict_resolution,
    require_predicted_refs,
    secret_union,
)
from agentworks.resources.reference import ResourceReference
from agentworks.secrets import SecretDecl
from agentworks.secrets.preview import ResolutionPreview, SourcePreviewAttempt


def _preview(name: str, result: object, *attempt_results: object) -> ResolutionPreview:
    attempts = tuple(
        SourcePreviewAttempt("fixture", f"id:{name}", attempt)  # type: ignore[arg-type]
        for attempt in (attempt_results or (result,))
    )
    return ResolutionPreview(
        name,
        result,  # type: ignore[arg-type]
        "fixture",
        f"id:{name}",
        attempts,
    )


def _ref(name: str) -> ResourceReference:
    return ResourceReference(
        name=name,
        kind="secret",
        usage="test credential",
        source=("fixture", "owner"),
    )


def test_predict_resolution_always_uses_zero_operator_impact(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def preview_batch(decls: tuple[SecretDecl, ...], sources: object, **kwargs: object) -> dict[str, ResolutionPreview]:
        captured.update(kwargs)
        return {decl.name: _preview(decl.name, PreviewAvailable()) for decl in decls}

    monkeypatch.setattr("agentworks.secrets.preview.preview_batch", preview_batch)
    predictions = predict_resolution(
        [SecretDecl(name="token", description="token")],
        (),
        tty_access=TtyInteractionAccess.DISABLED,
    )
    assert predictions["token"].result == PreviewAvailable()
    assert captured["impact"] is OperatorImpact.NONE
    assert captured["tty_access"] is TtyInteractionAccess.DISABLED


@pytest.mark.parametrize(
    "preview",
    [
        _preview("token", PreviewAvailable()),
        _preview("token", PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)),
        _preview(
            "token",
            PreviewFailed(FailureReason.AUTHENTICATION),
            PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED),
            PreviewFailed(FailureReason.AUTHENTICATION),
        ),
    ],
)
def test_preflight_accepts_available_indeterminate_and_later_failure_after_uncertainty(
    preview: ResolutionPreview,
) -> None:
    require_predicted_refs(
        "fixture/owner",
        [_ref("token")],
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        tty_access=TtyInteractionAccess.DISABLED,
        preview_memo={"token": preview},
        sources=(),
    )


def test_preflight_rejects_definitive_missing() -> None:
    with pytest.raises(ConfigError, match="cannot pass preflight"):
        require_predicted_refs(
            "fixture/owner",
            [_ref("token")],
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            tty_access=TtyInteractionAccess.UNAVAILABLE,
            preview_memo={"token": _preview("token", PreviewMissing())},
            sources=(),
        )


def test_secret_union_is_stable_and_scoped_delivery_is_enforced() -> None:
    class Node:
        def __init__(self, *names: str) -> None:
            self._names = names

        def secret_refs(self) -> tuple[str, ...]:
            return self._names

    nodes = cast("list[OrchestrationNode]", [Node("b", "a"), Node("a", "c")])
    assert secret_union(nodes) == ("b", "a", "c")
    scoped = ScopedSecrets({"a": "value-a", "b": "value-b"}, ["a"])
    assert scoped.get("a") == "value-a"
    with pytest.raises(StateError):
        scoped.get("b")
