"""Named-secret proof keeps values and interactive backends contained."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.cli import app
from agentworks.config import Config
from agentworks.errors import AgentworksError, ConnectivityError, ExternalError, NotFoundError, SecretMappingError
from agentworks.resources.registry import Registry
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.resolve import ActiveBackend
from agentworks.secrets.verification import SecretInteractionPolicy, verify_named_secret


class _Backend:
    name = "test"

    def __init__(
        self,
        *,
        interactive: bool = False,
        failure: Exception | None = None,
        value: str = "swordfish",
    ) -> None:
        self.interactive = interactive
        self.failure = failure
        self.value = value
        self.calls = 0

    def would_attempt(self, secret: SecretDecl, mapping: object) -> bool:
        del secret, mapping
        return True

    def describe_lookup(self, secret: SecretDecl, mapping: object) -> None:
        del secret, mapping

    def batch_get(self, wants: list[tuple[SecretDecl, object]]) -> dict[str, str]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return {secret.name: self.value for secret, _mapping in wants}


def _active(backend: _Backend, *, ready: bool = True) -> ActiveBackend:
    return ActiveBackend(
        capability=backend,  # type: ignore[arg-type]
        readiness=SimpleNamespace(is_ready=ready, reason="swordfish unavailable"),  # type: ignore[arg-type]
    )


def test_verify_filters_interactive_and_returns_no_value(monkeypatch: pytest.MonkeyPatch) -> None:
    interactive = _Backend(interactive=True)
    regular = _Backend()
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, candidate_registry: [_active(interactive), _active(regular)],
    )

    result = verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert result.name == "token"
    assert result.verified is True
    assert not hasattr(result, "value")
    assert interactive.calls == 0
    assert regular.calls == 1


def test_verify_sanitizes_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend(failure=ConnectivityError("backend exposed swordfish", hint="swordfish"))
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, candidate_registry: [_active(backend)],
    )

    with pytest.raises(ConnectivityError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert "swordfish" not in str(caught.value)
    assert caught.value.hint is None


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (SecretMappingError("mapped swordfish", hint="swordfish"), SecretMappingError),
        (RuntimeError("untyped swordfish"), ExternalError),
    ],
)
def test_verify_sanitizes_every_backend_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    error_type: type[Exception],
) -> None:
    backend = _Backend(failure=failure)
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(error_type) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None
    assert "swordfish" not in caplog.text
    assert "swordfish" not in repr(caught.value)


def test_verify_sanitizes_backend_activation_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: (_ for _ in ()).throw(RuntimeError("activation exposed swordfish")),
    )

    with pytest.raises(ExternalError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None


def test_verify_preserves_ordered_fallback_and_quiets_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped = _Backend(value="swordfish")
    miss = _Backend(value="")
    winner = _Backend(value="swordfish")
    # An empty result is the provider soft-miss contract.
    miss.batch_get = lambda wants: {}  # type: ignore[method-assign]
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: [_active(skipped, ready=False), _active(miss), _active(winner)],
    )
    events: list[str] = []
    monkeypatch.setattr(output, "warn", lambda message: events.append(message))
    monkeypatch.setattr(output, "info", lambda message: events.append(message))

    result = verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert result.verified is True
    assert skipped.calls == 0
    assert winner.calls == 1
    assert events == []


def test_verify_explicit_interactive_consent_and_global_state_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend(interactive=True)
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])
    output.set_non_interactive(False)
    before = output.non_interactive()

    result = verify_named_secret(
        SimpleNamespace(), registry, "token", interaction_policy=SecretInteractionPolicy.ALLOW_INTERACTIVE
    )  # type: ignore[arg-type]

    assert result.verified is True
    assert backend.calls == 1
    assert output.non_interactive() is before


def test_verify_service_enforces_global_noninteractive_policy() -> None:
    output.set_non_interactive(True)
    try:
        with pytest.raises(AgentworksError, match="interactive secret verification is unavailable"):
            verify_named_secret(
                SimpleNamespace(),  # type: ignore[arg-type]
                SimpleNamespace(),  # type: ignore[arg-type]
                "token",
                interaction_policy=SecretInteractionPolicy.ALLOW_INTERACTIVE,
            )
    finally:
        output.set_non_interactive(False)


def test_custom_agentworks_error_subclass_is_not_reconstructed(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProviderError(ConnectivityError):
        pass

    backend = _Backend(failure=ProviderError("backend exposed swordfish"))
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(ExternalError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert type(caught.value) is ExternalError
    assert caught.value.__context__ is None
    assert caught.value.__traceback__ is not None
    assert "swordfish" not in str(caught.value)


def test_secret_verify_cli_emits_exactly_one_success_line(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend(value="swordfish")
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 0
    assert result.stdout == "Secret 'token' verified.\n"
    assert result.stderr == ""
    assert "swordfish" not in result.output


def test_secret_verify_cli_rejects_interactive_noninteractive_conflict() -> None:
    result = CliRunner().invoke(app, ["--non-interactive", "secret", "verify", "token", "--allow-interactive"])

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "--allow-interactive cannot be used with --non-interactive" in str(result.exception)


def test_verify_requires_registered_secret() -> None:
    def missing(kind: str, name: str) -> object:
        raise KeyError((kind, name))

    with pytest.raises(NotFoundError, match="secret 'absent' not found"):
        verify_named_secret(
            cast("Config", SimpleNamespace()),
            cast("Registry", SimpleNamespace(lookup=missing)),
            "absent",
        )
