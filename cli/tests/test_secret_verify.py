"""One-name CLI checkpoint over the shared multi-name verification service."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.cli import app
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.secrets import SecretDecl
from agentworks.secrets.outcomes import ResolutionCategory, ResolutionDetail
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.verification import verify_secrets
from tests.secrets.test_resolution_lifecycle import _Backend, _source


class _Registry:
    def __init__(self, declarations: dict[str, SecretDecl]) -> None:
        self.declarations = declarations

    def lookup(self, kind: str, name: str) -> SecretDecl:
        try:
            return self.declarations[name]
        except KeyError:
            raise KeyError(name) from None


@pytest.fixture(autouse=True)
def _reset() -> object:
    _Backend.events = []
    _Backend.values = {}
    _Backend.failure = None
    output.set_non_interactive(False)
    yield
    output.set_non_interactive(False)


def _verify(monkeypatch: pytest.MonkeyPatch, *names: str):
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [_source()])
    registry = _Registry({name: SecretDecl(name=name, description=name) for name in names})
    return verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        names,
        interaction=InteractionPolicy.REFUSE,
    )


def test_verify_returns_value_free_shared_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-secret-value"}
    outcomes = _verify(monkeypatch, "token")
    assert len(outcomes) == 1
    assert outcomes[0].category is ResolutionCategory.RESOLVED
    assert outcomes[0].detail is ResolutionDetail.RESOLVED
    assert "sentinel" not in repr(outcomes)


def test_verify_preserves_first_order_dedupe_in_one_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"a": "one", "b": "two"}
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [_source()])
    registry = _Registry(
        {
            "a": SecretDecl(name="a", description="a"),
            "b": SecretDecl(name="b", description="b"),
        }
    )
    outcomes = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        ["b", "a", "b"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert [outcome.name for outcome in outcomes] == ["b", "a"]
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_verify_returns_soft_miss_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    (outcome,) = _verify(monkeypatch, "token")
    assert outcome.category is ResolutionCategory.UNAVAILABLE
    assert outcome.detail is ResolutionDetail.SOFT_MISS


def test_verify_missing_name_is_typed_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [_source()])
    with pytest.raises(NotFoundError, match="secret 'missing' not found"):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _Registry({}),  # type: ignore[arg-type]
            ["missing"],
            interaction=InteractionPolicy.REFUSE,
        )


@pytest.mark.parametrize("names", [[], [""], ["--bad"], ["bad\nname"]])
def test_verify_rejects_empty_or_unsafe_names_without_echo(names: list[str]) -> None:
    with pytest.raises(ValidationError) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _Registry({}),  # type: ignore[arg-type]
            names,
            interaction=InteractionPolicy.REFUSE,
        )
    assert "bad\nname" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_verify_rejects_non_exact_policy_before_other_work() -> None:
    with pytest.raises(StateError, match="exact InteractionPolicy"):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _Registry({}),  # type: ignore[arg-type]
            [],
            interaction="refuse",  # type: ignore[arg-type]
        )


def test_secret_verify_cli_emits_one_value_free_success_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-secret-value"}
    registry = _Registry({"token": SecretDecl(name="token", description="token")})
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, candidate: [_source()])

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 0
    assert result.stdout == "Secret 'token' verified.\n"
    assert result.stderr == ""
    assert "sentinel-secret-value" not in result.output


def test_secret_verify_cli_global_refusal_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    result = CliRunner().invoke(app, ["--non-interactive", "secret", "verify", "token", "--allow-interactive"])
    assert result.exit_code != 0
    assert "--allow-interactive cannot be used with --non-interactive" in str(result.exception)
