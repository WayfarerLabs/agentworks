"""Named-secret verification remains a value-free typed-core adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.capabilities.secret_backend.client import (
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretClientTimeout,
)
from agentworks.cli import app
from agentworks.errors import (
    ConfigError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    SecretMappingError,
    SecretUnavailableError,
    ValidationError,
)
from agentworks.secrets import SecretDecl
from agentworks.secrets.verification import (
    SecretInteractionPolicy,
    SecretVerification,
    verify_named_secret,
)
from tests.secrets.test_resolution_lifecycle import _Backend, _source


class _Registry:
    def __init__(self, decl: SecretDecl | None) -> None:
        self.decl = decl

    def lookup(self, kind: str, name: str) -> SecretDecl:
        if self.decl is None:
            raise KeyError(name)
        return self.decl


@pytest.fixture(autouse=True)
def _reset() -> object:
    _Backend.events = []
    _Backend.values = {}
    _Backend.failure = None
    output.set_non_interactive(False)
    yield
    output.set_non_interactive(False)


def test_verify_returns_only_named_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-secret-value"}
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_source()])

    result = verify_named_secret(SimpleNamespace(), _Registry(SecretDecl(name="token", description="token")), "token")

    assert result == SecretVerification(name="token")
    assert "sentinel" not in repr(result)


def test_verify_missing_name_is_typed_not_found() -> None:
    with pytest.raises(NotFoundError, match="secret 'missing' not found"):
        verify_named_secret(SimpleNamespace(), _Registry(None), "missing")


def test_verify_soft_miss_raises_value_free_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_source()])
    with pytest.raises(SecretUnavailableError) as caught:
        verify_named_secret(SimpleNamespace(), _Registry(SecretDecl(name="token", description="token")), "token")
    assert "token" in str(caught.value)


def test_verify_rejects_invalid_policy() -> None:
    with pytest.raises(ValidationError, match="explicit interaction policy"):
        verify_named_secret(
            SimpleNamespace(),
            _Registry(SecretDecl(name="token", description="token")),
            "token",
            interaction_policy=object(),  # type: ignore[arg-type]
        )


def test_global_non_interactive_overrides_explicit_allow() -> None:
    output.set_non_interactive(True)
    with pytest.raises(ValidationError, match="global non-interactive"):
        verify_named_secret(
            SimpleNamespace(),
            _Registry(SecretDecl(name="token", description="token")),
            "token",
            interaction_policy=SecretInteractionPolicy.ALLOW_INTERACTIVE,
        )


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.HARD_MAPPING,
                remediation=SecretClientRemediation.CHECK_MAPPING,
            ),
            SecretMappingError,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.AUTHENTICATION,
                remediation=SecretClientRemediation.SIGN_IN,
            ),
            ConnectivityError,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.CONNECTIVITY,
                remediation=SecretClientRemediation.CHECK_CONNECTIVITY,
            ),
            ConnectivityError,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.EXTERNAL,
                remediation=SecretClientRemediation.RETRY,
            ),
            ExternalError,
        ),
        (SecretClientTimeout(), ExternalError),
        (RuntimeError("provider exposed sentinel-secret"), ExternalError),
    ],
)
def test_verify_preserves_safe_error_categories_without_provider_text(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    error_type: type[Exception],
) -> None:
    _Backend.failure = failure
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_source()])
    with pytest.raises(error_type) as caught:
        verify_named_secret(SimpleNamespace(), _Registry(SecretDecl(name="token", description="token")), "token")
    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None
    assert "sentinel-secret" not in repr(caught.value)


class _InteractiveBackend(_Backend):
    interactive: ClassVar[bool] = True
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None


class _RegularBackend(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None


def test_verify_default_refuses_interactive_source_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _InteractiveBackend.events = []
    _InteractiveBackend.values = {"token": "interactive-sentinel"}
    _RegularBackend.events = []
    _RegularBackend.values = {"token": "regular-sentinel"}
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: [
            _source(name="interactive", backend_class=_InteractiveBackend),
            _source(name="regular", backend_class=_RegularBackend),
        ],
    )
    result = verify_named_secret(SimpleNamespace(), _Registry(SecretDecl(name="token", description="token")), "token")
    assert result == SecretVerification(name="token")
    assert _InteractiveBackend.events == []
    assert _RegularBackend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_verify_preserves_first_party_chain_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = ConfigError("unknown source in configured chain")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(ConfigError) as caught:
        verify_named_secret(SimpleNamespace(), _Registry(SecretDecl(name="token", description="token")), "token")
    assert caught.value is failure


def test_secret_verify_cli_emits_exactly_one_value_free_success_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-secret-value"}
    registry = _Registry(SecretDecl(name="token", description="token"))
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, candidate: [_source()])

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 0
    assert result.stdout == "Secret 'token' verified.\n"
    assert result.stderr == ""
    assert "sentinel-secret-value" not in result.output
