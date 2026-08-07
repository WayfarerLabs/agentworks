"""Named-secret proof keeps values and interactive backends contained."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.cli import app
from agentworks.config import Config
from agentworks.errors import (
    AgentworksError,
    ConfigError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    SecretMappingError,
    SecretUnavailableError,
)
from agentworks.resources.graph import Readiness
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
        self._interactive = interactive
        self.failure = failure
        self.value = value
        self.calls = 0

    @property
    def interactive(self) -> bool:
        return self._interactive

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


def _active(backend: object, *, ready: bool = True) -> ActiveBackend:
    return ActiveBackend(
        capability=backend,  # type: ignore[arg-type]
        readiness=Readiness.ready() if ready else Readiness.blocked("swordfish unavailable"),
        registered_name="test",
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
    assert not hasattr(result, "verified")
    assert not hasattr(result, "value")
    assert interactive.calls == 0
    assert regular.calls == 1


def test_verify_never_invokes_excluded_interactive_backend_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InertInteractiveBackend(_Backend):
        def would_attempt(self, secret: SecretDecl, mapping: object) -> bool:
            del secret, mapping
            raise RuntimeError("excluded backend exposed swordfish")

    interactive = InertInteractiveBackend(interactive=True)
    registry = SimpleNamespace(
        lookup=lambda kind, name: SecretDecl(name=name, description=""),
        graph=SimpleNamespace(),
        iter_kind_items=lambda kind: iter(()),
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, candidate_registry: [_active(interactive)],
    )

    with pytest.raises(SecretUnavailableError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert interactive.calls == 0
    assert "swordfish" not in repr(caught.value)


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


def test_verify_sanitizer_fails_closed_for_malformed_agentworks_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedProviderError(AgentworksError):
        def __init__(self) -> None:
            Exception.__init__(self, "provider exposed swordfish")

        def __getattribute__(self, name: str) -> object:
            if name in {"entity_kind", "entity_name", "hint"}:
                raise RuntimeError("attribute exposed swordfish")
            return super().__getattribute__(name)

    backend = _Backend(failure=MalformedProviderError())
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(ExternalError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None
    assert getattr(caught.value, "entity_kind", None) is None
    assert getattr(caught.value, "entity_name", None) is None
    assert "swordfish" not in repr(caught.value)


def test_verify_sanitizes_malformed_interactive_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedProviderError(AgentworksError):
        def __init__(self) -> None:
            Exception.__init__(self, "interactive property exposed swordfish")

    class RaisingInteractiveBackend(_Backend):
        @property
        def interactive(self) -> bool:
            raise MalformedProviderError

    backend = RaisingInteractiveBackend()
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(ExternalError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None
    assert "swordfish" not in repr(caught.value)
    assert backend.calls == 0


def test_verify_snapshots_interactive_decision_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class SingleReadInteractiveBackend(_Backend):
        property_reads = 0

        @property
        def interactive(self) -> bool:
            self.property_reads += 1
            if self.property_reads > 1:
                raise RuntimeError("interactive policy was read more than once")
            return False

    backend = SingleReadInteractiveBackend()
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: [_active(backend)],
    )

    result = verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert result.name == "token"
    assert backend.property_reads == 1
    assert backend.calls == 1


def test_verify_stops_before_reading_later_backend_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnreachedBackend(_Backend):
        property_reads = 0

        @property
        def interactive(self) -> bool:
            self.property_reads += 1
            raise RuntimeError("later policy exposed swordfish")

    winner = _Backend()
    unreached = UnreachedBackend()
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: [_active(winner), _active(unreached)],
    )

    result = verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert result.name == "token"
    assert winner.calls == 1
    assert unreached.property_reads == 0
    assert unreached.calls == 0


def test_verify_rejects_provider_authored_readiness_without_accessing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SecretBearingReadiness:
        property_reads = 0

        def __getattribute__(self, name: str) -> object:
            if name in {"is_ready", "reason", "is_available"}:
                type(self).property_reads += 1
                raise RuntimeError("readiness exposed swordfish")
            return super().__getattribute__(name)

    backend = _Backend()
    active = ActiveBackend(
        capability=backend,  # type: ignore[arg-type]
        readiness=SecretBearingReadiness(),  # type: ignore[arg-type]
        registered_name="test",
    )
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [active])

    with pytest.raises(ExternalError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert str(caught.value) == "secret verification failed"
    assert caught.value.__context__ is None
    assert "swordfish" not in repr(caught.value)
    assert SecretBearingReadiness.property_reads == 0
    assert backend.calls == 0


def test_verify_never_reads_provider_name_after_secret_soft_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FetchedValueNameBackend:
        interactive = False

        def __init__(self) -> None:
            self.fetched_value = ""
            self.name_reads = 0
            self.calls = 0

        @property
        def name(self) -> str:
            self.name_reads += 1
            return self.fetched_value

        def would_attempt(self, secret: SecretDecl, mapping: object) -> bool:
            del secret, mapping
            return True

        def describe_lookup(self, secret: SecretDecl, mapping: object) -> None:
            del secret, mapping

        def batch_get(self, wants: list[tuple[SecretDecl, object]]) -> dict[str, str]:
            del wants
            self.calls += 1
            self.fetched_value = "swordfish"
            return {}

    backend = FetchedValueNameBackend()
    registry = SimpleNamespace(
        lookup=lambda kind, name: SecretDecl(name=name, description=""),
        graph=SimpleNamespace(),
        iter_kind_items=lambda kind: iter(()),
    )
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(SecretUnavailableError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert backend.calls == 1
    assert backend.name_reads == 0
    assert "tried test" in str(caught.value.hint)
    assert "swordfish" not in str(caught.value)
    assert "swordfish" not in str(caught.value.hint)
    assert "swordfish" not in repr(caught.value)


def test_verify_distrusts_backend_authored_entity_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend(
        failure=ConnectivityError(
            "provider exposed swordfish",
            entity_kind="secret",
            entity_name="swordfish",
        )
    )
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [_active(backend)])

    with pytest.raises(ConnectivityError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert caught.value.entity_kind == "secret"
    assert caught.value.entity_name is None
    assert "swordfish" not in repr(caught.value)


def test_verify_preserves_first_party_chain_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: (_ for _ in ()).throw(ConfigError("unknown backend in configured chain")),
    )

    with pytest.raises(ConfigError, match="unknown backend in configured chain"):
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]


def test_verify_preserves_first_party_unavailable_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(
        lookup=lambda kind, name: SecretDecl(name=name, description=""),
        graph=SimpleNamespace(),
        iter_kind_items=lambda kind: iter(()),
    )
    monkeypatch.setattr("agentworks.secrets.resolve.active_backends", lambda config, registry: [])

    with pytest.raises(SecretUnavailableError, match="no active backend could resolve secret.*token") as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert caught.value.hint is not None
    assert "agw secret describe token" in caught.value.hint


def test_verify_does_not_misclassify_first_party_activation_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(lookup=lambda kind, name: SecretDecl(name=name, description=""))
    failure = RuntimeError("activation failed before any backend ran")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_backends",
        lambda config, registry: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RuntimeError) as caught:
        verify_named_secret(SimpleNamespace(), registry, "token")  # type: ignore[arg-type]

    assert caught.value is failure


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

    assert not hasattr(result, "verified")
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

    assert not hasattr(result, "verified")
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
