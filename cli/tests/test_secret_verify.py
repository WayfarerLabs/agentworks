"""Value-free secret verification service and CLI intent wiring."""

from __future__ import annotations

import warnings
from getpass import GetPassWarning
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentworks.capabilities.secret_backend import (
    BackendPreview,
    BlockReason,
    FailureReason,
    IndeterminateReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewBlocked,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewMissing,
    TtyInteractionAccess,
)
from agentworks.cli import app
from agentworks.errors import NotFoundError, ValidationError
from agentworks.secrets import SecretDecl
from agentworks.secrets.preview import PreviewStatus, ResolutionPreview, SourcePreviewAttempt
from agentworks.secrets.verification import verify_secrets


class _Registry:
    def __init__(self, *names: str) -> None:
        self.declarations = {name: SecretDecl(name=name, description=name) for name in names}
        self.lookups: list[str] = []

    def lookup(self, kind: str, name: str) -> SecretDecl:
        assert kind == "secret"
        self.lookups.append(name)
        try:
            return self.declarations[name]
        except KeyError:
            raise KeyError(name) from None


def _available(name: str) -> ResolutionPreview:
    attempt = SourcePreviewAttempt("fixture", f"id:{name}", PreviewAvailable())
    return ResolutionPreview(name, PreviewAvailable(), "fixture", f"id:{name}", (attempt,))


def _preview(name: str, result: BackendPreview) -> ResolutionPreview:
    attempt = SourcePreviewAttempt("fixture", f"id:{name}", result)
    return ResolutionPreview(name, result, "fixture", f"id:{name}", (attempt,))


def test_verify_deduplicates_in_first_order_and_returns_only_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _Registry("a", "b")
    captured: dict[str, object] = {}

    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: ())

    def preview_batch(
        declarations: list[SecretDecl], sources: object, **kwargs: object
    ) -> dict[str, ResolutionPreview]:
        captured["names"] = [decl.name for decl in declarations]
        captured.update(kwargs)
        return {decl.name: _available(decl.name) for decl in declarations}

    monkeypatch.setattr("agentworks.secrets.verification.preview_batch", preview_batch)
    previews = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        ["b", "a", "b"],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
    )
    assert [preview.name for preview in previews] == ["b", "a"]
    assert captured["names"] == ["b", "a"]
    assert captured["impact"] is OperatorImpact.NONE
    assert captured["tty_access"] is TtyInteractionAccess.UNAVAILABLE
    assert "value" not in repr(previews).lower()


@pytest.mark.parametrize("names", [[], ["Bad Name"]])
def test_verify_rejects_invalid_requests(names: list[str]) -> None:
    with pytest.raises(ValidationError):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _Registry(),  # type: ignore[arg-type]
            names,
            impact=OperatorImpact.NONE,
            tty_access=TtyInteractionAccess.UNAVAILABLE,
        )


def test_verify_rejects_unknown_secret() -> None:
    with pytest.raises(NotFoundError):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _Registry(),  # type: ignore[arg-type]
            ["missing"],
            impact=OperatorImpact.NONE,
            tty_access=TtyInteractionAccess.UNAVAILABLE,
        )


def test_cli_keeps_operator_impact_separate_from_global_tty_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("agentworks.config.load_config", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _Registry("token"))

    def verify(config: object, registry: object, names: list[str], **kwargs: object) -> tuple[ResolutionPreview, ...]:
        captured.update(kwargs)
        return (_available("token"),)

    monkeypatch.setattr("agentworks.secrets.verification.verify_secrets", verify)
    result = CliRunner().invoke(
        app,
        ["--non-interactive", "secret", "verify", "token", "--allow-interaction"],
    )
    assert result.exit_code == 0, result.output
    assert captured["impact"] is OperatorImpact.ALLOW
    assert captured["tty_access"] is TtyInteractionAccess.DISABLED


@pytest.mark.parametrize(
    ("preview", "exit_code"),
    [
        (_available("token"), 0),
        (_preview("token", PreviewMissing()), 1),
        (_preview("token", PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)), 1),
        (_preview("token", PreviewBlocked(BlockReason.TTY_UNAVAILABLE)), 1),
        (_preview("token", PreviewFailed(FailureReason.EXTERNAL)), 1),
    ],
    ids=(status.value for status in PreviewStatus),
)
def test_cli_renders_every_technical_status_and_only_available_succeeds(
    preview: ResolutionPreview,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentworks.config.load_config", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _Registry("token"))
    monkeypatch.setattr("agentworks.secrets.verification.verify_secrets", lambda *args, **kwargs: (preview,))

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == exit_code
    assert preview.status.value in result.stdout


def test_cli_verify_drives_the_real_prompt_broker_without_exposing_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output
    from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
    from agentworks.cli._typer_output import TyperHandler
    from agentworks.resources.graph import Readiness
    from agentworks.schema import CapabilityBlock
    from agentworks.secrets import SecretSourceDecl
    from agentworks.secrets.resolve import ActiveSource

    source = ActiveSource(
        source=SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
        backend_class=PromptBackend,
        config=PromptSourceConfig(name="prompt"),
        readiness=Readiness.ready(),
    )
    monkeypatch.setattr("agentworks.config.load_config", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _Registry("token"))
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: (source,))
    monkeypatch.setattr(
        "agentworks.cli._helpers.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
    )

    value = "prompt-value-must-not-escape"
    previous_handler = output.get_handler()
    output.set_handler(TyperHandler())
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", GetPassWarning)
            result = CliRunner().invoke(
                app,
                ["secret", "verify", "token", "--allow-interaction"],
                input=f"{value}\n",
            )
    finally:
        output.set_handler(previous_handler)

    assert result.exit_code == 0, result.output
    assert PreviewStatus.AVAILABLE.value in result.stdout
    assert value not in result.stdout
    assert value not in result.stderr
